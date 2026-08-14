#!/usr/bin/env bash
# Live login smoke test — guards the seed-admin / bootstrap regression.
# Fresh Postgres (schema.sql, NO seed admin) → ensure_bootstrap_admin() creates
# the first admin from ARTA_BOOTSTRAP_ADMIN_* → password login returns a token.
#
# Requires: docker + Python deps installed (pip install -r requirements.txt).
# Env overrides: PYTHON, PORT_PG, PORT_API.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY=${PYTHON:-python3}
PORT_PG=${PORT_PG:-55432}
PORT_API=${PORT_API:-58000}
CID="arta-smoke-pg-$$"
API_PID=""
cleanup() { [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true; docker rm -f "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "[smoke] starting postgres (schema.sql at initdb)…"
docker run -d --name "$CID" \
  -e POSTGRES_USER=arta -e POSTGRES_PASSWORD=arta -e POSTGRES_DB=arta \
  -p "$PORT_PG:5432" \
  -v "$ROOT/src/db/schema.sql:/docker-entrypoint-initdb.d/schema.sql:ro" \
  postgres:16-alpine >/dev/null
# Gate on TCP readiness (-h 127.0.0.1): during initdb, postgres serves the
# schema.sql load on a socket-only temp server, so a plain pg_isready passes
# too early. TCP only listens once initdb is complete and the real server is up.
for _ in $(seq 1 60); do docker exec "$CID" pg_isready -h 127.0.0.1 -p 5432 -U arta >/dev/null 2>&1 && break; sleep 1; done
# Settle: the users table must be queryable over TCP (schema.sql fully applied)
# before the API floods a cold DB — otherwise bootstrap's DB call gets reset.
for _ in $(seq 1 60); do docker exec "$CID" psql -h 127.0.0.1 -U arta -d arta -tAc "select count(*) from users" >/dev/null 2>&1 && break; sleep 1; done
sleep 2

export DATABASE_URL="postgresql+asyncpg://arta:arta@localhost:$PORT_PG/arta"
export ARTA_BOOTSTRAP_ADMIN_EMAIL="smoke-admin@example.com"
export ARTA_BOOTSTRAP_ADMIN_PASSWORD="smoke-pass-123"
export ARTA_TELEMETRY=0
echo "[smoke] starting API…"
( cd "$ROOT" && exec "$PY" -m uvicorn src.api.main:app --host 127.0.0.1 --port "$PORT_API" ) >/tmp/arta-smoke-api.log 2>&1 &
API_PID=$!
for _ in $(seq 1 45); do curl -sf "http://127.0.0.1:$PORT_API/health" >/dev/null 2>&1 && break; sleep 1; done

echo "[smoke] logging in with the bootstrap admin…"
resp=$(curl -s -X POST "http://127.0.0.1:$PORT_API/api/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=$ARTA_BOOTSTRAP_ADMIN_EMAIL&password=$ARTA_BOOTSTRAP_ADMIN_PASSWORD")
if echo "$resp" | grep -q access_token; then
  echo "[smoke] PASS — bootstrap admin login returned a token"
else
  echo "[smoke] FAIL — no token. response: $resp"; echo "---- api log tail ----"; tail -30 /tmp/arta-smoke-api.log; exit 1
fi
