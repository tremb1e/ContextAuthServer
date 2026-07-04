# ContextAuthServer

FastAPI ingest and disk-storage service for ContextAuthLab. This project is now independent from the Android Gradle project.

The Android client lives in the sibling directory:

```text
/data/paper/sp/app_exp/ContextAuthlab
/data/paper/sp/app_exp/ContextAuthServer
```

## Layout

```text
app/                    FastAPI ingest service: schema validation, rules, storage
research/               offline ML experiment layer (MoE authenticator); conda env
                        hmog_1dcnn, NOT run in the ingest container. See research/README.md
deploy/                 production deployment (compose + real data root deploy/data/paper);
                        authoritative for production. See deploy/README.md
docs/                   maintainer docs; ContextAuthServer_服务端说明.md is the
                        authoritative current-state reference (contract, privacy, data state)
tests/                  Pytest suite for the ingest service (base env)
tools/                  sample ingest, e2e, Docker smoke, load, event_detail sanitizer scripts
data/                   local runtime data and test fixtures (gitignored, synthetic)
logs/                   local server logs
vendor/wheels/          offline Python wheels for Docker builds
docker-compose.yml      LOCAL dev/integration compose ONLY — production uses deploy/docker-compose.yml
```

## Local Python

```bash
cd /data/paper/sp/app_exp/ContextAuthServer
python3 -m pip install -r requirements-dev.txt
PYTHONPATH=. pytest -q tests
PYTHONPATH=. uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Docker

```bash
cd /data/paper/sp/app_exp/ContextAuthServer
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

Build the server image explicitly:

```bash
docker build -t contextauth/server:latest .
```

## Tests

```bash
make test-server
make test-e2e
make test-docker
```

`tools/test_load.py` is a manual pressure test and is not intended for default CI.
