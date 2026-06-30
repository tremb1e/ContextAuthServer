#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

cleanup() {
  docker compose down >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build -t contextauth/server:test .
docker image inspect contextauth/server:test >/dev/null

uid="$(docker run --rm contextauth/server:test id -u)"
test "$uid" = "1000"

docker inspect contextauth/server:test --format '{{json .Config.Healthcheck.Test}}' | grep -q '/ready'
if docker run --rm --entrypoint sh contextauth/server:test -c "find /app/app -maxdepth 2 -type d \\( -name templates -o -name static \\) | grep ." ; then
  echo "dashboard/static/template files found"
  exit 1
fi

mkdir -p data/paper logs
docker compose up -d --build
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health | grep -q '"ok"'; then
    break
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:8000/health | grep -q '"ok"'
docker compose exec -T contextauth-server python -c "import os, re; status=open('/proc/1/status', encoding='utf-8').read(); assert re.search(r'^Uid:\\s+1000\\s+1000\\s+1000\\s+1000$', status, re.M); assert os.stat('/data/paper').st_uid == 1000; assert os.stat('/app/logs').st_uid == 1000"

python tools/send_sample_batch.py --server http://127.0.0.1:8000 --output tools/docker_smoke_result.json
curl -fsS http://127.0.0.1:8000/metrics | grep -q 'ingest_total'
docker compose logs --no-color contextauth-server | tail -n 50 | grep -q '"event"'
test "$(curl -o /dev/null -s -w "%{http_code}" http://127.0.0.1:8000/dashboard)" = "404"

docker compose restart contextauth-server
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/health | grep -q '"ok"'; then
    break
  fi
  sleep 1
done
test -s data/paper/index/batches.jsonl

docker compose down
test -d data/paper
echo "docker deployment smoke ok"
