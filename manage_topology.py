#!/usr/bin/env python3
"""Generate the local deployment directories for a Patroni cluster topology."""

from __future__ import annotations

import argparse
import ipaddress
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from string import Template


ROOT = Path(__file__).resolve().parent
INVENTORY = ROOT / "wireguard-ips.txt"
STATE_FILE = ROOT / "topology-state.json"
MANAGED_MARKER = ".cluster-topology-managed"
NODES_PER_VM = 3
POSTGRES_BASE_PORT = 15432
PATRONI_REST_BASE_PORT = 18008


@dataclass(frozen=True)
class Vm:
    index: int
    wireguard_ip: ipaddress.IPv4Address

    @property
    def name(self) -> str:
        return f"vm-{roman(self.index)}"

    @property
    def directory(self) -> Path:
        return ROOT / self.name

    @property
    def node_start(self) -> int:
        return ((self.index - 1) * NODES_PER_VM) + 1


def roman(value: int) -> str:
    numerals = (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    )
    result = []
    for number, symbol in numerals:
        while value >= number:
            result.append(symbol)
            value -= number
    return "".join(result)


def load_inventory(path: Path) -> list[ipaddress.IPv4Address]:
    if not path.is_file():
        raise ValueError(f"Inventory file does not exist: {path}")

    addresses: list[ipaddress.IPv4Address] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw_line.split("#", 1)[0].strip()
        if not value:
            continue
        try:
            address = ipaddress.ip_address(value)
        except ValueError as error:
            raise ValueError(
                f"{path}:{line_number}: '{value}' is not a valid IP address."
            ) from error
        if not isinstance(address, ipaddress.IPv4Address):
            raise ValueError(f"{path}:{line_number}: only IPv4 addresses are supported.")
        if address not in ipaddress.ip_network("10.100.0.0/16"):
            raise ValueError(
                f"{path}:{line_number}: {address} is outside the WireGuard subnet 10.100.0.0/16."
            )
        if address in addresses:
            raise ValueError(f"{path}:{line_number}: {address} is duplicated.")
        addresses.append(address)

    if not addresses:
        raise ValueError("The inventory must contain at least one WireGuard IPv4 address.")
    return addresses


def write_inventory(path: Path, addresses: list[ipaddress.IPv4Address]) -> None:
    content = (
        "# One WireGuard IPv4 address per VM. This file is the cluster topology source of truth.\n"
        + "".join(f"{address}\n" for address in addresses)
    )
    path.write_text(content, encoding="utf-8")


def load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"version": 1, "next_vm_index": 1, "members": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not valid JSON.") from error

    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not isinstance(state.get("next_vm_index"), int)
        or state["next_vm_index"] < 1
        or not isinstance(state.get("members"), dict)
    ):
        raise ValueError(f"{path} has an unsupported topology state format.")
    return state


def reconcile_state(
    addresses: list[ipaddress.IPv4Address], state: dict[str, object]
) -> dict[str, object]:
    members = state["members"]
    assert isinstance(members, dict)
    next_vm_index = state["next_vm_index"]
    assert isinstance(next_vm_index, int)

    current_ips = {str(address) for address in addresses}
    retained_members: dict[str, int] = {}
    assigned_indexes: set[int] = set()
    for ip, index in members.items():
        if not isinstance(ip, str) or not isinstance(index, int) or index < 1:
            raise ValueError("Topology state contains an invalid VM member.")
        if index in assigned_indexes:
            raise ValueError("Topology state assigns the same VM identity twice.")
        assigned_indexes.add(index)
        if ip in current_ips:
            retained_members[ip] = index

    next_vm_index = max(next_vm_index, max(assigned_indexes, default=0) + 1)
    for address in addresses:
        ip = str(address)
        if ip not in retained_members:
            retained_members[ip] = next_vm_index
            next_vm_index += 1

    return {
        "version": 1,
        "next_vm_index": next_vm_index,
        "members": retained_members,
    }


def write_state(path: Path, state: dict[str, object]) -> None:
    path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_vms(
    addresses: list[ipaddress.IPv4Address], state: dict[str, object]
) -> list[Vm]:
    members = state["members"]
    assert isinstance(members, dict)
    return [Vm(members[str(address)], address) for address in addresses]


def render(template: str, **values: object) -> str:
    return Template(template).substitute(**values)


def node_name(number: int) -> str:
    return f"patroni{number}"


def node_postgres_port(number: int) -> int:
    return POSTGRES_BASE_PORT + ((number - 1) % NODES_PER_VM)


def node_rest_port(number: int) -> int:
    return PATRONI_REST_BASE_PORT + ((number - 1) % NODES_PER_VM)


def all_nodes(vms: list[Vm]) -> list[tuple[str, str, int, int]]:
    nodes = []
    for vm in vms:
        for number in range(vm.node_start, vm.node_start + NODES_PER_VM):
            nodes.append(
                (node_name(number), str(vm.wireguard_ip), node_postgres_port(number), node_rest_port(number))
            )
    return nodes


PATRONI_DOCKERFILE = """\
FROM postgres:18

RUN apt-get update && apt-get install -y --no-install-recommends \\
    python3-pip \\
    python3-venv \\
    curl \\
    gosu \\
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/patroni-venv \\
    && /opt/patroni-venv/bin/pip install --no-cache-dir patroni[etcd3] psycopg2-binary \\
    && ln -s /opt/patroni-venv/bin/patroni /usr/local/bin/patroni

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
"""

PATRONI_ENTRYPOINT = """\
#!/bin/sh
set -eu

until curl --fail --silent --show-error "http://${PATRONI_ETCD3_HOSTS%%,*}/health" >/dev/null; do
    echo "Waiting for etcd at ${PATRONI_ETCD3_HOSTS%%,*}..."
    sleep 2
done

mkdir -p /etc/patroni
chown -R postgres:postgres /var/lib/postgresql /etc/patroni

cat > /etc/patroni/patroni.yml <<EOF
scope: ${PATRONI_SCOPE}
namespace: /service/
name: ${PATRONI_NAME}

restapi:
  listen: 0.0.0.0:8008
  connect_address: ${PATRONI_RESTAPI_CONNECT_ADDRESS}

etcd3:
  hosts: ${PATRONI_ETCD3_HOSTS}

bootstrap:
  dcs:
    ttl: 30
    loop_wait: 10
    retry_timeout: 10
    maximum_lag_on_failover: 1048576
    postgresql:
      use_pg_rewind: true
      use_slots: true
      parameters:
        max_wal_senders: 10
        max_replication_slots: 10
        wal_level: replica
        hot_standby: on
        wal_keep_size: 128MB
        wal_log_hints: on
  initdb:
    - encoding: UTF8
    - locale: en_US.UTF-8
  pg_hba:
    - host replication replicator 10.100.0.0/16 scram-sha-256
    - host all all 10.100.0.0/16 scram-sha-256

postgresql:
  listen: 0.0.0.0:5432
  connect_address: ${PATRONI_POSTGRESQL_CONNECT_ADDRESS}
  data_dir: /var/lib/postgresql/data/pgdata
  pgpass: /tmp/pgpass
  authentication:
    replication:
      username: ${PATRONI_REPLICATION_USERNAME}
      password: ${PATRONI_REPLICATION_PASSWORD}
    superuser:
      username: ${POSTGRES_USER}
      password: ${POSTGRES_PASSWORD}
EOF

exec gosu postgres patroni /etc/patroni/patroni.yml
"""


def write_patroni(vm: Vm, vms: list[Vm]) -> None:
    directory = vm.directory / "patroni"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "Dockerfile").write_text(PATRONI_DOCKERFILE, encoding="utf-8")
    entrypoint = directory / "entrypoint.sh"
    entrypoint.write_text(PATRONI_ENTRYPOINT, encoding="utf-8")
    entrypoint.chmod(0o755)

    etcd_hosts = ",".join(f"{member.wireguard_ip}:2379" for member in vms)
    services = []
    for number in range(vm.node_start, vm.node_start + NODES_PER_VM):
        name = node_name(number)
        services.append(
            render(
                """\
  $name:
    build: .
    container_name: $name
    restart: unless-stopped
    environment:
      PATRONI_SCOPE: postgres-cluster
      PATRONI_NAME: $name
      PATRONI_ETCD3_HOSTS: $etcd_hosts
      PATRONI_RESTAPI_CONNECT_ADDRESS: $wireguard_ip:$rest_port
      PATRONI_POSTGRESQL_CONNECT_ADDRESS: $wireguard_ip:$postgres_port
      POSTGRES_USER: $${POSTGRES_USER:?Set POSTGRES_USER in patroni/.env}
      POSTGRES_PASSWORD: $${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in patroni/.env}
      PATRONI_REPLICATION_USERNAME: $${PATRONI_POSTGRESQL_AUTHENTICATION_REPLICATION_USERNAME:?Set PATRONI_POSTGRESQL_AUTHENTICATION_REPLICATION_USERNAME in patroni/.env}
      PATRONI_REPLICATION_PASSWORD: $${PATRONI_POSTGRESQL_AUTHENTICATION_REPLICATION_PASSWORD:?Set PATRONI_POSTGRESQL_AUTHENTICATION_REPLICATION_PASSWORD in patroni/.env}
    volumes:
      - $name-data:/var/lib/postgresql/data
    ports:
      - "$wireguard_ip:$postgres_port:5432"
      - "$wireguard_ip:$rest_port:8008"
""",
                name=name,
                etcd_hosts=etcd_hosts,
                wireguard_ip=vm.wireguard_ip,
                postgres_port=node_postgres_port(number),
                rest_port=node_rest_port(number),
            )
        )
    volumes = "\n".join(
        f"  {node_name(number)}-data:"
        for number in range(vm.node_start, vm.node_start + NODES_PER_VM)
    )
    (directory / "compose.yml").write_text(
        f"services:\n{''.join(services)}\nvolumes:\n{volumes}\n", encoding="utf-8"
    )
    env_file = directory / ".env"
    if not env_file.exists():
        env_file.write_text(
            "POSTGRES_USER=postgres\n"
            "POSTGRES_PASSWORD=replace-with-a-secret\n"
            "PATRONI_POSTGRESQL_AUTHENTICATION_REPLICATION_USERNAME=replicator\n"
            "PATRONI_POSTGRESQL_AUTHENTICATION_REPLICATION_PASSWORD=replace-with-a-secret\n",
            encoding="utf-8",
        )


def write_etcd(vm: Vm, vms: list[Vm]) -> None:
    directory = vm.directory / "etcd"
    directory.mkdir(parents=True, exist_ok=True)
    cluster = ",".join(
        f"etcd{member.index}=http://{member.wireguard_ip}:2380" for member in vms
    )
    (directory / "compose.yml").write_text(
        render(
            """\
services:
  etcd:
    image: quay.io/coreos/etcd:v3.6.5
    container_name: etcd$index
    restart: unless-stopped
    environment:
      ETCD_NAME: etcd$index
      ETCD_DATA_DIR: /etcd-data
      ETCD_LISTEN_CLIENT_URLS: http://0.0.0.0:2379
      ETCD_ADVERTISE_CLIENT_URLS: http://$wireguard_ip:2379
      ETCD_LISTEN_PEER_URLS: http://0.0.0.0:2380
      ETCD_INITIAL_ADVERTISE_PEER_URLS: http://$wireguard_ip:2380
      ETCD_INITIAL_CLUSTER: $cluster
      ETCD_INITIAL_CLUSTER_TOKEN: postgres-etcd-cluster
      ETCD_INITIAL_CLUSTER_STATE: new
    command: etcd
    volumes:
      - etcd-data:/etcd-data
    ports:
      - "$wireguard_ip:2379:2379"
      - "$wireguard_ip:2380:2380"

volumes:
  etcd-data:
""",
            index=vm.index,
            wireguard_ip=vm.wireguard_ip,
            cluster=cluster,
        ),
        encoding="utf-8",
    )


def write_haproxy(vm: Vm, vms: list[Vm]) -> None:
    directory = vm.directory / "haproxy"
    directory.mkdir(parents=True, exist_ok=True)
    backends = "\n".join(
        f"    server {name} {ip}:{postgres_port} check port {rest_port}"
        for name, ip, postgres_port, rest_port in all_nodes(vms)
    )
    config = render(
        """\
global
    log stdout local0

defaults
    mode tcp
    timeout connect 10s
    timeout client 5m
    timeout server 5m
    option tcplog

frontend pg_rw
    bind *:5433
    default_backend pg_rw

frontend pg_ro
    bind *:5434
    default_backend pg_ro

backend pg_rw
    option httpchk GET /primary
    http-check expect status 200
    default-server inter 2s fall 2 rise 1 on-marked-down shutdown-sessions
$backends

backend pg_ro
    balance roundrobin
    option httpchk GET /replica
    http-check expect status 200
    default-server inter 2s fall 2 rise 1 on-marked-down shutdown-sessions
$backends

listen stats
    bind *:8404
    mode http
    stats enable
    stats uri /
""",
        backends=backends,
    )
    (directory / "haproxy.cfg").write_text(config, encoding="utf-8")
    (directory / "compose.yml").write_text(
        render(
            """\
services:
  haproxy:
    image: haproxy:3.1-alpine
    container_name: haproxy$index
    restart: unless-stopped
    ports:
      - "$wireguard_ip:5433:5433"
      - "$wireguard_ip:5434:5434"
      - "$wireguard_ip:8404:8404"
    volumes:
      - ./haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro
""",
            index=vm.index,
            wireguard_ip=vm.wireguard_ip,
        ),
        encoding="utf-8",
    )


def generate(vms: list[Vm]) -> None:
    wanted_directories = {vm.directory.resolve() for vm in vms}

    for vm in vms:
        vm.directory.mkdir(exist_ok=True)
        (vm.directory / MANAGED_MARKER).write_text(
            "This directory is generated by manage_topology.py. Do not edit generated files.\n",
            encoding="utf-8",
        )
        write_patroni(vm, vms)
        write_etcd(vm, vms)
        write_haproxy(vm, vms)

    for directory in ROOT.glob("vm-*"):
        if not directory.is_dir() or directory.resolve() in wanted_directories:
            continue
        marker = directory / MANAGED_MARKER
        if marker.is_file():
            shutil.rmtree(directory)
            print(f"Removed obsolete generated directory: {directory.name}")
        else:
            print(
                f"Leaving unmanaged directory untouched: {directory.name}. "
                f"Add {MANAGED_MARKER} only after confirming it is generator-owned.",
                file=sys.stderr,
            )

    print(f"Generated {len(vms)} VM directories and {len(vms) * NODES_PER_VM} Patroni nodes.")


def prompt_add(addresses: list[ipaddress.IPv4Address]) -> None:
    value = input("WireGuard IPv4 address for the new VM: ").strip()
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError(f"'{value}' is not a valid IPv4 address.") from error
    if not isinstance(address, ipaddress.IPv4Address) or address not in ipaddress.ip_network("10.100.0.0/16"):
        raise ValueError("The address must be an IPv4 address in 10.100.0.0/16.")
    if address in addresses:
        raise ValueError(f"{address} already exists in the inventory.")
    addresses.append(address)


def prompt_remove(addresses: list[ipaddress.IPv4Address]) -> None:
    if len(addresses) == 1:
        raise ValueError("Refusing to remove the only VM from the topology.")
    print("Current VMs:")
    for index, address in enumerate(addresses, 1):
        print(f"  {index}. vm-{roman(index)} ({address})")
    value = input("WireGuard IPv4 address to remove: ").strip()
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as error:
        raise ValueError(f"'{value}' is not a valid IPv4 address.") from error
    if address not in addresses:
        raise ValueError(f"{address} is not in the inventory.")
    confirmation = input(f"Remove {address} and regenerate all remaining VMs? [y/N]: ").strip().lower()
    if confirmation != "y":
        print("No changes made.")
        return
    addresses.remove(address)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage a Patroni deployment topology from WireGuard IP inventory."
    )
    parser.add_argument(
        "action",
        choices=("generate", "add", "remove"),
        nargs="?",
        default="generate",
        help="Generate the current topology, add a VM, or remove a VM.",
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=INVENTORY,
        help=f"Inventory path (default: {INVENTORY.name}).",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=STATE_FILE,
        help=f"Persistent VM identity state path (default: {STATE_FILE.name}).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        addresses = load_inventory(args.inventory)
        state = load_state(args.state)
        if args.action == "add":
            prompt_add(addresses)
            write_inventory(args.inventory, addresses)
        elif args.action == "remove":
            initial_addresses = addresses.copy()
            prompt_remove(addresses)
            if addresses == initial_addresses:
                return 0
            write_inventory(args.inventory, addresses)
        state = reconcile_state(addresses, state)
        write_state(args.state, state)
        generate(build_vms(addresses, state))
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
