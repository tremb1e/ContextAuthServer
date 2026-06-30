# ContextAuth Server Image Deployment

Generated: 2026-06-30
Image: contextauth/server:latest
Image ID: sha256:5027854cabbe950e77668147be9bc15f9ebf258fa4a409f2b2d1ea657784888f
Platform: linux/amd64
Artifact: artifacts/contextauth-server-latest.tar (50M)
SHA-256: 9eccd07cc2adc25ca4e5565c2efcd2ef64009c7459735736bc3fe1be8c937723

## Files

- contextauth-server-latest.tar: Docker image archive exported with docker save.
- contextauth-server-latest.tar.sha256: checksum for transfer verification.
- contextauth-server-deployment.md: this deployment guide.

## Import On Target Server

Copy the tar and checksum to the target server, then run:

```bash
sha256sum -c contextauth-server-latest.tar.sha256
docker load -i contextauth-server-latest.tar
docker image inspect contextauth/server:latest
docker run --rm contextauth/server:latest id -u
```

The exported artifact is linux/amd64. ARM64 servers need emulation or a native ARM64 rebuild with matching binary wheels. The image config runs as root so the entrypoint can repair bind-mount ownership, then starts the API as UID/GID 1000 by default.

## Recommended docker run

```bash
mkdir -p /var/lib/contextauth/data/paper /var/log/contextauth

# Optional but useful before first boot; the image also fixes ownership on startup.
chown -R 1000:1000 /var/lib/contextauth/data/paper /var/log/contextauth

# Bind to localhost when using a reverse proxy. Use 0.0.0.0 only on a controlled lab network.
docker run -d --name contextauth-server \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -e SERVER_DATA_DIR=/data/paper \
  -e SERVER_LOG_DIR=/app/logs \
  -e SERVER_RULES_FILE=/data/paper/rules.json \
  -e SERVER_STUDY_SALT=Continuous_Authentication \
  -e SERVER_FIX_PERMISSIONS=true \
  -e SERVER_CHOWN_RECURSIVE=true \
  -v /var/lib/contextauth/data/paper:/data/paper:rw \
  -v /var/log/contextauth:/app/logs:rw \
  contextauth/server:latest
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
DATA_VOLUME=/var/lib/contextauth/data/paper
LOG_VOLUME=/var/log/contextauth
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
chown -R 1000:1000 /var/lib/contextauth/data/paper /var/log/contextauth
chmod -R u+rwX,g+rwX /var/lib/contextauth/data/paper /var/log/contextauth
```

## Verification

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/api/v1/config
docker logs --tail=100 contextauth-server
```

Keep `SERVER_STUDY_SALT` stable. Changing it changes derived research device IDs.
