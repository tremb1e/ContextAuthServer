#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p data/paper logs
chmod 0777 data/paper logs 2>/dev/null || true
cp -n .env.example .env >/dev/null 2>&1 || true

docker compose -f docker-compose.yml up -d --build

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/health | grep -q '"ok"'; then
    break
  fi
  sleep 1
done

curl -fsS http://127.0.0.1:8000/health | grep -q '"ok"'
curl -fsS http://127.0.0.1:8000/api/v1/config | grep -q 'serverStudySalt'
curl -fsS http://127.0.0.1:8000/api/v1/rules | grep -q 'rule_hash'

python tools/send_sample_batch.py --server http://127.0.0.1:8000 --count 6 --task-category I2 --output tools/e2e_typing_result.json
DEVICE_ID="$(python - <<'PY'
import json
print(json.load(open("tools/e2e_typing_result.json"))["device_id"])
PY
)"

batch_count="$(find data/paper/devices/"$DEVICE_ID" -path "*/by_category/*" -prune -o -name '*.json' ! -name '*.meta.json' -type f -print | wc -l)"
test "$batch_count" -ge 6

category_count="$(find data/paper/devices/"$DEVICE_ID"/by_category/I2 -name '*.json' -print | wc -l)"
test "$category_count" -ge 6

tail -n 6 data/paper/index/batches.jsonl | grep -q '"task_category":"I2"'
grep -q '"event":"ingest_stored"' logs/server.jsonl

python tools/send_sample_batch.py --server http://127.0.0.1:8000 --count 12 --task-category THIRD_PARTY_APP --output tools/e2e_third_party_result.json
curl -fsS http://127.0.0.1:8000/metrics | grep -q 'ingest_total{result="ok"}'
docker compose logs --no-color contextauth-server | grep -E '"event":"(ingest_received|ingest_stored)"' >/dev/null

docker compose down
test -d data/paper/devices/"$DEVICE_ID"
docker compose up -d
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/health | grep -q '"ok"'; then
    break
  fi
  sleep 1
done
test -d data/paper/devices/"$DEVICE_ID"

docker compose down
echo "e2e ok"
