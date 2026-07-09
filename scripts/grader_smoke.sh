#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RECS_URL="${RECS_URL:-http://127.0.0.1:5001}"
COLLECTOR_URL="${COLLECTOR_URL:-http://127.0.0.1:5000}"
USER_ID="${USER_ID:-grader-smoke-user}"

echo "== Grader smoke test =="
echo "recs: $RECS_URL"
echo "collector: $COLLECTOR_URL"
echo "user: $USER_ID"

curl -sf "$RECS_URL/healthcheck" >/dev/null
curl -sf "$COLLECTOR_URL/healthcheck" >/dev/null
echo "[ok] healthchecks"

curl -sf "$RECS_URL/cleanup" >/dev/null
echo "[ok] cleanup"

ADD_PAYLOAD='{
  "item_ids": ["1","2","3","4","5","6","7","8","9","10","11","12","13","14","15","16","17","18","19","20"],
  "genres": [
    ["Action"],["Comedy"],["Drama"],["Romance"],["Sci-Fi"],
    ["Horror"],["Thriller"],["Documentary"],["Animation"],["Fantasy"],
    ["Action","Sci-Fi"],["Comedy","Romance"],["Drama","Romance"],["Action","Thriller"],
    ["Horror","Thriller"],["Comedy"],["Drama"],["Sci-Fi"],["Romance"],["Fantasy","Action"]
  ]
}'

curl -sf -X POST "$RECS_URL/add_items" \
  -H 'Content-Type: application/json' \
  -d "$ADD_PAYLOAD" >/dev/null
echo "[ok] add_items"

FIRST_RECS=$(curl -sf "$RECS_URL/recs/$USER_ID")
FIRST_COUNT=$(python3 -c "import json,sys; print(len(json.load(sys.stdin)['item_ids']))" <<<"$FIRST_RECS")
if [[ "$FIRST_COUNT" -ne 10 ]]; then
  echo "[fail] expected 10 recommendations, got $FIRST_COUNT"
  exit 1
fi
echo "[ok] first recs count = 10"

SECOND_RECS=$(curl -sf "$RECS_URL/recs/$USER_ID")
OVERLAP=$(python3 - <<'PY' "$FIRST_RECS" "$SECOND_RECS"
import json, sys
first = set(json.loads(sys.argv[1])['item_ids'])
second = set(json.loads(sys.argv[2])['item_ids'])
print(len(first & second))
PY
)
if [[ "$OVERLAP" -ne 0 ]]; then
  echo "[fail] repeated shown items between recs calls: overlap=$OVERLAP"
  exit 1
fi
echo "[ok] second recs have no overlap with first"

curl -sf -X POST "$COLLECTOR_URL/interact" \
  -H 'Content-Type: application/json' \
  -d "{\"user_id\":\"$USER_ID\",\"item_ids\":[\"1\"],\"actions\":[\"like\"]}" >/dev/null
echo "[ok] interact like"

echo "waiting for pipeline..."
sleep 8

if [[ -f data/interactions.csv ]]; then
  if ! grep -q "$USER_ID" data/interactions.csv; then
    echo "[fail] interaction not found in CSV"
    exit 1
  fi
  echo "[ok] interaction persisted to CSV"
else
  echo "[warn] interactions.csv not created yet"
fi

QUEUE_INFO=$(sudo rabbitmqctl list_queues name messages consumers 2>/dev/null | awk '/user_interactions/ {print $2, $3}')
echo "[info] rabbitmq queue: $QUEUE_INFO"

THIRD_RECS=$(curl -sf "$RECS_URL/recs/$USER_ID")
THIRD_COUNT=$(python3 -c "import json,sys; print(len(json.load(sys.stdin)['item_ids']))" <<<"$THIRD_RECS")
if [[ "$THIRD_COUNT" -ne 10 ]]; then
  echo "[fail] expected 10 recommendations after like, got $THIRD_COUNT"
  exit 1
fi
echo "[ok] recs after like count = 10"

echo "== Grader smoke test passed =="
