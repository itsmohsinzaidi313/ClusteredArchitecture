#!/bin/sh
# Wrapper healthcheck for Patroni + PostgreSQL
# Exits 0 when both Patroni REST API (port 8008) and Postgres (pg_isready on 5433)

set -e

# Check Patroni REST API
if curl -fsS http://localhost:8008 >/dev/null 2>&1; then
  # Check PostgreSQL readiness
  if command -v pg_isready >/dev/null 2>&1; then
    if pg_isready -U postgres -h localhost -p 5433 -q; then
      exit 0
    else
      # Postgres not ready
      exit 1
    fi
  else
    # Fallback: tcp port check using /dev/tcp (may not be available)
    # If /dev/tcp isn't available, fail so Docker will retry.
    if (exec 3<>/dev/tcp/localhost/5433) >/dev/null 2>&1; then
      exit 0
    else
      exit 1
    fi
  fi
else
  # Patroni REST API not responding
  exit 1
fi
