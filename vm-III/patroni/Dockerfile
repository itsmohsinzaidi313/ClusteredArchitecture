FROM postgres:18

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    python3-venv \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/patroni-venv \
    && /opt/patroni-venv/bin/pip install --no-cache-dir patroni[etcd3] psycopg2-binary \
    && ln -s /opt/patroni-venv/bin/patroni /usr/local/bin/patroni

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
