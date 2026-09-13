#!/usr/bin/env bash
# Generates an unencrypted RSA key pair for the Snowflake service user.
# Private key: keys/rsa_key.p8 (gitignored). Prints the public key body to paste into
# scripts/snowflake_setup.sql.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p keys
chmod 700 keys
if [[ -f keys/rsa_key.p8 ]]; then
  echo "keys/rsa_key.p8 already exists; not overwriting." >&2
else
  openssl genrsa 2048 2>/dev/null | openssl pkcs8 -topk8 -inform PEM -out keys/rsa_key.p8 -nocrypt
  chmod 600 keys/rsa_key.p8
fi
openssl rsa -in keys/rsa_key.p8 -pubout -out keys/rsa_key.pub 2>/dev/null
echo
echo "Public key body (paste into RSA_PUBLIC_KEY in scripts/snowflake_setup.sql):"
echo
grep -v -- "-----" keys/rsa_key.pub | tr -d '\n'
echo
