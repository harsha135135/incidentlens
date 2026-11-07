#!/bin/sh
# Stage 0: list the telemetry an unmodified LedgerCore actually exports.
#
# Starts LedgerCore's own PostgreSQL in a separate Compose project, runs the packaged jar with
# the demo settings from LedgerCore's .env.example on port 18080, drives its demo script so
# request metrics exist, then records the metric names, the Prometheus endpoint status and a
# sample of log lines. Everything is torn down afterwards (including the database volume).
#
# Usage: scripts/stage0_ledgercore_audit.sh [path-to-ledgercore]   (default ../ledgercore)
set -eu

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LEDGERCORE="$(cd "${1:-../ledgercore}" && pwd)"
OUT="$ROOT/evaluation/results/stage0/ledgercore-audit-$(date -u +%Y%m%dT%H%M%SZ)"
PROJECT=incidentlens-stage0-audit
PORT=18080
mkdir -p "$OUT"

cd "$LEDGERCORE"
JAR=target/ledgercore-0.0.1-SNAPSHOT.jar
[ -f "$JAR" ] || ./mvnw -q -DskipTests package
git rev-parse HEAD >"$OUT/ledgercore-revision.txt"
git status --short >>"$OUT/ledgercore-revision.txt"

set -a
. ./.env.example
set +a

cleanup() {
	[ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
	docker compose -p "$PROJECT" --env-file .env.example down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker compose -p "$PROJECT" --env-file .env.example up -d --wait >"$OUT/compose.log" 2>&1
LEDGERCORE_PORT=$PORT java -jar "$JAR" >"$OUT/ledgercore-stdout.log" 2>&1 &
APP_PID=$!

for _ in $(seq 1 60); do
	curl -sf "http://127.0.0.1:$PORT/actuator/health" >/dev/null 2>&1 && break
	sleep 1
done
curl -s "http://127.0.0.1:$PORT/actuator/health" >"$OUT/health.json"

LEDGERCORE_BASE_URL="http://127.0.0.1:$PORT" sh scripts/demo.sh >"$OUT/demo.log" 2>&1 || echo "demo script exited non-zero" >>"$OUT/demo.log"
sleep 3   # let the outbox poller deliver the demo events

ADMIN=demo-admin:admin-demo
curl -s -u "$ADMIN" "http://127.0.0.1:$PORT/actuator/metrics" | python3 -c \
	'import json,sys; print("\n".join(sorted(json.load(sys.stdin)["names"])))' >"$OUT/metric-names.txt"
for name in http.server.requests hikaricp.connections.pending hikaricp.connections.timeout \
	ledgercore.outbox.pending.events ledgercore.outbox.failed.events ledgercore.outbox.oldest.undelivered.age; do
	curl -s -u "$ADMIN" "http://127.0.0.1:$PORT/actuator/metrics/$name" >"$OUT/metric-$name.json"
done
curl -s -o /dev/null -w '%{http_code}\n' -u "$ADMIN" "http://127.0.0.1:$PORT/actuator/prometheus" >"$OUT/prometheus-endpoint-status.txt"
# A customer login can read metrics today (recorded as an observed limitation).
curl -s -o /dev/null -w '%{http_code}\n' -u customer-a:customer-a-demo \
	"http://127.0.0.1:$PORT/actuator/metrics" >"$OUT/customer-metrics-access-status.txt"

grep -E "Outbox|demo-consumer|WARN|ERROR" "$OUT/ledgercore-stdout.log" | head -40 >"$OUT/log-sample.txt" || true

echo "metric names: $(wc -l <"$OUT/metric-names.txt" | tr -d ' ')"
echo "prometheus endpoint status: $(cat "$OUT/prometheus-endpoint-status.txt")"
echo "customer access to /actuator/metrics: $(cat "$OUT/customer-metrics-access-status.txt")"
echo "results: ${OUT#$ROOT/}"
