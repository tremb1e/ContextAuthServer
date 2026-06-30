FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

FROM base AS builder

WORKDIR /app
COPY requirements.txt ./
COPY vendor/wheels ./vendor/wheels
RUN pip install --no-cache-dir --no-index --find-links ./vendor/wheels --prefix=/install -r requirements.txt \
    && find /install -depth \( -type d -name '__pycache__' -o -type f -name '*.pyc' \) -exec rm -rf '{}' +

FROM base AS runtime

ENV SERVER_DATA_DIR=/data/paper \
    SERVER_LOG_DIR=/app/logs \
    SERVER_RULES_FILE=/data/paper/rules.json \
    SERVER_FIX_PERMISSIONS=true \
    SERVER_CHOWN_RECURSIVE=true \
    APP_UID=1000 \
    APP_GID=1000 \
    RULES_VERSION=1

WORKDIR /app
RUN groupadd -g 1000 appuser \
    && useradd -u 1000 -g 1000 -m appuser \
    && mkdir -p /data/paper /app/logs \
    && chown -R appuser:appuser /data /app

COPY --from=builder /install /usr/local
RUN printf '%s\n' \
    '#!/usr/local/bin/python' \
    'import sys, urllib.request' \
    'url = sys.argv[-1]' \
    'urllib.request.urlopen(url, timeout=5).read()' \
    > /usr/local/bin/curl \
    && chmod +x /usr/local/bin/curl

COPY app ./app
COPY main.py ./
COPY docker-entrypoint.py /usr/local/bin/contextauth-entrypoint
RUN chmod +x /usr/local/bin/contextauth-entrypoint \
    && chown -R appuser:appuser /app

USER root

EXPOSE 8000
VOLUME ["/data/paper"]
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD curl -fsS http://127.0.0.1:8000/ready || exit 1

ENTRYPOINT ["contextauth-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
