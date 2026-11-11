#!/usr/bin/python3
"""Publish the healthy portal replicas in the private BIND zone."""
from ipaddress import IPv4Address
import json
import logging
from pathlib import Path
import re
import subprocess
from urllib.request import ProxyHandler, Request, build_opener

LOG = logging.getLogger(__name__)
LOCAL_HTTP = build_opener(ProxyHandler({}))
CONSUL = "http://127.0.0.1:8500/v1"


def local_json(url):
    request = Request(url, headers={"Metadata-Flavor": "Google"} if url.startswith("http://169.254.") else {})
    with LOCAL_HTTP.open(request, timeout=4) as response:
        if response.headers.get("X-Consul-Results-Filtered-By-ACLs", "").lower() == "true":
            raise ValueError("Consul returned an ACL-filtered response")
        if url.endswith("?passing=true&consistent") and response.headers.get(
            "X-Consul-Knownleader", ""
        ).lower() != "true":
            raise ValueError("Consul did not confirm a leader")
        return json.load(response)


def healthy_addresses(payload, expected):
    if not isinstance(payload, list):
        raise ValueError("Consul health response must be a list")
    addresses = set()
    for entry in payload:
        service = entry["Service"]
        address = str(IPv4Address(service["Address"]))
        checks = entry["Checks"]
        if (expected.get(service["ID"]) != address or service["Service"] != "portal"
                or service["Port"] != 443 or address in addresses or not isinstance(checks, list)
                or not checks or any(check["Status"] != "passing" for check in checks)
                or not any(check.get("ServiceID") == service["ID"] for check in checks)):
            raise ValueError("Consul returned an unexpected or incomplete portal record")
        addresses.add(address)
    return addresses


def dns_addresses(name, port, *, authoritative=False):
    fqdn = name.rstrip(".").lower() + "."
    result = subprocess.run(
        ["dig", "@127.0.0.1", "-p", str(port), fqdn, "A", "+noall", "+comments",
         "+answer", "+time=2", "+tries=1"], capture_output=True, text=True, timeout=5,
    )
    status = re.search(r"\bstatus: ([A-Z]+)", result.stdout)
    flags = re.search(r";; flags: ([^;]+);", result.stdout)
    if (result.returncode or not status or status[1] not in {"NOERROR", "NXDOMAIN"}
            or authoritative and (not flags or "aa" not in flags[1].split())):
        raise RuntimeError(f"DNS {port} returned no usable answer for {name}")
    addresses = set()
    for line in result.stdout.splitlines():
        if line and not line.startswith(";"):
            owner, _, klass, kind, value = line.split()
            if (owner.lower(), klass, kind) != (fqdn, "IN", "A"):
                raise ValueError(f"Unexpected DNS record for {name}")
            addresses.add(str(IPv4Address(value)))
    return addresses


def update_records(name, addresses):
    fqdn = name.rstrip(".") + "."
    changes = ["server 127.0.0.1", f"zone {fqdn}", f"update delete {fqdn} A"]
    changes.extend(f"update add {fqdn} 5 A {address}" for address in sorted(addresses))
    subprocess.run(
        ["nsupdate", "-v", "-t", "4", "-k", "/etc/bind/otus-dz09.key"],
        input="\n".join([*changes, "send", ""]) + "\n", text=True, timeout=6, check=True,
    )


def synchronize(settings):
    name = settings["portal"]
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", name):
        raise ValueError("Invalid portal DNS name")
    payload = local_json(f"{CONSUL}/health/service/portal?passing=true&consistent")
    desired = healthy_addresses(payload, settings["instances"])
    current = dns_addresses(name, 53, authoritative=True)
    if current != desired:
        update_records(name, desired)
        LOG.info("BIND %s: %s -> %s", name, sorted(current), sorted(desired))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        synchronize(json.loads(Path("/etc/otus-dz09/sync.json").read_text()))
    except (KeyError, TypeError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        LOG.error("BIND update failed: %s", error)
        raise SystemExit(1) from None
