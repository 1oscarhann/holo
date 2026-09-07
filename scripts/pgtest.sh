#!/usr/bin/env bash
# Start a throwaway Postgres for the test suite and print its DATABASE_URL.
#
#     eval "$(scripts/pgtest.sh start)"   # exports TEST_DATABASE_URL
#     pytest
#     scripts/pgtest.sh stop
#
# The cluster lives under /tmp and is deleted by `stop`. Never point the tests
# at a database you care about: conftest.py truncates every table between tests.
set -euo pipefail

PORT="${HOLO_TEST_PGPORT:-5433}"
DATA="${HOLO_TEST_PGDATA:-/tmp/holo-testpg}"
BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | tail -1 || true)"
[ -n "$BIN" ] || BIN="$(dirname "$(command -v initdb)")"

case "${1:-start}" in
  start)
    if [ ! -d "$DATA/base" ]; then
      "$BIN/initdb" -D "$DATA" -U postgres --auth=trust >/dev/null
    fi
    "$BIN/pg_ctl" -D "$DATA" -o "-p $PORT" -l "$DATA/server.log" -w start >/dev/null 2>&1 || true
    "$BIN/psql" -p "$PORT" -h localhost -U postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='holotest'" | grep -q 1 \
      || "$BIN/createdb" -p "$PORT" -h localhost -U postgres holotest
    echo "export TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:$PORT/holotest"
    ;;
  stop)
    "$BIN/pg_ctl" -D "$DATA" -m immediate stop >/dev/null 2>&1 || true
    rm -rf "$DATA"
    ;;
  *) echo "usage: $0 {start|stop}" >&2; exit 2 ;;
esac
