# Deployment

## System Requirements

- Docker 20.10 or newer.
- Docker Compose v2.
- x86_64 Linux host for the exported offline image. ARM64 hosts need emulation or an ARM64-specific rebuild/wheelhouse.
- 2 GB RAM minimum.
- 50 GB free disk recommended for study data.

## Quick Start

```bash
git clone <repo>
cd ContextAuthLabServer
cp .env.example .env
docker compose up -d
curl http://127.0.0.1:8000/ready
```

Set `SERVER_STUDY_SALT` once and keep it stable. Losing or changing it changes every derived research `device_id`.

The image entrypoint and compose `*-server-init` service both prepare mounted
data/log directories for the non-root API process (UID/GID `1000:1000` by
default). This avoids the common bind-mount failure where Docker creates host
directories as root and the server cannot create `devices`, `index`, or
`quarantine`. `SERVER_FIX_PERMISSIONS=false` skips the compose init ownership
change, and `SERVER_CHOWN_RECURSIVE=false` limits it to the top-level mounted
paths after the first successful boot.

## Environment

- `SERVER_BIND`: host bind address, default `127.0.0.1`. Use a reverse proxy for public HTTPS; set `0.0.0.0` only for controlled lab HTTP access.
- `SERVER_PORT`: host port, default `8000`.
- `SERVER_STUDY_SALT`: stable study salt. Default for this prototype is `Continuous_Authentication`.
- `SERVER_RULES_FILE`: editable redaction rules JSON path. Defaults to `${SERVER_DATA_DIR}/rules.json` and is materialized from `app/default_rules.json` on first start.
- `SERVER_FIX_PERMISSIONS`: let the container entrypoint create/chown data and log paths before dropping to `appuser`, default `true`.
- `SERVER_CHOWN_RECURSIVE`: recursively chown mounted data/log paths on startup, default `true`; set to `false` after first boot for very large datasets if ownership is already correct.
- `SERVER_MIN_FREE_BYTES`: non-negative free-space floor before accepting writes, default `10485760`.
- `RULES_VERSION`: fallback redaction rules version used when a newly materialized rules file has no version, default `1`.
- `INGEST_REQUIRE_AUTH`: reserved and unsupported in this prototype. Leave `false`; setting it to `true` fails fast at startup instead of silently running an unauthenticated ingest API.
- `TIME_SYNC_REGION`: advisory region in `/api/v1/config`, default `CN`.
- `TIME_SYNC_NTP_SERVERS`: comma-separated advisory NTP hosts in `/api/v1/config`; defaults to China-region public/cloud hosts.
- `TIME_SYNC_MAX_ACCEPTABLE_RTT_MILLIS`: advisory client clock-sync RTT limit, default `3000`.
- `TZ`: container timezone.
- `VERSION`: Docker image tag.
- `DATA_VOLUME` and `LOG_VOLUME`: production override paths.
- `SERVER_CONTAINER_UID` and `SERVER_CONTAINER_GID`: numeric UID/GID used by the compose init service and API process, default `1000:1000`.

## Modes

The checked-in Dockerfile uses `python:3.11-slim-bookworm` and installs from the offline `vendor/wheels` directory, so the container build does not need external package index access. The runtime stage contains only a Python-based healthcheck shim, installed Python dependencies, and the API code. It does not contain tests, docs, `.git`, Node, npm, yarn, templates, static assets, or dashboard code.

Local development:

```bash
docker compose up
```

Build a local server image for deployment:

```bash
docker build -t contextauthlab/server:latest .
docker image inspect contextauthlab/server:latest
```

Export/import a portable server image:

```bash
mkdir -p artifacts
docker save contextauthlab/server:latest -o artifacts/contextauthlab-server-latest.tar
(cd artifacts && sha256sum contextauthlab-server-latest.tar > contextauthlab-server-latest.tar.sha256)

# On another server:
docker load -i contextauthlab-server-latest.tar
docker run --rm contextauthlab/server:latest id
```

Build the Android APK artifact image from the sibling Android project:

```bash
cd /data/paper/sp/app_exp/ContextAuthLabApp
make build-app-image
docker image inspect contextauthlab/android-app-debug:latest --format '{{.Id}}'
```

The Android image is an APK artifact image, not a runnable mobile runtime. To
extract the APK from a registry-delivered image:

```bash
cid=$(docker create contextauthlab/android-app-debug:latest)
docker cp "$cid":/artifacts/contextauthlab-debug.apk artifacts/contextauthlab-debug.apk
docker rm "$cid"
```

Single-machine background deployment:

```bash
cp .env.example .env
docker compose up -d
docker compose logs -f contextauthlab-server
```

Production override:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

## Health And Troubleshooting

```bash
docker compose ps
docker compose logs -f contextauthlab-server
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
docker compose exec contextauthlab-server sh
docker compose exec contextauthlab-server sh -c 'id && ls -la /data/paper /app/logs'
```

If the service fails with `PermissionError: [Errno 13] Permission denied:
'/data/paper/devices'`, the bind-mounted host directory is not writable by the
non-root container user. With the provided compose file, restart so the init
service can repair ownership:

```bash
docker compose down
docker compose up -d
```

For a manual `docker run` deployment, prepare the host directories yourself:

```bash
mkdir -p /var/lib/contextauthlab/data/paper /var/log/contextauthlab
chown -R 1000:1000 /var/lib/contextauthlab/data/paper /var/log/contextauthlab
docker run -d --name contextauthlab-server \
  -p 127.0.0.1:8000:8000 \
  -e SERVER_DATA_DIR=/data/paper \
  -e SERVER_LOG_DIR=/app/logs \
  -e SERVER_RULES_FILE=/data/paper/rules.json \
  -e SERVER_STUDY_SALT=Continuous_Authentication \
  -v /var/lib/contextauthlab/data/paper:/data/paper:rw \
  -v /var/log/contextauthlab:/app/logs:rw \
  contextauthlab/server:latest
```

ClockSync troubleshooting:

```bash
curl -fsS http://127.0.0.1:8000/api/v1/config
```

The Android app syncs on resume and then every 60 seconds. It first tries the China-region NTP hosts advertised in `timeSync.recommendedNtpServers`. If UDP/123 is blocked, it falls back to the config response `serverTimeMillis`. Running sensor timestamps use the latest synced server offset rather than only the offset captured when collection started.

The app uses these NTP hosts internally only. Home and Details display generic ClockSync sources such as `NTP synced` or `Server time fallback`; they do not display individual NTP server addresses.

Release builds disable cleartext traffic and trust only system certificate
authorities through the main network security config. Debug builds keep
cleartext and user CA trust enabled for emulator/lab endpoints such as
`http://10.0.2.2:8000`.

## Android APK

Debug APK build:

```bash
cd /data/paper/sp/app_exp/ContextAuthLabApp
JAVA_HOME=/opt/android-studio/jbr ANDROID_HOME=/home/tremb1e/Android/Sdk ./gradlew :android-app:assembleDebug
mkdir -p artifacts
cp android-app/build/outputs/apk/debug/android-app-debug.apk artifacts/contextauthlab-debug.apk
```

Install on a test device:

```bash
adb install -r artifacts/contextauthlab-debug.apk
```

After installation, enable AccessibilityService, battery optimization exemption, and notification permission. The app starts collection automatically once required permissions, a valid research `device_id`, and screen/unlock state are ready. Server readiness, ClockSync, and Wi-Fi failures do not block local sampling; failed uploads are queued and replayed according to the Wi-Fi policy. Non-retriable server responses during queue replay are moved to the dead-letter area instead of retrying until the maximum retry count.

For UI verification, switch the device system language between Chinese and English and relaunch the app. Participant-facing screens, task instructions, protocol text, notification copy, settings, details, and dialogs should follow the system language.

Home's `Collection Status` card shows server connectivity, automatic collection state, latest connectivity-test time, latest upload time, and latest server response. Tapping the server connection chip triggers a fresh `/ready` readiness test so an unwritable data directory is visible before uploads fail.

## Server Rules File (endpoint not consumed by the app)

The server still hosts a `GET /api/v1/rules` endpoint backed by an editable file.
On startup the server reads `SERVER_RULES_FILE`. If the file does not exist, it
copies the packaged default payload from `app/default_rules.json` into
that location and then serves it from `/api/v1/rules`. The server computes
`rule_hash` at response time; do not store `rule_hash` inside the editable file.

The Android app does **not** fetch or apply this endpoint. All displayed and
entered text is dropped on-device, so there is no in-app text redaction for these
rules to configure (see `docs/redaction_rules.md`). Editing this file changes
only what the endpoint returns; it has no effect on what the app collects. The
`rule_version`/`rule_hash` carried in upload payloads are the fixed constants
`"1"` and 64 zeros.

Minimal editable file:

```json
{
  "version": "1",
  "updated_at": "2026-05-22T00:00:00Z",
  "rules": [
    {"id": "email", "target": "text", "action": "REDACT", "pattern": "...", "replacement": "<EMAIL>"}
  ],
  "package_blocklist": [],
  "max_text_length": 128,
  "default_text_action": "REDACT"
}
```

Restart the container after editing the file.

## Data Backup

Backup:

```bash
tar -czf contextauthlab-data-$(date +%Y%m%d).tar.gz data/paper logs
```

Restore:

```bash
tar -xzf contextauthlab-data-YYYYMMDD.tar.gz
docker compose up -d
```

For larger deployments, use `rsync -a data/paper/ backup-host:/path/`.

## Upgrade

Prebuilt image:

```bash
docker compose pull
docker compose up -d
```

Self-built:

```bash
git pull
docker compose build --no-cache
docker compose up -d
```

## Cleanup

Archive old data before deleting. A future `tools/prune_data/paper.py` can be mounted into the container for dry-run cleanup; current deployments should use manual retention review.

## Metrics

Prometheus scrape example:

```yaml
scrape_configs:
  - job_name: contextauthlab
    static_configs:
      - targets: ["127.0.0.1:8000"]
```

## TLS Reverse Proxy

Caddy:

```caddyfile
cca.macrz.com {
    reverse_proxy 127.0.0.1:8000
}
```

Nginx:

```nginx
server {
    server_name cca.macrz.com;
    listen 443 ssl http2;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

## Recovery

`server_study_salt.txt` loss or `SERVER_STUDY_SALT` changes invalidate stable device IDs. Back up `SERVER_DATA_DIR` daily, preferably at 00:00.

## Resource Baseline

- 5 devices: under 0.25 CPU, under 256 MB RAM, low disk growth.
- 50 devices: around 0.5 CPU, under 512 MB RAM, depends on sensor/context volume.
- 500 devices: use external monitoring, log rotation, and disk capacity planning.

## Security

Do not expose port 8000 directly to the public internet. Put the service behind a firewall and terminate TLS with Caddy or Nginx. This prototype does not enforce ingest authentication, so public deployments must rely on firewall/reverse-proxy access control. `INGEST_REQUIRE_AUTH=true` is intentionally rejected at startup until an authentication scheme is implemented. The service runs as non-root `appuser` and uses bind-mounted host directories for direct research data access.
