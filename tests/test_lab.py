import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / name) for name in ("scripts", "ansible/files")]

from lab import Lab
from runtime import Terraform, admin_access, save
import sync_dns
import verify
from yc import choose, requirements


def portal_record(suffix, address):
    identifier = "portal-" + suffix
    return {
        "Service": {"ID": identifier, "Address": address, "Service": "portal", "Port": 443},
        "Checks": [{"Status": "passing", "ServiceID": ""}, {"Status": "passing", "ServiceID": identifier}],
    }


class DnsTests(unittest.TestCase):
    settings = {"portal": "dz09.example.ru", "instances": {
        "portal-a": "10.42.1.11", "portal-b": "10.42.1.12", "portal-c": "10.42.1.13",
    }}

    def test_sync_removes_and_restores_exact_addresses(self):
        full = set(self.settings["instances"].values())
        reduced = full - {"10.42.1.12"}
        records = [portal_record(key[-1], value) for key, value in self.settings["instances"].items()]
        with (patch.object(sync_dns, "local_json", side_effect=[records, records[::2], records, []]),
              patch.object(sync_dns, "dns_addresses", side_effect=[full, full, reduced, full]),
              patch.object(sync_dns, "update_records") as update):
            for _ in range(4):
                sync_dns.synchronize(self.settings)
        self.assertEqual([call.args[1] for call in update.call_args_list], [reduced, full, set()])

    def test_failed_or_invalid_consul_reads_do_not_update(self):
        for response in (TimeoutError(), {}, [portal_record("b", "10.42.1.11")],
                         [portal_record("d", "10.42.1.14")]):
            with self.subTest(response=response):
                payload = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
                with (patch.object(sync_dns, "local_json", **payload),
                      patch.object(sync_dns, "update_records") as update):
                    with self.assertRaises((TimeoutError, ValueError)):
                        sync_dns.synchronize(self.settings)
                    update.assert_not_called()

    def test_dns_parser_checks_authority_and_error_status(self):
        data = (";; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 42\n"
                ";; flags: qr aa; QUERY: 1, ANSWER: 2\n"
                "dz09.example.ru.\t5\tIN\tA\t10.42.1.12\n"
                "dz09.example.ru.\t5\tIN\tA\t10.42.1.11\n")
        result = subprocess.CompletedProcess([], 0, data, "")
        with patch.object(sync_dns.subprocess, "run", return_value=result):
            self.assertEqual(sync_dns.dns_addresses("dz09.example.ru", 53, authoritative=True),
                             {"10.42.1.11", "10.42.1.12"})
            result.stdout = data.replace("qr aa", "qr")
            with self.assertRaises(RuntimeError):
                sync_dns.dns_addresses("dz09.example.ru", 53, authoritative=True)
            result.stdout = data.replace("NOERROR", "SERVFAIL")
            with self.assertRaises(RuntimeError):
                sync_dns.dns_addresses("dz09.example.ru", 53, authoritative=True)

    def test_nsupdate_sends_one_transaction(self):
        with patch.object(sync_dns.subprocess, "run") as command:
            sync_dns.update_records("dz09.example.ru", {"10.42.1.13", "10.42.1.11"})
        script = command.call_args.kwargs["input"].splitlines()
        self.assertEqual(script.count("send"), 1)
        self.assertEqual(script[2:5], [
            "update delete dz09.example.ru. A",
            "update add dz09.example.ru. 5 A 10.42.1.11",
            "update add dz09.example.ru. 5 A 10.42.1.13",
        ])


class VerificationTests(unittest.TestCase):
    def test_vm_restore_retries_start_after_temporary_rejection(self):
        check = self.check()
        check.compute = Mock(side_effect=[
            {"status": "STOPPED"}, RuntimeError("temporary rejection"),
            {"status": "STOPPED"}, {}, {"status": "RUNNING"},
        ])
        with patch.object(verify.time, "sleep"):
            check.restore_vm()
        self.assertEqual(sum(call.args == ("start",) for call in check.compute.call_args_list), 2)

    def check(self):
        nodes = {name: {"id": name.replace("-", ""), "private": address}
                 for name, address in {"web-a": "10.42.1.11", "web-b": "10.42.1.12",
                                       "web-c": "10.42.1.13", "client": "10.42.2.10"}.items()}
        return verify.Check({"nodes": nodes, "portal": "dz09.example.ru", "name": "otus-dz09-test",
                             "folder": "folder"})

    def test_verifier_restores_service_on_failure(self):
        check = self.check()
        check.cluster = Mock()
        check.chain = Mock(side_effect=[None, RuntimeError("BIND failed")])
        check.ssh = Mock()
        with patch.object(verify, "wait_for", side_effect=lambda _, action, **kwargs: action()):
            with self.assertRaisesRegex(RuntimeError, "BIND failed"):
                check.verify()
        self.assertEqual([call.args[-2:] for call in check.ssh.call_args_list],
                         [("is-active", "nginx"), ("stop", "nginx"), ("start", "nginx")])

    def test_verifier_restores_vm_on_failure(self):
        check = self.check()
        check.cluster = Mock()
        check.chain = Mock(side_effect=[None, None, None, RuntimeError("HTTPS failed")])
        check.ssh = Mock()
        check.compute = Mock()
        check.power = Mock()
        check.restore_vm = Mock()
        with patch.object(verify, "wait_for", side_effect=lambda _, action, **kwargs: action()):
            with self.assertRaisesRegex(RuntimeError, "HTTPS failed"):
                check.verify()
        check.compute.assert_called_once_with("stop")
        check.restore_vm.assert_called_once_with()


class PlacementAndLifecycleTests(unittest.TestCase):
    def test_additive_ingress_accepts_computed_rule_metadata_and_empty_defaults(self):
        previous = ["8.8.8.8/32"]
        approved = [*previous, "1.1.1.1/32"]
        before = {"port": 22, "protocol": "TCP", "v4_cidr_blocks": previous,
                  "labels": {}, "id": "old", "security_group_id": ""}
        after = {"port": 22, "protocol": "TCP", "v4_cidr_blocks": approved,
                 "security_group_id": None}
        resource = {"address": "yandex_vpc_security_group.client",
                    "type": "yandex_vpc_security_group",
                    "change": {"actions": ["update"], "before": {"ingress": [before]},
                               "after": {"ingress": [after]},
                               "after_unknown": {"ingress": [{"id": True, "labels": True}]}}}
        self.assertTrue(Terraform.client_ingress_update(resource, approved))
        after["port"] = 23
        self.assertFalse(Terraform.client_ingress_update(resource, approved))

    def test_placement_respects_quota_and_subnet_conflicts(self):
        policy = json.loads((ROOT / "config/lab.json").read_text())
        zones = set(policy["subnet_zones"].values())
        available = requirements(policy)
        self.assertEqual(choose(policy, zones, [], available), policy["subnet_zones"])
        with self.assertRaisesRegex(RuntimeError, "overlap"):
            choose(policy, zones, ["10.42.1.128/25"], available)
        with self.assertRaisesRegex(RuntimeError, "insufficient quotas"):
            choose(policy, zones, [], {})

    def test_repeated_addresses_persist_but_cleanup_leaves_user_materials(self):
        with TemporaryDirectory(dir=ROOT) as temporary:
            workspace = Path(temporary).resolve()
            local = workspace / ".lab/runtime"
            local.mkdir(parents=True)
            local_materials = workspace / ".local"
            local_materials.mkdir()
            (local_materials / "lecture.pdf").touch()
            lab = Lab(local, workspace=workspace)
            cloud = Mock(cloud="cloud", folder="folder")
            cloud.public_ipv4.side_effect = ["1.1.1.1", "8.8.8.8"]
            lab.tf.probe_id = Mock(return_value="probe")
            state = {"admin_cidrs": []}
            lab.confirm_address(cloud, state, ["9.9.9.9/32"])
            self.assertEqual(state["admin_cidrs"], ["9.9.9.9/32", "1.1.1.1/32"])
            lab.clean()
            self.assertTrue((local_materials / "lecture.pdf").exists())
            state = {"admin_cidrs": []}
            lab.confirm_address(cloud, state, ["9.9.9.9/32"])
            self.assertEqual(state["admin_cidrs"], ["9.9.9.9/32", "1.1.1.1/32", "8.8.8.8/32"])

    def test_terraform_rejects_other_instances_in_access_binding(self):
        with TemporaryDirectory(dir=ROOT) as temporary:
            tf = Terraform(Path(temporary))
            context = {"name": "otus-dz09-test", "folder": "folder"}
            attrs = {"name": "otus-dz09-test-verify", "folder_id": "folder", "id": "account"}
            account = {"mode": "managed", "type": "yandex_iam_service_account",
                       "instances": [{"attributes": attrs}]}
            binding = {"mode": "managed", "type": "yandex_compute_instance_iam_binding",
                       "instances": [{"attributes": {
                           "instance_id": "other", "role": "compute.operator",
                           "members": ["serviceAccount:account"],
                       }}]}
            save(tf.state, {"lineage": "test", "resources": [account, binding]})
            with self.assertRaisesRegex(RuntimeError, "Unexpected VM access"):
                tf.resources(context)

    def test_rejects_public_subnets_wider_than_a_host(self):
        with self.assertRaises(ValueError):
            admin_access(["8.8.8.0/24"])

    def test_portal_has_nine_complete_materials_and_dz09_article(self):
        path = ROOT / "portal/import-content.py"
        spec = importlib.util.spec_from_file_location("portal_import", path)
        importer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(importer)
        materials = importer.materials(ROOT / "portal/materials.xml")
        self.assertEqual(len(materials), 9)
        dz09 = next(item for item in materials if item["lesson"] == "9")
        self.assertEqual(dz09["slug"], "consul-dns")
        self.assertIn("Повышенная часть", dz09["body"])
