#!/usr/bin/env bash
# F20-27: Generate a 256-bit hex secret for filling .env values
# (POSTGRES_PASSWORD, NEO4J_PASSWORD, ARTA_API_KEY, JWT_SECRET_KEY, etc.).
#
# Usage:
#   ./scripts/gen-secret.sh                    # one secret to stdout
#   ./scripts/gen-secret.sh JWT_SECRET_KEY     # prefixed: JWT_SECRET_KEY=<hex>
#   ./scripts/gen-secret.sh --all              # one per common .env secret slot
#
# Prefer this over hand-typing — placeholder values like `change-me-*` are
# easy to miss in code reviews and catastrophically weaken any deploy.

set -euo pipefail

gen() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    elif [ -r /dev/urandom ]; then
        head -c 32 /dev/urandom | xxd -p -c 64
    else
        echo "ERROR: neither openssl nor /dev/urandom available — install openssl first" >&2
        exit 1
    fi
}

case "${1:-}" in
    --all|-a)
        for slot in POSTGRES_PASSWORD NEO4J_PASSWORD ARTA_API_KEY JWT_SECRET_KEY REDIS_PASSWORD; do
            echo "$slot=$(gen)"
        done
        ;;
    "")
        gen
        ;;
    *)
        echo "$1=$(gen)"
        ;;
esac
