# ContextAuthLabServer

FastAPI ingest and disk-storage service for ContextAuthLab. This project is now independent from the Android Gradle project.

The Android client lives in the sibling directory:

```text
/data/paper/sp/app_exp/ContextAuthLabApp
/data/paper/sp/app_exp/ContextAuthLabServer
```

## Layout

```text
app/                    FastAPI application, schema validation, rules, storage
tests/                  Pytest suite
tools/                  sample ingest, e2e, Docker smoke, load scripts
data/                   local runtime data and test data
logs/                   local server logs
vendor/wheels/          offline Python wheels for Docker builds
docker-compose.yml      canonical local compose file
```

## Local Python

```bash
cd /data/paper/sp/app_exp/ContextAuthLabServer
python3 -m pip install -r requirements-dev.txt
PYTHONPATH=. pytest -q tests
PYTHONPATH=. uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Docker

```bash
cd /data/paper/sp/app_exp/ContextAuthLabServer
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

Build the server image explicitly:

```bash
docker build -t contextauthlab/server:latest .
```

## Tests

```bash
make test-server
make test-e2e
make test-docker
```

`tools/test_load.py` is a manual pressure test and is not intended for default CI.
