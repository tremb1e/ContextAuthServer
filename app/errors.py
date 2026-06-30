from __future__ import annotations

from enum import StrEnum


class RejectReason(StrEnum):
    INVALID_ENVELOPE = "invalid_envelope"
    INVALID_BASE64 = "invalid_base64"
    PAYLOAD_HASH_MISMATCH = "payload_hash_mismatch"
    CORRUPTED_LZ4_PAYLOAD = "corrupted_lz4_payload"
    INVALID_JSON = "invalid_json"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    INTERNAL_ERROR = "internal_error"
