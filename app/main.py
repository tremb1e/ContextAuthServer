from __future__ import annotations

import json
import time
import uuid
from typing import Any

import lz4.frame
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import ValidationError

from .config import SETTINGS, get_server_study_salt
from .integrity import decode_base64, verify_sha256
from .logging_config import configure_logging, ingest_log
from .rules import active_rules_version, rules_response
from .schemas import Batch, ConfigResponse, Envelope, RulesResponse, TimeSyncConfig
from .storage import STORE, DuplicateBatchConflict, now_ms


configure_logging()

app = FastAPI(title="ContextAuth Server", version="1.0.0")

METRICS_REGISTRY = CollectorRegistry()
INGEST_TOTAL = Counter("ingest_total", "Ingest requests by result", ["result"], registry=METRICS_REGISTRY)
INGEST_DECRYPT_SECONDS = Histogram("ingest_decrypt_seconds", "Compatibility no-op: this prototype performs no decryption", registry=METRICS_REGISTRY)
INGEST_DECOMPRESS_SECONDS = Histogram("ingest_decompress_seconds", "LZ4 decompression duration", registry=METRICS_REGISTRY)
INGEST_PAYLOAD_BYTES_IN = Histogram("ingest_payload_bytes_in", "Compressed payload size", registry=METRICS_REGISTRY)
INGEST_PAYLOAD_BYTES_OUT = Histogram("ingest_payload_bytes_out", "Decompressed payload size", registry=METRICS_REGISTRY)
INGEST_ERRORS_TOTAL = Counter("ingest_errors_total", "Ingest errors by type", ["type"], registry=METRICS_REGISTRY)
SERVER_UP = Gauge("server_up", "Server availability", registry=METRICS_REGISTRY)
SERVER_UP.set(1)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    try:
        STORE.assert_ready()
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "ready"}


@app.get("/api/v1/config", response_model=ConfigResponse)
def config() -> ConfigResponse:
    return ConfigResponse(
        server_study_salt=get_server_study_salt(SETTINGS),
        rules_version=active_rules_version(),
        server_time_millis=now_ms(),
        time_sync=TimeSyncConfig(
            method="HTTP_MIDPOINT",
            region=SETTINGS.time_sync_region,
            server_time_field="serverTimeMillis",
            recommended_ntp_servers=list(SETTINGS.time_sync_ntp_servers),
            max_acceptable_rtt_millis=SETTINGS.time_sync_max_acceptable_rtt_millis,
        ),
    )


@app.get("/api/v1/rules", response_model=RulesResponse)
def rules() -> RulesResponse:
    return rules_response()


@app.get("/metrics")
def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(METRICS_REGISTRY).decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else None


def _safe_append_error(reason: str, envelope: Envelope | None, request_id: str, details: dict[str, Any] | None = None) -> None:
    try:
        STORE.append_error(reason, envelope, request_id, details)
    except OSError:
        pass


def _reject(
    status_code: int,
    reason: str,
    request_id: str,
    envelope: Envelope | None,
    request: Request,
    details: dict[str, Any] | None = None,
) -> None:
    INGEST_TOTAL.labels(result="reject").inc()
    INGEST_ERRORS_TOTAL.labels(type=reason).inc()
    _safe_append_error(reason, envelope, request_id, details)
    ingest_log(
        "ingest_rejected",
        request_id=request_id,
        device_id=envelope.device_id if envelope else None,
        batch_id=envelope.batch_id if envelope else None,
        rule_version=envelope.rule_version if envelope else None,
        schema_ok=False,
        reject_reason=reason,
        client_ip=_client_ip(request),
        status_code=status_code,
    )
    raise HTTPException(status_code=status_code, detail=reason)


def _quarantine_reject(
    reason: str,
    request_id: str,
    envelope: Envelope,
    plaintext_obj: dict[str, Any],
    compressed_bytes: bytes,
    plaintext_bytes: bytes,
    decompress_ms: float,
    client_ip: str | None,
    schema_ok: bool,
) -> None:
    try:
        STORE.quarantine(envelope, plaintext_obj, reason, request_id)
    except OSError:
        _safe_append_error(reason, envelope, request_id, {"quarantine_error": "write_failed"})
    INGEST_TOTAL.labels(result="quarantine").inc()
    INGEST_ERRORS_TOTAL.labels(type=reason).inc()
    ingest_log(
        "ingest_quarantined",
        request_id=request_id,
        device_id=envelope.device_id,
        batch_id=envelope.batch_id,
        rule_version=envelope.rule_version,
        bytes_in=len(compressed_bytes),
        bytes_decompressed=len(plaintext_bytes),
        decompress_ms=decompress_ms,
        schema_ok=schema_ok,
        quarantined=True,
        reject_reason=reason,
        client_ip=client_ip,
        status_code=400,
    )
    raise HTTPException(status_code=400, detail=reason)


@app.post("/api/v1/ingest")
async def ingest(request: Request) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    envelope: Envelope | None = None
    compressed_bytes = b""
    plaintext_obj: dict[str, Any] | None = None
    client_ip = _client_ip(request)

    try:
        raw_body = await request.body()
        try:
            envelope = Envelope.model_validate_json(raw_body)
        except ValidationError:
            _reject(400, "invalid_envelope", request_id, None, request)

        assert envelope is not None
        ingest_log(
            "ingest_received",
            request_id=request_id,
            device_id=envelope.device_id,
            batch_id=envelope.batch_id,
            rule_version=envelope.rule_version,
            bytes_in=len(raw_body),
            schema_ok=None,
            client_ip=client_ip,
            status_code=202,
        )

        try:
            compressed_bytes = decode_base64(envelope.payload_base64)
        except ValueError:
            _reject(400, "invalid_base64", request_id, envelope, request)

        if not verify_sha256(compressed_bytes, envelope.payload_sha256_hex):
            _reject(400, "payload_hash_mismatch", request_id, envelope, request)

        INGEST_PAYLOAD_BYTES_IN.observe(len(compressed_bytes))
        t0 = time.perf_counter()
        try:
            plaintext_bytes = lz4.frame.decompress(compressed_bytes)
        except Exception:
            _reject(400, "corrupted_lz4_payload", request_id, envelope, request)
        decompress_ms = (time.perf_counter() - t0) * 1000
        INGEST_DECOMPRESS_SECONDS.observe(decompress_ms / 1000)
        INGEST_PAYLOAD_BYTES_OUT.observe(len(plaintext_bytes))

        ingest_log(
            "ingest_decompressed",
            request_id=request_id,
            device_id=envelope.device_id,
            batch_id=envelope.batch_id,
            rule_version=envelope.rule_version,
            bytes_in=len(compressed_bytes),
            bytes_decompressed=len(plaintext_bytes),
            decompress_ms=decompress_ms,
            schema_ok=None,
            client_ip=client_ip,
            status_code=202,
        )

        try:
            loaded = json.loads(plaintext_bytes.decode("utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("batch_json_not_object")
            plaintext_obj = loaded
        except Exception:
            _reject(400, "invalid_json", request_id, envelope, request)

        try:
            batch = Batch.model_validate(plaintext_obj)
        except ValidationError as exc:
            try:
                _quarantine_reject(
                    "schema_validation_failed",
                    request_id,
                    envelope,
                    plaintext_obj,
                    compressed_bytes,
                    plaintext_bytes,
                    decompress_ms,
                    client_ip,
                    schema_ok=False,
                )
            except HTTPException as http_exc:
                raise http_exc from exc

        if batch.device_id != envelope.device_id:
            _quarantine_reject(
                "envelope_batch_device_id_mismatch",
                request_id,
                envelope,
                plaintext_obj,
                compressed_bytes,
                plaintext_bytes,
                decompress_ms,
                client_ip,
                schema_ok=True,
            )

        if batch.batch_id != envelope.batch_id:
            _quarantine_reject(
                "envelope_batch_id_mismatch",
                request_id,
                envelope,
                plaintext_obj,
                compressed_bytes,
                plaintext_bytes,
                decompress_ms,
                client_ip,
                schema_ok=True,
            )

        try:
            stored = STORE.store(envelope, batch, plaintext_obj, request_id, len(compressed_bytes), len(plaintext_bytes))
        except DuplicateBatchConflict:
            _reject(409, "duplicate_batch_id_conflict", request_id, envelope, request)
        except OSError as exc:
            reason = "disk_space_below_threshold" if str(exc) == "disk_space_below_threshold" else "storage_write_failed"
            _reject(507, reason, request_id, envelope, request, {"type": type(exc).__name__})

        INGEST_TOTAL.labels(result="ok").inc()
        ingest_log(
            "ingest_stored",
            request_id=request_id,
            device_id=envelope.device_id,
            batch_id=envelope.batch_id,
            rule_version=envelope.rule_version,
            bytes_in=len(compressed_bytes),
            bytes_decompressed=len(plaintext_bytes),
            decompress_ms=decompress_ms,
            schema_ok=True,
            quarantined=False,
            client_ip=client_ip,
            status_code=200,
        )
        return {
            "status": "ok",
            "device_id_prefix": envelope.device_id[:8],
            "batch_id": envelope.batch_id,
            "stored": True,
        }
    except HTTPException:
        raise
    except Exception as exc:
        reason = "internal_error"
        INGEST_TOTAL.labels(result="reject").inc()
        INGEST_ERRORS_TOTAL.labels(type=reason).inc()
        _safe_append_error(reason, envelope, request_id, {"type": type(exc).__name__})
        ingest_log(
            "ingest_rejected",
            request_id=request_id,
            device_id=envelope.device_id if envelope else None,
            batch_id=envelope.batch_id if envelope else None,
            rule_version=envelope.rule_version if envelope else None,
            schema_ok=False,
            reject_reason=reason,
            client_ip=client_ip,
            status_code=500,
        )
        raise HTTPException(status_code=500, detail=reason) from exc
