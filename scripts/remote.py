"""Передача проекта и запуск Ansible на клиентской ВМ."""
from base64 import b64encode
from ipaddress import IPv4Address
from pathlib import Path
from tempfile import TemporaryDirectory
import re
import secrets
import ssl
import subprocess
import tarfile
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from runtime import ROOT, read, run, save, wp_cli


class Remote:
    def __init__(self, local, context, nodes):
        self.local = local
        self.context = context
        self.nodes = nodes
        self.address = str(IPv4Address(nodes["client"]["public"]))
        self.config = local / "ssh-config"
        self.ca = local / "ca.crt"
        known = (local / "known_hosts").as_posix()
        key = (local / "operator").as_posix()
        self.config.write_text(
            f"Host client\n HostName {self.address}\n User ubuntu\n"
            f' IdentityFile "{key}"\n UserKnownHostsFile "{known}"\n'
            " StrictHostKeyChecking accept-new\n IdentitiesOnly yes\n BatchMode yes\n"
            " ConnectTimeout 8\n ServerAliveInterval 15\n ServerAliveCountMax 3\n",
            encoding="utf-8",
        )

    def command(self, command, *, capture=False, timeout=900):
        return run("ssh", "-F", str(self.config), "client", command,
                   capture=capture, timeout=timeout)

    def wait(self):
        for attempt in range(30):
            try:
                self.command("true", capture=True, timeout=12)
                return
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt == 29:
                    raise RuntimeError("SSH до client недоступен. Проверьте маршрут и admin_cidrs.")
                time.sleep(5)

    def settings(self):
        path = self.local / "secrets.json"
        if not path.exists():
            data = {key: secrets.token_hex(24) for key in ("wordpress_admin", "wordpress_db")}
            data["consul_gossip_key"] = b64encode(secrets.token_bytes(32)).decode("ascii")
            data["bind_tsig"] = b64encode(secrets.token_bytes(32)).decode("ascii")
            save(path, data)
        return dict(read(path), portal_name=self.context["portal_name"], lab_nodes=self.nodes,
                    lab_name=self.context["name"], lab_folder=self.context["folder"])

    def bundle(self, path):
        settings = path.parent / "settings.json"
        save(settings, self.settings())
        with tarfile.open(path, "w:gz") as archive:
            for directory in ("ansible", "portal"):
                archive.add(ROOT / directory, arcname=directory,
                            filter=lambda item: None if "__pycache__" in Path(item.name).parts else item)
            for source, name in (
                (wp_cli(), "wp-cli.phar"),
                (ROOT / "scripts/bootstrap.sh", "bootstrap.sh"),
                (ROOT / "scripts/verify.py", "verify.py"),
                (ROOT / "config/stack.yml", "stack.yml"),
                (settings, "settings.json"),
                (self.local / "ansible", "ansible-key"),
            ):
                archive.add(source, arcname=name)

    def configure(self):
        self.wait()
        with TemporaryDirectory(dir=self.local) as temp:
            bundle = Path(temp) / "bundle.tar.gz"
            self.bundle(bundle)
            remote = self.command("mktemp -d /tmp/otus-dz09.XXXXXXXX", capture=True).strip()
            if not re.fullmatch(r"/tmp/otus-dz09\.[A-Za-z0-9]{8}", remote):
                raise RuntimeError("Неожиданный временный путь на client.")
            try:
                run("scp", "-F", str(self.config), str(bundle), f"client:{remote}/bundle.tar.gz")
                self.command(
                    f"umask 077; cd {remote} && tar -xzf bundle.tar.gz && "
                    "chmod 600 ansible-key settings.json && bash bootstrap.sh", timeout=3000)
            finally:
                self.command(f"rm -r -- {remote}")
        certificate = self.command("sudo cat /etc/otus-dz09/ca.crt", capture=True)
        ssl.PEM_cert_to_DER_cert(certificate)
        self.ca.write_text(certificate, encoding="ascii")

    def verify(self):
        self.command("sudo /usr/local/lib/otus-dz09/verify.py", timeout=2400)
        self.verify_http_preview()

    def verify_http_preview(self):
        base = f"http://{self.address}"
        opener = build_opener(ProxyHandler({}))
        article = "/articles/consul-dns/"
        for path, expected in (("/", f'href="{article}"'.encode()),
                               (article, "ДЗ09. Consul и DNS".encode()),
                               ("/wp-content/themes/otus/style.css", b"Theme Name: OTUS Lessons")):
            url = base + path
            with opener.open(url, timeout=10) as response:
                if response.geturl() != url or expected not in response.read():
                    raise RuntimeError(f"HTTP preview returned unexpected content for {path}.")
        for path, method, status in (("/wp-admin/", "GET", 404),
                                     ("/wp-login.php", "GET", 404),
                                     (article, "POST", 403)):
            try:
                with opener.open(Request(base + path, method=method), timeout=10):
                    raise RuntimeError(f"HTTP preview unexpectedly allowed {method} {path}.")
            except HTTPError as error:
                if error.code != status:
                    raise
        print("HTTP: главная, статья ДЗ09 и стили доступны по IP. Вход и POST закрыты.", flush=True)

    def open(self):
        if not self.ca.is_file():
            raise RuntimeError("Сертификат УЦ отсутствует. Завершите deploy.")
        name = self.context["portal_name"]
        print(f"Портал: http://{self.address}/")
        print(f"ДЗ09: http://{self.address}/articles/consul-dns/")
        print(f"curl.exe --ssl-revoke-best-effort --noproxy '*' --cacert \"{self.ca}\" "
              f"--resolve {name}:443:{self.address} https://{name}/articles/consul-dns/")
