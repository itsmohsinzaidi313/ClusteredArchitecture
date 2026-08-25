# Patroni topology generator

`wireguard-ips.txt` is the source of truth for the VM topology. It contains one
WireGuard IPv4 address per line, in VM order. The generator creates three
Patroni PostgreSQL nodes per address and updates every HAProxy backend list.
`topology-state.json` preserves VM and Patroni node identities. Do not edit or
delete it after deployment: this ensures a VM removal does not renumber the
surviving VMs or their PostgreSQL nodes.

WireGuard must already connect `10.100.0.0/16` peers before starting the
generated services. The generator does not create WireGuard interfaces,
configure peers, or deploy files to remote machines.

## Manage topology

Run commands from this directory:

```sh
python3 manage_topology.py
python3 manage_topology.py add
python3 manage_topology.py remove
```

`add` prompts for a unique `10.100.0.0/16` WireGuard IPv4 address, appends it
to the inventory, creates its next `vm-<Roman numeral>` directory, and
rewrites the etcd/Patroni/HAProxy configuration of every VM. `remove` displays
the known VMs, asks for the address to remove, requires confirmation, removes
its generator-managed directory, and rewrites the remaining topology. IDs are
never reused: removing `vm-II` leaves `vm-I` and `vm-III` unchanged, and the
next addition becomes `vm-IV`.

The script deletes only directories containing its `.cluster-topology-managed`
marker. It deliberately leaves unmarked `vm-*` directories untouched.

## Generated services

Each VM directory contains:

- `etcd/compose.yml`: one etcd member, advertising client and peer endpoints
  through its WireGuard address.
- `patroni/compose.yml`: three PostgreSQL/Patroni services. Direct database
  ports are `15432`-`15434`; REST health ports are `18008`-`18010`, both bound
  only to that VM's WireGuard address.
- `haproxy/compose.yml`: read-write `5433`, read-only `5434`, and stats `8404`
  listeners bound only to that VM's WireGuard address. All HAProxy instances
  target every Patroni node.

Set production secrets in each generated `patroni/.env` before deployment. The
required values are `POSTGRES_USER`, `POSTGRES_PASSWORD`,
`PATRONI_POSTGRESQL_AUTHENTICATION_REPLICATION_USERNAME`, and
`PATRONI_POSTGRESQL_AUTHENTICATION_REPLICATION_PASSWORD`.

## Deployment and topology changes

Copy each matching `vm-*` directory to its VM. Start a new cluster with etcd
on all VMs before Patroni, then HAProxy:

```sh
docker compose -f etcd/compose.yml up -d
docker compose -f patroni/compose.yml up -d --build
docker compose -f haproxy/compose.yml up -d
```

The generated etcd `initial-cluster` configuration is for a fresh cluster.
Adding or removing a VM from an already running etcd cluster also requires the
standard etcd runtime member-add/member-remove procedure before restarting the
affected etcd containers. Generate and distribute the updated files first,
then follow the etcd operational procedure for the running cluster; never
replace a live member's data volume to force a topology change.
