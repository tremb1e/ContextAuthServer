from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SETTINGS


LOGGER = logging.getLogger("contextauthlab")


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "payload", None)
        if not isinstance(payload, dict):
            payload = {"event": record.getMessage()}
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        payload.setdefault("level", record.levelname)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def configure_logging() -> None:
    SETTINGS.log_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False

    formatter = JsonLineFormatter()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    LOGGER.addHandler(stream)

    file_handler = logging.FileHandler(SETTINGS.log_dir / "server.jsonl", encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    logging.getLogger("uvicorn.access").disabled = True


def hash_ip(client_ip: str | None) -> str | None:
    if not client_ip:
        return None
    return hashlib.sha256(client_ip.encode("utf-8")).hexdigest()[:16]


def ingest_log(
    event: str,
    request_id: str,
    device_id: str | None = None,
    batch_id: str | None = None,
    rule_version: str | None = None,
    bytes_in: int | None = None,
    bytes_decompressed: int | None = None,
    decompress_ms: float | None = None,
    schema_ok: bool | None = None,
    quarantined: bool = False,
    reject_reason: str | None = None,
    client_ip: str | None = None,
    status_code: int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "event": event,
        "request_id": request_id,
        "device_id_prefix": device_id[:8] if device_id else None,
        "batch_id": batch_id,
        "rule_version": rule_version,
        "bytes_in": bytes_in,
        "bytes_decompressed": bytes_decompressed,
        "decrypt_ms": 0,
        "decompress_ms": decompress_ms,
        "schema_ok": schema_ok,
        "quarantined": quarantined,
        "reject_reason": reject_reason,
        "client_ip_hashed": hash_ip(client_ip),
        "status_code": status_code,
    }
    LOGGER.info(event, extra={"payload": payload})


def log_path() -> Path:
    return SETTINGS.log_dir / "server.jsonl"
