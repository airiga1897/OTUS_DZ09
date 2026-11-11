#!/usr/bin/python3
"""Проверка Consul DNS, BIND и портала с клиентской ВМ."""
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import re
import signal
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sync_dns import CONSUL, dns_addresses, local_json

LOG = logging.getLogger(__name__)
CONFIG = Path("/etc/otus-dz09")
COMPUTE = "https://compute.api.cloud.yandex.net/compute/v1/instances"
METADATA = "http://169.254.169.254/computeMetadata/v1/instance"
ARTICLE = "/articles/consul-dns/"
ARTICLE_TITLE = "ДЗ09. Consul и DNS".encode()


def wait_for(description, action, *, timeout=120):
    end = time.monotonic() + timeout
    while True:
        try:
            return action()
        except (KeyError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            if time.monotonic() >= end:
                raise RuntimeError(f"{description}: {error}") from error
        time.sleep(2)


@contextmanager
def fault(restore):
    try:
        yield
    finally:
        try:
            restore()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            LOG.error("Не удалось восстановить web-b. Требуется ручное восстановление.")
            raise


class Check:
    def __init__(self, settings):
        self.name = settings["portal"]
        self.nodes = settings["nodes"]
        self.instances = {node: self.nodes[node]["private"] for node in ("web-a", "web-b", "web-c")}
        self.target = self.nodes["web-b"]["id"]
        self.client = self.nodes["client"]["id"]
        self.folder = settings["folder"]
        self.lab_name = settings["name"]
        if not re.fullmatch(r"[a-z0-9]+", self.target) or self.target == self.client:
            raise ValueError("Неверный идентификатор целевой ВМ.")

    def ssh(self, *command):
        subprocess.run(
            ["ssh", "-i", str(CONFIG / "ansible-key"), "-o", "IdentitiesOnly=yes",
             "-o", f"UserKnownHostsFile={CONFIG / 'known_hosts'}", "-o", "StrictHostKeyChecking=yes",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             f"ubuntu@{self.instances['web-b']}", *command],
            check=True, timeout=30,
        )

    def compute(self, action=None):
        token = local_json(f"{METADATA}/service-accounts/default/token")["access_token"]
        url = f"{COMPUTE}/{self.target}" + (f":{action}" if action else "")
        request = Request(
            url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            data=b"{}" if action else None,
        )
        try:
            with urlopen(request, timeout=15) as response:
                result = json.load(response)
        except HTTPError as error:
            try:
                reason = json.load(error).get("message", "")
            except (ValueError, AttributeError):
                reason = ""
            raise RuntimeError(f"YC Compute {action or 'get'}: HTTP {error.code} {reason}".strip()) from error
        if action:
            if result.get("error"):
                raise RuntimeError(f"YC Compute rejected {action}.")
        elif (result.get("id"), result.get("name"), result.get("folderId")) != (
            self.target, self.lab_name + "-web-b", self.folder
        ):
            raise RuntimeError("Целевая ВМ не соответствует инвентарю стенда.")
        return result

    def power(self, expected):
        status = self.compute().get("status")
        if status != expected:
            raise RuntimeError(f"web-b: статус {status}, ожидается {expected}")

    def request_start(self):
        status = self.compute().get("status")
        if status in {"STARTING", "RUNNING"}:
            return
        if status != "STOPPED":
            raise RuntimeError(f"web-b: ожидание завершения остановки ({status})")
        self.compute("start")

    def restore_vm(self):
        wait_for("Запрос запуска web-b", self.request_start, timeout=60)
        wait_for("Запуск web-b", lambda: self.power("RUNNING"), timeout=300)

    def cluster(self):
        members = local_json(f"{CONSUL}/agent/members")
        active = {item["Name"]: item["Tags"]["role"] for item in members if item["Status"] == 1}
        if active != {"client": "node", **{name: "consul" for name in self.instances}}:
            raise RuntimeError(f"Неожиданный состав Consul: {active}")
        servers = local_json(f"{CONSUL}/operator/raft/configuration")["Servers"]
        if (len(servers) != 3 or {peer["Node"] for peer in servers if peer["Voter"]} != self.instances.keys()
                or sum(peer["Leader"] for peer in servers) != 1):
            raise RuntimeError("В Consul ожидаются три голосующих сервера и один лидер.")

    def http(self, address, expected_nodes, path="/"):
        result = subprocess.run(
            ["curl", "--silent", "--show-error", "--noproxy", "*", "--max-time", "8",
             "--cacert", str(CONFIG / "ca.crt"), "--resolve", f"{self.name}:443:{address}",
             "--write-out", "\n%{http_code} %header{x-backend-node}", f"https://{self.name}{path}"],
            capture_output=True, check=True, timeout=10,
        )
        body, _, trailer = result.stdout.rpartition(b"\n")
        status, _, backend = trailer.partition(b" ")
        if status != b"200":
            raise RuntimeError(f"HTTPS {address}{path}: HTTP {status.decode(errors='replace')}")
        node = backend.decode("ascii")
        if node not in expected_nodes:
            raise RuntimeError(f"HTTPS {address}{path}: неожиданный X-Backend-Node {node!r}")
        if path == "/":
            if body.count(b'class="lesson-card"') != 9 or ARTICLE.encode() not in body:
                raise RuntimeError(f"HTTPS {address}: ожидаются девять карточек и ссылка на ДЗ09.")
        elif ARTICLE_TITLE not in body:
            raise RuntimeError(f"HTTPS {address}: статья ДЗ09 отсутствует.")
        return node

    def chain(self, expected_nodes):
        expected = {self.instances[node] for node in expected_nodes}
        consul = dns_addresses("portal.service.consul", 8600)
        bind = dns_addresses(self.name, 53, authoritative=True)
        if consul != expected or bind != expected:
            raise RuntimeError(
                f"Consul DNS {sorted(consul)}, BIND {sorted(bind)}, ожидается {sorted(expected)}"
            )
        for node in sorted(expected_nodes):
            self.http(self.instances[node], {node})
            self.http(self.instances[node], {node}, ARTICLE)
        actual = self.http("127.0.0.1", expected_nodes)
        self.http("127.0.0.1", expected_nodes, ARTICLE)
        LOG.info("Consul DNS и BIND: %s. Через Nginx ответил %s.", sorted(expected), actual)

    def verify(self):
        full = set(self.instances)
        reduced = full - {"web-b"}
        wait_for("Кворум Consul", self.cluster)
        wait_for("Исходная проверка", lambda: self.chain(full))
        self.ssh("sudo", "systemctl", "is-active", "nginx")
        LOG.info("Проверка остановки Nginx на web-b.")
        with fault(lambda: self.ssh("sudo", "systemctl", "start", "nginx")):
            self.ssh("sudo", "systemctl", "stop", "nginx")
            wait_for("Исключение отказавшего веб-сервера", lambda: self.chain(reduced))
        wait_for("Восстановление веб-сервера", lambda: self.chain(full))

        LOG.info("Проверка отключения ВМ web-b.")
        self.power("RUNNING")
        with fault(self.restore_vm):
            self.compute("stop")
            wait_for("Остановка web-b", lambda: self.power("STOPPED"), timeout=300)
            wait_for("Исключение выключенной ВМ", lambda: self.chain(reduced))
        wait_for("Восстановление ВМ и портала", lambda: self.chain(full), timeout=300)
        wait_for("Восстановление кворума", self.cluster)


def interrupt(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")


def main():
    import fcntl

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)
    try:
        with Path("/home/ubuntu/.otus-dz09/deploy.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            settings = json.loads((CONFIG / "verify.json").read_text())
            Check(settings).verify()
    except (KeyboardInterrupt, KeyError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        LOG.error("Проверка не пройдена: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
