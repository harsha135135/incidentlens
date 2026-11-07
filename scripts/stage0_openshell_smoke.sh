#!/bin/sh
# Stage 0: prove the OpenShell sandbox can reach the internal gateway port and nothing else.
#
# Usage: scripts/stage0_openshell_smoke.sh [BIND_ADDRESS]   (default 127.0.0.1)
# Starts a capability-checking listener on BIND_ADDRESS:8089 and a decoy on BIND_ADDRESS:8088,
# runs scripts/stage0/probe.py inside a sandbox created with deploy/openshell/stage0-policy.yaml,
# saves everything under evaluation/results/stage0/, and exits non-zero if any check fails.
set -eu

cd "$(dirname "$0")/.."
export NO_COLOR=1
BIND="${1:-127.0.0.1}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="evaluation/results/stage0/openshell-smoke-${STAMP}"
NAME="il-stage0-$(date -u +%H%M%S)"
mkdir -p "$OUT"

# A throwaway capability for this run only; it never leaves this machine.
STAGE0_CAPABILITY="$(openssl rand -hex 32)"
export STAGE0_CAPABILITY

python3 scripts/stage0/listener.py "$BIND" 8089 internal >"$OUT/listener-8089.jsonl" 2>&1 &
INTERNAL_PID=$!
python3 scripts/stage0/listener.py "$BIND" 8088 public >"$OUT/listener-8088.jsonl" 2>&1 &
PUBLIC_PID=$!

cleanup() {
	openshell sandbox delete "$NAME" >/dev/null 2>&1 || true
	kill "$INTERNAL_PID" "$PUBLIC_PID" 2>/dev/null || true
}
trap cleanup EXIT
sleep 1

{
	echo "bind_address=$BIND"
	echo "openshell=$(openshell --version) gateway=$(openshell-gateway --version)"
	echo "image=$(docker image inspect incidentlens-runner:stage0 --format '{{.Id}}')"
	echo "policy_sha256=$(shasum -a 256 deploy/openshell/stage0-policy.yaml | cut -d' ' -f1)"
	echo "listeners:"
	lsof -nP -iTCP:8088 -iTCP:8089 -sTCP:LISTEN | tail -n +2
} >"$OUT/environment.txt"

openshell sandbox create --name "$NAME" --from incidentlens-runner:stage0 \
	--policy deploy/openshell/stage0-policy.yaml \
	--upload scripts/stage0/probe.py:/sandbox/probe.py \
	--detach --no-tty --no-auto-providers >"$OUT/sandbox-create.log" 2>&1

# The capability goes in on stdin so it never appears in a process argument list.
printf '%s\n' "$STAGE0_CAPABILITY" | openshell sandbox exec -n "$NAME" --no-tty --no-login-shell -- \
	sh -c 'read -r token; STAGE0_CAPABILITY="$token" exec python3 /sandbox/probe.py' \
	>"$OUT/probe.jsonl" 2>"$OUT/probe-stderr.log" || true

# A binary the policy does not list, started directly by the exec shell rather than by python3.
if openshell sandbox exec -n "$NAME" --no-tty --no-login-shell -- \
	bash -c 'exec 3<>/dev/tcp/host.openshell.internal/8089 && echo connected' 2>&1 | grep -q '^connected'; then
	echo '{"check": "other_binary_denied", "expect": "bash cannot reach 8089", "pass": false, "detail": "connected"}' >>"$OUT/probe.jsonl"
else
	echo '{"check": "other_binary_denied", "expect": "bash cannot reach 8089", "pass": true, "detail": "not connected"}' >>"$OUT/probe.jsonl"
fi

openshell policy get "$NAME" >"$OUT/effective-policy.txt" 2>&1 || true
openshell logs "$NAME" >"$OUT/sandbox-logs.txt" 2>&1 || true

total=$(grep -c '"check"' "$OUT/probe.jsonl" || true)
passed=$(grep -c '"pass": true' "$OUT/probe.jsonl" || true)
echo "checks passed: ${passed}/${total}" | tee "$OUT/summary.txt"
cat "$OUT/probe.jsonl"
echo "listener 8089 saw:"; grep '"peer"' "$OUT/listener-8089.jsonl" || echo "  (no requests)"
echo "listener 8088 saw:"; grep '"peer"' "$OUT/listener-8088.jsonl" || echo "  (no requests)"
echo "results: $OUT"
[ "$total" -gt 0 ] && [ "$passed" -eq "$total" ]
