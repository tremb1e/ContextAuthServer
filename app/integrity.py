from __future__ import annotations

import base64
import binascii
import hashlib
import hmac


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_base64(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("invalid_base64") from exc


def verify_sha256(data: bytes, expected_hex: str) -> bool:
    return hmac.compare_digest(sha256_hex(data), expected_hex)


def encode_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
