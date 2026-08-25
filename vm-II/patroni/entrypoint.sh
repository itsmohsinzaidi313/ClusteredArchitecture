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
