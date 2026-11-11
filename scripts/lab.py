"""Создание, настройка, проверка и удаление стенда ДЗ09."""
from argparse import ArgumentParser
from ipaddress import ip_address, ip_network
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from uuid import uuid4

from remote import Remote
from runtime import ROOT, Terraform, admin_access, locked, protect, read, run, save, wp_cli
from yc import Cloud, GIB, choose

LOG = logging.getLogger(__name__)


def dotenv(path):
    if not path.is_file() or path.is_symlink():
        raise ValueError("Для --env-file нужен обычный файл .env в корне ДЗ09.")
    if os.name == "nt":
        run("powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$a=Get-Acl -LiteralPath '.env'; "
            "$id=[Security.Principal.WindowsIdentity]::GetCurrent().User; "
            "if ($a.GetOwner([Security.Principal.SecurityIdentifier]) -ne $id -or "
            "@($a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | "
            "Where-Object { $_.AccessControlType -eq 'Allow' -and $_.IdentityReference -ne $id }).Count) "
            "{ throw 'Only the current user may own and read .env' }")
    else:
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Файл .env должен принадлежать текущему пользователю с правами 0600.")
    data = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (not separator or key in data
                or key not in {"YC_CLOUD_ID", "YC_FOLDER_ID", "YC_TOKEN"}):
            raise ValueError("В .env допустимы только строки KEY=VALUE для реквизитов YC.")
        data[key] = value.strip()
    return data


class Lab:
    def __init__(self, local, *, plugin_dir=None, workspace=ROOT):
        self.local = local
        self.workspace = workspace.resolve()
        self.context = local / "context.json"
        self.variables = local / "terraform.tfvars.json"
        self.tf = Terraform(local, plugin_dir=plugin_dir)
        self.policy = read(ROOT / "config/lab.json")

    def saved(self, cloud=None):
        if not self.context.is_file():
            raise RuntimeError("Нет контекста стенда. Для восстановления нужен исходный клон.")
        context = read(self.context)
        if not re.fullmatch(r"otus-dz09-[0-9a-f]{16}", context["name"]):
            raise RuntimeError("Неверная область стенда в контексте.")
        if cloud and (context["cloud"], context["folder"]) != (cloud.cloud, cloud.folder):
            raise RuntimeError("Реквизиты указывают на другое облако или каталог.")
        return context

    def configuration(self):
        path = ROOT / "config/lab.local.json"
        if not path.is_file():
            raise RuntimeError("Заполните config/lab.local.json по примеру.")
        settings = read(path)
        domain = settings["portal_name"].lower().rstrip(".")
        if len(domain) > 253 or len(domain.split(".")) < 3 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in domain.split(".")
        ) or domain == "portal.service.consul" or domain.endswith(".consul"):
            raise ValueError("portal_name должен быть отдельным DNS-именем в домене оператора.")
        return domain, admin_access(settings.get("admin_cidrs", []))

    def prepare_context(self, cloud, domain):
        if self.tf.state.exists() or self.variables.exists() or cloud.lab_names():
            raise RuntimeError("Обнаружен стенд без локального контекста. Используйте исходный клон.")
        image, zones, used = cloud.catalog(self.policy)
        if any(node["disk"] * GIB < int(image["minDiskSize"])
               for node in self.policy["nodes"].values()):
            raise RuntimeError("Размер диска меньше минимума образа.")
        subnet_zones = choose(self.policy, zones, used, cloud.quota())
        context = {
            "cloud": cloud.cloud, "folder": cloud.folder, "name": "otus-dz09-" + uuid4().hex[:16],
            "portal_name": domain, "subnet_zones": subnet_zones, "image_id": image["id"], "admin_cidrs": [],
            **{key: self.policy[key] for key in ("nodes", "subnets", "platform", "disk_type")},
        }
        if cloud.inventory(context["name"]):
            raise RuntimeError("Область нового стенда уже занята.")
        save(self.context, context)
        return context

    def key(self, name):
        private = self.local / name
        if not private.exists():
            run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private))
        return private.with_suffix(".pub").read_text(encoding="ascii").strip()

    def prepare_variables(self, context):
        save(self.variables, {
            **{key: context[key] for key in ("name", "subnet_zones", "nodes", "subnets", "admin_cidrs",
                                           "platform", "disk_type", "image_id")},
            "cloud_id": context["cloud"], "folder_id": context["folder"],
            "operator_key": self.key("operator"), "ansible_key": self.key("ansible"),
            "ip_probe_zip": self.tf.prepare_probe(),
        })

    def check_inventory(self, cloud, context):
        owned = self.tf.resources(context)
        if cloud.inventory(context["name"]) != owned:
            raise RuntimeError("Ресурсы YC и Terraform state расходятся. Данные сохранены.")
        return owned

    def confirm_address(self, cloud, context, configured):
        address = cloud.public_ipv4(self.tf.probe_id(context))
        history_path = self.local.parent / "admin-history.json"
        history = read(history_path) if history_path.exists() else {}
        scope = f"{cloud.cloud}/{cloud.folder}"
        history[scope] = admin_access(history.get(scope, []), address)
        save(history_path, history)
        context["admin_cidrs"] = admin_access([*context["admin_cidrs"], *configured, *history[scope]])
        save(self.context, context)

    def deploy(self, cloud):
        wp_cli()
        domain, configured = self.configuration()
        cloud.confirm_folder()
        context = self.saved(cloud) if self.context.exists() else self.prepare_context(cloud, domain)
        if context["portal_name"] != domain:
            raise RuntimeError("Имя существующего портала изменилось в конфигурации.")
        owned = self.check_inventory(cloud, context)
        if context.get("lineage") and not owned:
            raise RuntimeError("Утрачено состояние ранее созданного стенда.")
        self.prepare_variables(context)
        self.tf.init()
        if not any(kind == "yandex_function" for kind, _ in owned):
            self.tf.plan_apply(token=cloud.token, probe_only=True)
            context["lineage"] = read(self.tf.state)["lineage"]
            save(self.context, context)
            self.check_inventory(cloud, context)
        self.confirm_address(cloud, context, configured)
        self.prepare_variables(context)
        self.tf.plan_apply(token=cloud.token)
        self.check_inventory(cloud, context)
        self.remote().configure()

    def remote(self):
        context = self.saved()
        nodes = self.tf.nodes(context)
        for name, node in nodes.items():
            expected = context["nodes"][name]
            network = ip_network(context["subnets"][expected["subnet"]])
            if (ip_address(node["private"]) != network[expected["host"]]
                    or node["zone"] != context["subnet_zones"][expected["subnet"]]
                    or bool(node["public"]) != (name == "client")):
                raise RuntimeError(f"Фактическое размещение {name} отличается от ожидаемого.")
        return Remote(self.local, context, nodes)

    def destroy(self, cloud):
        cloud.confirm_folder()
        context = self.saved(cloud)
        owned = self.tf.resources(context)
        found = cloud.inventory(context["name"])
        if found - owned:
            raise RuntimeError("Обнаружены ресурсы вне state. Данные восстановления сохранены.")
        cloud.confirm_ownership(context["name"], owned, found)
        if owned:
            if not self.variables.is_file():
                raise RuntimeError("Восстановите переменные Terraform из исходного клона.")
            self.tf.init()
            self.tf.plan_apply(token=cloud.token, destroy=True)
        for attempt in range(10):
            if not self.tf.resources(context) and not cloud.inventory(context["name"]):
                self.clean()
                LOG.info("Удаление подтверждено в Terraform state и YC.")
                return
            if attempt < 9:
                time.sleep(3)
        raise RuntimeError("Удаление не подтверждено. Данные восстановления сохранены.")

    def clean(self):
        if (self.local != self.local.resolve() or self.local.parent != self.workspace / ".lab"
                or self.local.name not in {"runtime", "test"}):
            raise RuntimeError("Очистка разрешена только в рабочем каталоге стенда.")
        entries = [entry for entry in self.local.iterdir() if entry.name != "operation.lock"]
        if any(entry.is_symlink() or (os.name == "nt" and entry.is_junction()) for entry in entries):
            raise RuntimeError("Обнаружена ссылка в рабочем каталоге. Очистка остановлена.")
        for entry in entries:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("deploy", "verify", "open", "destroy", "test-cycle"))
    parser.add_argument("--env-file", action="store_true", help="явно прочитать корневой .env")
    parser.add_argument("--plugin-dir", type=lambda path: os.path.abspath(path))
    parser.add_argument("--test-context", action="store_true", help="очистить незавершённый test-cycle")
    args = parser.parse_args()
    if args.test_context and args.command != "destroy":
        parser.error("--test-context применяется только с destroy")
    try:
        directory = ROOT / ".lab"
        protect(directory)
        with locked(directory):
            local = directory / ("test" if args.test_context or args.command == "test-cycle" else "runtime")
            protect(local)
            lab = Lab(local, plugin_dir=args.plugin_dir)
            if args.command in {"open", "verify"}:
                getattr(lab.remote(), args.command)()
                return 0
            cloud = Cloud(dotenv(ROOT / ".env") if args.env_file else None)
            if args.command == "test-cycle":
                if lab.context.exists() or lab.tf.state.exists():
                    raise RuntimeError("Сначала очистите незавершённый тестовый стенд.")
                try:
                    lab.deploy(cloud)
                    lab.remote().verify()
                finally:
                    if lab.context.exists():
                        lab.destroy(cloud)
            else:
                getattr(lab, args.command)(cloud)
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        LOG.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
