"""Local files, command execution and Terraform ownership checks."""
from contextlib import contextmanager
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from shutil import which
from hashlib import file_digest
import csv
import json
import os
import subprocess
from zipfile import ZipFile, ZipInfo

from yc import IAM_BINDING, TYPES

ROOT = Path(__file__).resolve().parents[1]


def wp_cli():
    config = read(ROOT / "config/wp-cli.json")
    path = ROOT / ".tools/wp-cli" / f"wp-cli-{config['version']}.phar"
    if not path.is_file():
        raise RuntimeError("WP-CLI отсутствует. Выполните .\\scripts\\setup.ps1.")
    with path.open("rb") as source:
        digest = file_digest(source, "sha512").hexdigest()
    if digest != config["sha512"]:
        raise RuntimeError("Контрольная сумма WP-CLI не совпадает. Выполните .\\scripts\\setup.ps1.")
    return path


def run(*args, capture=False, cwd=ROOT, timeout=1200, env=None):
    return subprocess.run(
        args, cwd=cwd, check=True, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE if capture else None, timeout=timeout, env=env,
    ).stdout


def save(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def admin_access(configured, address=None):
    networks = [IPv4Network(cidr) for cidr in configured]
    if any(net.prefixlen != 32 or not net.network_address.is_global
           or net.network_address.is_multicast for net in networks):
        raise ValueError("admin_cidrs должны содержать только публичные IPv4 /32.")
    result = [str(net) for net in networks]
    if address is not None:
        ip = IPv4Address(address)
        if not ip.is_global or ip.is_multicast:
            raise ValueError("YC вернул непубличный IPv4.")
        result.append(f"{ip}/32")
    return list(dict.fromkeys(result))


def protect(directory):
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or (os.name == "nt" and directory.is_junction()):
        raise RuntimeError("The local data directory must not be a link.")
    if os.name == "nt":
        row = next(csv.reader([run("whoami", "/user", "/fo", "csv", "/nh", capture=True).strip()]))
        run("icacls", str(directory), "/inheritance:r", "/grant:r", f"*{row[1]}:(OI)(CI)F",
            capture=True)
    else:
        directory.chmod(0o700)


@contextmanager
def locked(directory):
    protect(directory)
    with (directory / "operation.lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Terraform:
    def __init__(self, local, *, plugin_dir=None):
        self.local = local
        self.state = local / "terraform.tfstate"
        self.plan = local / "saved.tfplan"
        self.plugin_dir = plugin_dir

    def call(self, *args, capture=False, token=None):
        env = dict(os.environ, TF_DATA_DIR=str(self.local / "terraform-data"),
                   TF_IN_AUTOMATION="true", TF_INPUT="false", TF_WORKSPACE="default")
        env.setdefault("TF_CLI_CONFIG_FILE", str(ROOT / "terraform/terraform.rc"))
        if token is not None:
            env["YC_TOKEN"] = token
        bundled = ROOT / ".tools/bin/terraform.exe"
        cli = which("terraform") or (str(bundled) if os.name == "nt" and bundled.is_file() else "terraform")
        return run(cli, f"-chdir={ROOT / 'terraform'}", *args, capture=capture, env=env)

    def init(self):
        source = [f"-plugin-dir={self.plugin_dir}"] if self.plugin_dir is not None else []
        self.call("init", "-input=false", "-lockfile=readonly", "-reconfigure",
                  f"-backend-config=path={self.state.resolve()}", *source)

    def prepare_probe(self):
        path = self.local / "ip-probe.zip"
        with ZipFile(path, "w") as archive:
            archive.writestr(ZipInfo("index.py"), (ROOT / "functions/ip_probe.py").read_bytes())
        return path.resolve().as_posix()

    def probe_id(self, context):
        owned = self.resources(context)
        identifiers = [identifier for kind, identifier in owned if kind == "yandex_function"]
        if len(identifiers) != 1:
            raise RuntimeError("В state должна быть одна функция определения IP.")
        return identifiers[0]

    def plan_apply(self, *, token, destroy=False, probe_only=False):
        if destroy and probe_only:
            raise ValueError("Нельзя совместить удаление и подготовку функции.")
        mode = ["-destroy"] if destroy else []
        if probe_only:
            mode.append("-target=yandex_function.ip_probe")
        self.call("plan", *mode, "-input=false",
                  f"-var-file={self.local / 'terraform.tfvars.json'}", f"-out={self.plan}", token=token)
        data = json.loads(self.call("show", "-json", str(self.plan), capture=True))
        allowed = {("no-op",), ("delete",) if destroy else ("create",)}
        for change in data.get("resource_changes", []):
            actions = tuple(change["change"]["actions"])
            if probe_only and actions != ("no-op",) and change["address"] != "yandex_function.ip_probe":
                raise RuntimeError("Подготовка IP-функции затрагивает другие ресурсы.")
            if (change["type"] in TYPES or change["type"] == IAM_BINDING) and actions in allowed:
                continue
            if (not destroy and change["address"] == "yandex_vpc_security_group.client"
                    and self.client_ingress_update(
                        change, read(self.local / "terraform.tfvars.json")["admin_cidrs"])):
                continue
            if not destroy and not probe_only and self.cloud_init_metadata_update(change):
                continue
            raise RuntimeError(f"Refusing unexpected Terraform action for {change['address']}: "
                               f"{', '.join(actions)}. Nothing was applied.")
        self.call("apply", "-input=false", str(self.plan), token=token)

    @staticmethod
    def cloud_init_metadata_update(resource):
        if (resource["type"] != "yandex_compute_instance"
                or resource.get("name") != "lab"
                or resource["change"]["actions"] != ["update"]):
            return False
        change = resource["change"]
        before, after = change["before"], change["after"]
        old, new = before.get("metadata_options", []), after.get("metadata_options", [])
        if len(old) != 1 or len(new) != 1 or old[0].get("aws_v1_http_endpoint") != 2:
            return False
        return (dict(old[0], aws_v1_http_endpoint=1) == new[0]
                and all(before.get(key) == after.get(key)
                        for key in before.keys() | after.keys() if key != "metadata_options"))

    @staticmethod
    def client_ingress_update(resource, approved_cidrs):
        def has_unknown(value):
            if isinstance(value, dict):
                return any(has_unknown(item) for item in value.values())
            if isinstance(value, list):
                return any(has_unknown(item) for item in value)
            return value is not False

        change = resource["change"]
        if (resource["type"] != "yandex_vpc_security_group"
                or resource["address"] != "yandex_vpc_security_group.client"
                or change["actions"] != ["update"]):
            return False
        before, after = change["before"], change["after"]
        other_fields = before.keys() | after.keys()
        if (not all(before.get(key) == after.get(key) for key in other_fields - {"ingress"})
                or any(has_unknown(value) for key, value in change.get("after_unknown", {}).items()
                       if key != "ingress")):
            return False
        # YC recomputes rule IDs and labels when an ingress CIDR changes.
        rule_metadata = {"id", "labels"}
        unknown = change.get("after_unknown", {}).get("ingress", [])
        if not isinstance(unknown, list) or any(
            not isinstance(rule, dict) or any(
                has_unknown(value) for key, value in rule.items() if key not in rule_metadata
            )
            for rule in unknown
        ):
            return False

        def rules_by_settings(rules):
            result = {}
            for rule in rules:
                settings = {key: value for key, value in rule.items()
                            if key not in rule_metadata | {"v4_cidr_blocks"}}
                for field in ("description", "security_group_id", "predefined_target"):
                    settings[field] = settings.get(field) or ""
                key = json.dumps(settings, sort_keys=True)
                if key in result:
                    raise ValueError("Duplicate ingress settings")
                external = rule["protocol"] == "TCP" and rule["port"] in {22, 80, 443}
                cidrs = rule["v4_cidr_blocks"]
                result[key] = (external, set(admin_access(cidrs) if external else cidrs))
            return result

        try:
            approved = set(admin_access(approved_cidrs))
            old = rules_by_settings(before["ingress"])
            new = rules_by_settings(after["ingress"])
        except (KeyError, TypeError, ValueError):
            return False
        for key in new.keys() - old.keys():
            settings = json.loads(key)
            if (settings.get("port") != 80
                    or json.dumps(dict(settings, port=443), sort_keys=True) not in old
                    or new[key] != (True, approved)):
                return False
        return bool(old) and old.keys() <= new.keys() and all(
            old[key][1] <= new[key][1] <= approved if old[key][0] else old[key] == new[key]
            for key in old
        )

    def resources(self, context):
        if not self.state.exists():
            return set()
        document = read(self.state)
        if context.get("lineage") and document["lineage"] != context["lineage"]:
            raise RuntimeError("Terraform state lineage differs from the saved context.")
        owned = set()
        bindings = []
        instances = {}
        for resource in document["resources"]:
            kind = resource["type"]
            if resource["mode"] != "managed" or (kind not in TYPES and kind != IAM_BINDING):
                raise RuntimeError("Terraform state contains an unsupported resource.")
            for instance in resource["instances"]:
                record = instance["attributes"]
                if kind == IAM_BINDING:
                    bindings.append(record)
                    continue
                name = record["name"]
                prefix = context["name"]
                if name != prefix and not name.startswith(prefix + "-"):
                    raise RuntimeError("Terraform state contains a resource outside the lab.")
                if record.get("folder_id") != context["folder"]:
                    raise RuntimeError("Terraform state resource belongs to another YC folder.")
                owned.add((kind, record["id"]))
                if kind == "yandex_compute_instance":
                    instances[record["name"]] = record["id"]
                    disk_id = record["boot_disk"][0]["disk_id"]
                    owned.add(("disk", disk_id))
        accounts = [f"serviceAccount:{identifier}" for kind, identifier in owned
                    if kind == "yandex_iam_service_account"]
        if len(bindings) > 1 or any(
            binding["instance_id"] != instances.get(context["name"] + "-web-b")
            or binding["role"] != "compute.operator" or len(accounts) != 1
            or binding["members"] != accounts for binding in bindings
        ):
            raise RuntimeError("Unexpected VM access binding in Terraform state.")
        return owned

    def nodes(self, context):
        if not self.resources(context):
            raise RuntimeError("The lab has no managed infrastructure.")
        nodes = read(self.state)["outputs"]["nodes"]["value"]
        ids = self.resources(context)
        if set(nodes) != set(context["nodes"]) or any(
            ("yandex_compute_instance", node["id"]) not in ids for node in nodes.values()
        ):
            raise RuntimeError("Terraform outputs do not match the managed nodes.")
        return nodes
