#!/bin/bash
set -euo pipefail
umask 077
export DEBIAN_FRONTEND=noninteractive
sudo cloud-init status --wait
sudo apt-get -o DPkg::Lock::Timeout=120 update -qq
sudo apt-get -o DPkg::Lock::Timeout=120 install -y ansible-core openssl

mkdir -p "$HOME/.otus-dz09"
exec 9>"$HOME/.otus-dz09/deploy.lock"
flock -n 9
export ANSIBLE_HOST_KEY_CHECKING=True
export ANSIBLE_SSH_ARGS="-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$HOME/.otus-dz09/known_hosts"

python3 - <<'PY'
import json
from pathlib import Path

settings = json.loads(Path("settings.json").read_text())
nodes = settings["lab_nodes"]
hosts = {
    name: {
        "ansible_host": node["private"],
        "ansible_user": "ubuntu",
        "ansible_ssh_private_key_file": str(Path("ansible-key").resolve()),
    }
    for name, node in nodes.items() if name != "client"
}
Path("inventory.json").write_text(json.dumps({
    "all": {"children": {
        "controller": {"hosts": {"client": {"ansible_connection": "local"}}},
        "web": {"hosts": hosts},
    }}
}))
PY

tls="$HOME/.otus-dz09/tls"
mkdir -p "$tls"
domain=$(python3 -c 'import json; print(json.load(open("settings.json"))["portal_name"])')
if [[ ! -e "$tls/ca.key" && ! -e "$tls/ca.crt" ]]; then
    openssl req -x509 -newkey rsa:3072 -nodes -days 3650 \
        -subj '/CN=OTUS DZ09 Lab CA' -addext 'basicConstraints=critical,CA:TRUE' \
        -keyout "$tls/ca.key" -out "$tls/ca.crt" 2>/dev/null
fi
test -s "$tls/ca.key" && test -s "$tls/ca.crt"
printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n' "$domain" > tls-ext
for node in client web-a web-b web-c; do
    if [[ -s "$tls/$node.key" && -s "$tls/$node.crt" ]] && \
       openssl x509 -in "$tls/$node.crt" -checkend 86400 -noout >/dev/null; then
        continue
    fi
    openssl req -new -newkey rsa:2048 -nodes -subj "/CN=$domain" \
        -keyout "$tls/$node.key" -out "$node.csr" 2>/dev/null
    openssl x509 -req -in "$node.csr" -CA "$tls/ca.crt" -CAkey "$tls/ca.key" \
        -CAcreateserial -days 365 -sha256 -extfile tls-ext -out "$tls/$node.crt"
done

ansible-playbook -i inventory.json ansible/site.yml -e @stack.yml -e @settings.json \
    -e "tls_source=$tls"
