# ContextAuthLab Server Image Deployment

Generated: 2026-05-27
Image: contextauthlab/server:latest
Image ID: sha256:c38bfae1eb872532036f1c9230a673d822d701088dc60bf52e4d835a293f3fe3
Platform: linux/amd64
Artifact: artifacts/contextauthlab-server-latest.tar (50M)
SHA-256: 50b65309d9981371b4bb526d856e8f84abbda7b0c18a894e1b80afa918c5279f

## Files

- contextauthlab-server-latest.tar: Docker image archive exported with docker save.
- contextauthlab-server-latest.tar.sha256: checksum for transfer verification.
- contextauthlab-server-deployment.md: this deployment guide.

## Import On Target Server

Copy the tar and checksum to the target server, then run:

```bash
sha256sum -c contextauthlab-server-latest.tar.sha256
docker load -i contextauthlab-server-latest.tar
docker image inspect contextauthlab/server:latest
docker run --rm contextauthlab/server:latest id -u
```

The exported artifact is linux/amd64. ARM64 servers need emulation or a native ARM64 rebuild with matching binary wheels. The image config runs as root so the entrypoint can repair bind-mount ownership, then starts the API as UID/GID 1000 by default.

## Recommended docker run

```bash
mkdir -p /var/lib/contextauthlab/data/paper /var/log/contextauthlab

# Optional but useful before first boot; the image also fixes ownership on startup.
chown -R 1000:1000 /var/lib/contextauthlab/data/paper /var/log/contextauthlab

# Bind to localhost when using a reverse proxy. Use 0.0.0.0 only on a controlled lab network.
docker run -d --name contextauthlab-server \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -e SERVER_DATA_DIR=/data/paper \
  -e SERVER_LOG_DIR=/app/logs \
  -e SERVER_RULES_FILE=/data/paper/rules.json \
  -e SERVER_STUDY_SALT=Continuous_Authentication \
  -e SERVER_FIX_PERMISSIONS=true \
  -e SERVER_CHOWN_RECURSIVE=true \
  -v /var/lib/contextauthlab/data/paper:/data/paper:rw \
  -v /var/log/contextauthlab:/app/logs:rw \
  contextauthlab/server:latest
```

If the host path must be owned by a different numeric user, set the API process UID/GID and chown target together:

```bash
-e APP_UID=<uid> -e APP_GID=<gid>
```

## Compose Deployment

Use the repository `docker-compose.yml` and set `.env` values:

```env
SERVER_BIND=127.0.0.1
SERVER_PORT=8000
SERVER_STUDY_SALT=Continuous_Authentication
SERVER_FIX_PERMISSIONS=true
SERVER_CHOWN_RECURSIVE=true
SERVER_CONTAINER_UID=1000
SERVER_CONTAINER_GID=1000
DATA_VOLUME=/var/lib/contextauthlab/data/paper
LOG_VOLUME=/var/log/contextauthlab
```

Then start:

```bash
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8000/ready
```

## Permission Error Recovery

For `PermissionError: [Errno 13] Permission denied: '/data/paper/devices'`, the bind-mounted host directory is not writable by the non-root API process. Fix with one of:

```bash
# Provided compose: let init service and entrypoint repair ownership.
docker compose down
docker compose up -d

# Manual repair for default UID/GID.
chown -R 1000:1000 /var/lib/contextauthlab/data/paper /var/log/contextauthlab
chmod -R u+rwX,g+rwX /var/lib/contextauthlab/data/paper /var/log/contextauthlab
```

## Verification

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/api/v1/config
docker logs --tail=100 contextauthlab-server
```

Keep `SERVER_STUDY_SALT` stable. Changing it changes derived research device IDs.
