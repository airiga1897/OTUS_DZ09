"""Yandex Cloud inventory, authenticated IP probe and placement."""
from collections import defaultdict
from ipaddress import IPv4Network, ip_network
from pathlib import Path
import json
import os
import re
import time
from uuid import uuid4
from ipaddress import IPv4Address
from shutil import which
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GIB = 1024**3
COMPUTE = "https://compute.api.cloud.yandex.net/compute/v1"
VPC = "https://vpc.api.cloud.yandex.net/vpc/v1"
QUOTA = "https://quota-manager.api.cloud.yandex.net/quota-manager/v1"
IAM_RESOURCES = "https://resource-manager.api.cloud.yandex.net/resource-manager/v1"
FUNCTIONS = "https://serverless-functions.api.cloud.yandex.net/functions/v1"
IAM = "https://iam.api.cloud.yandex.net/iam/v1"
TYPES = {
    "yandex_function": (FUNCTIONS, "functions"),
    "yandex_compute_instance": (COMPUTE, "instances"),
    "disk": (COMPUTE, "disks"),
    "yandex_vpc_network": (VPC, "networks"),
    "yandex_vpc_subnet": (VPC, "subnets"),
    "yandex_vpc_route_table": (VPC, "routeTables"),
    "yandex_vpc_gateway": (VPC, "gateways"),
    "yandex_vpc_security_group": (VPC, "securityGroups"),
    "yandex_iam_service_account": (IAM, "serviceAccounts"),
}
IAM_BINDING = "yandex_compute_instance_iam_binding"


def profile_value(cli, config, *args, pattern):
    label = " ".join(args)
    hint = "Run `yc.exe --config PATH_TO_CLONE/.tools/yc-config.yaml init` as shown in README.md."
    try:
        result = subprocess.run([cli, "--config", str(config), *args], check=True, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"yc {label} failed. {hint}") from error
    value = result.stdout.strip()
    if not re.fullmatch(pattern, value):
        raise RuntimeError(f"yc {label} returned an invalid value. {hint}")
    return value


class Cloud:
    def __init__(self, settings=None):
        settings = os.environ if settings is None else settings
        credentials = [settings.get(key, "") for key in ("YC_CLOUD_ID", "YC_FOLDER_ID", "YC_TOKEN")]
        if any(credentials) or settings is not os.environ:
            if not all(credentials) or not all(re.fullmatch(r"[a-z0-9]+", value)
                                               for value in credentials[:2]) or not re.fullmatch(r"\S+", credentials[2]):
                raise ValueError("Set YC_CLOUD_ID, YC_FOLDER_ID and YC_TOKEN together.")
            self.cloud, self.folder, self.token = credentials
            return
        tools = Path(__file__).resolve().parents[1] / ".tools"
        bundled = tools / "bin/yc.exe"
        cli = str(bundled) if bundled.is_file() else which("yc")
        if not cli:
            raise RuntimeError("Install yc in PATH or place yc.exe in .tools/bin.")

        config = tools / "yc-config.yaml"
        self.cloud = profile_value(cli, config, "config", "get", "cloud-id", pattern=r"[a-z0-9]+")
        self.folder = profile_value(cli, config, "config", "get", "folder-id", pattern=r"[a-z0-9]+")
        self.token = profile_value(cli, config, "iam", "create-token", "--no-browser", pattern=r"\S+")

    def get(self, base, path, *, missing_ok=False, **query):
        url = f"{base}/{path}?{urlencode(query)}"
        request = Request(url, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise RuntimeError(f"YC API {path}: HTTP {error.code}") from error
        except (URLError, TimeoutError) as error:
            raise RuntimeError(f"YC API {path}: connection failed") from error

    def pages(self, base, path, key, **query):
        while True:
            page = self.get(base, path, pageSize=100, **query)
            if not isinstance(page, dict) or set(page) - {key, "nextPageToken", "resource"}:
                raise RuntimeError(f"YC API returned an unexpected list response for {path}.")
            yield from page.get(key, [])
            token = page.get("nextPageToken")
            if not token:
                return
            query["pageToken"] = token

    def resources(self, kind):
        base, path = TYPES[kind]
        return self.pages(base, path, path, folderId=self.folder)

    def lab_names(self):
        names = set()
        for kind in ("yandex_vpc_network", "yandex_function"):
            for item in self.resources(kind):
                match = re.fullmatch(r"(otus-dz09-[0-9a-f]{16})(?:-ip-probe)?", item["name"])
                if match:
                    names.add(match[1])
        return sorted(names)

    def public_ipv4(self, function_id):
        if not re.fullmatch(r"[a-z0-9]+", function_id):
            raise ValueError("Некорректный ID функции YC.")
        nonce = uuid4().hex
        request = Request(
            f"https://functions.yandexcloud.net/{function_id}?nonce={nonce}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        for attempt in range(3):
            try:
                with urlopen(request, timeout=20) as response:
                    result = json.loads(response.read(4096))
                if result.get("nonce") != nonce:
                    raise ValueError("Ответ функции YC не соответствует текущему запросу.")
                address = IPv4Address(result["ip"])
                if not address.is_global or address.is_multicast:
                    raise ValueError("Функция YC вернула непубличный IPv4.")
                return str(address)
            except HTTPError as error:
                error.close()
                if error.code not in (429, 502, 503, 504):
                    raise RuntimeError(f"Функция определения IP YC: HTTP {error.code}.") from error
            except (URLError, TimeoutError):
                pass
            if attempt < 2:
                time.sleep(2)
        raise RuntimeError("Не удалось получить IP через YC. Повторите deploy; state сохранён.")

    def inventory(self, prefix):
        return {
            (kind, item["id"])
            for kind in TYPES
            for item in self.resources(kind)
            if item["name"] == prefix or item["name"].startswith(prefix + "-")
        }

    def confirm_folder(self):
        folder = self.get(IAM_RESOURCES, f"folders/{self.folder}")
        if folder["cloudId"] != self.cloud or folder["status"] != "ACTIVE":
            raise RuntimeError("The YC folder is unavailable or belongs to another cloud.")

    def confirm_ownership(self, prefix, owned, found):
        for kind, identifier in owned - found:
            base, path = TYPES[kind]
            item = self.get(base, f"{path}/{identifier}", missing_ok=True)
            if item is not None:
                raise RuntimeError(f"YC resource {kind}/{identifier} no longer matches {prefix}.")

    def quota(self):
        result = {}
        for service in ("compute", "vpc", "iam", "serverless-functions"):
            for quota in self.pages(
                QUOTA, "quotaLimits", "quotaLimits", service=service,
                **{"resource.id": self.cloud, "resource.type": "resource-manager.cloud"},
            ):
                result[quota["quotaId"]] = int(quota["limit"]) - int(quota.get("usage", 0))
        return result

    def catalog(self, policy):
        check_platform(policy)
        image = self.get(COMPUTE, "images:latestByFamily", folderId="standard-images",
                         family=policy["image_family"])
        if image.get("status") != "READY" or image.get("architecture") not in (None, "X86_64"):
            raise RuntimeError("The selected Ubuntu x86-64 image is unavailable.")
        disk = self.get(COMPUTE, f'diskTypes/{policy["disk_type"]}')
        zones = {zone["id"] for zone in self.pages(COMPUTE, "zones", "zones")
                 if zone["status"] == "UP"}
        used = [cidr for subnet in self.resources("yandex_vpc_subnet")
                for cidr in subnet["v4CidrBlocks"]]
        disk_zones = disk.get("zoneIds")
        if disk.get("id") != policy["disk_type"]:
            raise RuntimeError("YC returned an unexpected disk type.")
        if disk_zones is None and disk["id"] in {"network-hdd", "network-ssd"}:
            # Standard network disks may omit zoneIds. Quota Manager still
            # verifies availability of the requested disk size per zone.
            disk_zones = zones
        if not isinstance(disk_zones, (list, set)):
            raise RuntimeError("YC did not confirm the disk type's placement.")
        return image, zones & set(disk_zones), used


def choose(policy, available_zones, used, free):
    occupied = [ip_network(cidr) for cidr in used]
    networks = [ip_network(cidr) for cidr in policy["subnets"].values()]
    if (not all(isinstance(cidr, IPv4Network) and cidr.prefixlen == 24 for cidr in networks)
            or len(set(networks)) != len(networks)):
        raise ValueError("Placement requires distinct IPv4 /24 networks.")
    placement = policy["subnet_zones"]
    if placement.keys() != policy["subnets"].keys() or set(placement.values()) - available_zones:
        raise RuntimeError("Configured zone or disk type unavailable.")
    if any(network.overlaps(other) for network in networks for other in occupied):
        raise RuntimeError("Configured subnets overlap existing subnets.")
    totals = defaultdict(int)
    missing = []
    for key, count in requirements(policy).items():
        prefix, _, suffix = key.rpartition(".")
        if suffix in placement.values() and prefix in free:
            totals[prefix] += count
        if free.get(key, count if suffix in placement.values() and prefix in free else 0) < count:
            missing.append(key)
    missing.extend(key for key, count in totals.items() if free[key] < count)
    if missing:
        raise RuntimeError("Missing or insufficient quotas: " + ", ".join(missing))
    return placement


def requirements(policy):
    needs = defaultdict(int, {
        "compute.instances.count": len(policy["nodes"]), "compute.disks.count": len(policy["nodes"]),
        "vpc.networks.count": 1, "vpc.subnets.count": len(policy["subnets"]),
        "vpc.routeTables.count": 1, "vpc.securityGroups.count": 3,
        "vpc.externalAddresses.count": 1, "vpc.staticRoutes.count": 1,
        "vpc.gateways.count": 1, "vpc.sharedEgressGateways.count": 1,
        "iam.serviceAccounts.count": 1, "iam.accessBindings.count": 1,
        "serverless.functions.count": 1,
    })
    disk_quota = {"network-hdd": "hddDisks", "network-ssd": "ssdDisks"}[policy["disk_type"]]
    for node in policy["nodes"].values():
        zone = policy["subnet_zones"][node["subnet"]]
        needs[f"compute.instanceCores.count.{zone}"] += node["cores"]
        needs[f"compute.instanceMemory.size.{zone}"] += node["memory"] * GIB
        needs[f"compute.{disk_quota}.size.{zone}"] += node["disk"] * GIB
    return {key: count for key, count in needs.items() if count}


def check_platform(policy):
    if policy["platform"] != "standard-v3":
        raise ValueError("This lab supports the YC standard-v3 platform.")
    for name, node in policy["nodes"].items():
        cores = node["cores"]
        if (cores not in {2, 4, 6, 8, 10, 12, 14, 16, 20, 24, 28, 32} or
                node["memory"] % cores != 0 or not 1 <= node["memory"] / cores <= 16):
            raise ValueError(f"{name}: unsupported vCPU/RAM pair for the configured platform.")
