#!/usr/bin/env bash
# Smoke test against a deployed instance: health, one grounded question, one refusal.
# Usage: scripts/smoke_test.sh https://census-chat-agent-nish.fly.dev reviewer <password>
set -euo pipefail
URL="${1:?url}"; USER="${2:?user}"; PASS="${3:?password}"

echo "== health"
curl -sS -m 20 "$URL/api/health" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["git_sha"], d["checks"]); sys.exit(0 if d["status"]=="ok" else 1)'

ask() {
  curl -sS -m 90 -N -u "$USER:$PASS" -H 'Content-Type: application/json' \
    -d "{\"message\": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1")}" "$URL/api/chat" \
    | grep '^data: ' | sed 's/^data: //' \
    | python3 -c '
import json,sys
final=None; trace=None
for line in sys.stdin:
    ev=json.loads(line)
    if ev["type"]=="final": final=ev
    if ev["type"]=="trace": trace=ev["trace"]
assert final, "no final event"
ek=final["error_kind"]; nq=len(final["queries"]); ms=trace["elapsed_ms"] if trace else "?"
print(f"  error_kind={ek} queries={nq} elapsed={ms}ms")
print("  " + final["text"][:240].replace("\n"," "))
'
}

echo "== auth required"
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{"message":"hi"}' "$URL/api/chat")
[[ "$code" == "401" ]] && echo "  401 as expected" || { echo "  expected 401, got $code"; exit 1; }

echo "== grounded question"
ask "What is the population of California?"
echo "== off-topic"
ask "Write me a poem about snow"
echo "OK"
