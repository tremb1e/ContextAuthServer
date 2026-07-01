"""Batch + envelope loaders for the ingest / synthetic on-disk layouts.

Two on-disk layouts are supported by a single reader, because the synthetic
generator writes the SAME tree the ingest server writes:

    {input_dir}/devices/{device_id}/{date}/{batch_id}.json      (accepted batch)
    {input_dir}/devices/{device_id}/{date}/{batch_id}.meta.json (skipped)
    {input_dir}/devices/{device_id}/by_category/...             (skipped symlinks)

``load_batches`` yields validated raw batch dicts. ``load_envelope`` decodes an
8-field ``LZ4_FRAME+JSON`` envelope back to its batch dict, mirroring the server
consumer path (base64 -> sha256-over-compressed check -> lz4 decompress -> json).
``iter_windows`` is a light session-scoped sliding-window helper over a batch's
sorted sensor samples, used by higher stages and smoke tests.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Iterator, Union

import lz4.frame

PathLike = Union[str, Path]

#: Minimum set of keys a raw batch dict must carry to be usable downstream.
#: This is a LOOSE gate (the authoritative validator is ``app.schemas.Batch``);
#: it exists so loaders fail fast on obviously-corrupt files.
REQUIRED_BATCH_KEYS: frozenset[str] = frozenset(
    {
        "batch_id",
        "device_id",
        "session_id",
        "record_type",
        "collection_source",
        "app_package_name",
        "sampling_rate_hz",
        "base_elapsed_nanos",
        "started_at_wall_millis",
        "ended_at_wall_millis",
        "sensor_samples",
        "context_events",
        "context_features",
        "diagnostics",
    }
)

#: The 8 envelope keys (see _recon_contract.md §a).
ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
        "algorithm",
        "payload_base64",
        "payload_sha256_hex",
        "device_id",
        "batch_id",
        "rule_version",
        "rule_hash",
        "created_at_wall_millis",
    }
)


class BatchValidationError(ValueError):
    """Raised when a batch dict is missing required keys."""


class EnvelopeError(ValueError):
    """Raised when an envelope is malformed or fails integrity checks."""


def validate_batch_keys(batch: dict[str, Any]) -> dict[str, Any]:
    """Loosely validate a raw batch dict against :data:`REQUIRED_BATCH_KEYS`.

    Args:
        batch: A decoded batch dict.

    Returns:
        The same ``batch`` (for chaining).

    Raises:
        BatchValidationError: If any required key is missing or the record type
            is not ``"collection"``.
    """
    if not isinstance(batch, dict):
        raise BatchValidationError(f"batch must be a dict, got {type(batch).__name__}")
    missing = REQUIRED_BATCH_KEYS - batch.keys()
    if missing:
        raise BatchValidationError(f"batch missing required keys: {sorted(missing)}")
    if batch.get("record_type") != "collection":
        raise BatchValidationError(f"unexpected record_type: {batch.get('record_type')!r}")
    return batch


def _iter_batch_files(input_dir: Path) -> Iterator[Path]:
    """Yield accepted batch JSON files under ``input_dir/devices``.

    Skips ``*.meta.json`` sidecars and anything under a ``by_category``
    directory (those are symlinks / pointers to the real batch files).

    Args:
        input_dir: Dataset root that contains a ``devices/`` subtree.

    Yields:
        Paths to batch JSON files, in sorted order for determinism.
    """
    devices_dir = input_dir / "devices"
    search_root = devices_dir if devices_dir.is_dir() else input_dir
    for path in sorted(search_root.rglob("*.json")):
        name = path.name
        if name.endswith(".meta.json"):
            continue
        if "by_category" in path.parts:
            continue
        yield path


def load_batches(input_dir: PathLike, *, strict: bool = True) -> Iterator[dict[str, Any]]:
    """Iterate validated raw batch dicts from an on-disk dataset root.

    Reads the ingest / synthetic ``devices/{device_id}/{date}/{batch_id}.json``
    layout. ``.meta.json`` files and ``by_category`` symlinks are skipped.

    Args:
        input_dir: Dataset root (contains ``devices/``).
        strict: If True (default), raise on unreadable / invalid files. If
            False, silently skip them (useful for best-effort scans).

    Yields:
        Validated raw batch dicts.

    Raises:
        FileNotFoundError: If ``input_dir`` does not exist.
        BatchValidationError: If ``strict`` and a file is invalid.
    """
    root = Path(input_dir)
    if not root.exists():
        raise FileNotFoundError(f"input_dir does not exist: {root}")
    for path in _iter_batch_files(root):
        try:
            with path.open("r", encoding="utf-8") as handle:
                batch = json.load(handle)
            validate_batch_keys(batch)
        except (json.JSONDecodeError, BatchValidationError, OSError) as exc:
            if strict:
                raise BatchValidationError(f"failed to load batch {path}: {exc}") from exc
            continue
        yield batch


def load_envelope(envelope: dict[str, Any] | PathLike, *, verify_hash: bool = True) -> dict[str, Any]:
    """Decode an 8-field ``LZ4_FRAME+JSON`` envelope back to its batch dict.

    Mirrors the server consumer path (_recon_contract.md §0): base64-decode the
    payload, optionally verify the SHA-256 over the COMPRESSED bytes, lz4-frame
    decompress, then ``json.loads`` to a dict.

    Args:
        envelope: Either an already-parsed envelope dict, or a path to an
            envelope JSON file.
        verify_hash: If True, verify ``payload_sha256_hex`` over the compressed
            bytes and raise on mismatch.

    Returns:
        The decoded batch dict.

    Raises:
        EnvelopeError: On missing keys, wrong algorithm, base64/hash/lz4/json
            failures.
    """
    if not isinstance(envelope, dict):
        with Path(envelope).open("r", encoding="utf-8") as handle:
            envelope = json.load(handle)

    missing = ENVELOPE_KEYS - envelope.keys()
    if missing:
        raise EnvelopeError(f"envelope missing required keys: {sorted(missing)}")
    if envelope["algorithm"] != "LZ4_FRAME+JSON":
        raise EnvelopeError(f"unexpected algorithm: {envelope['algorithm']!r}")

    try:
        compressed = base64.b64decode(str(envelope["payload_base64"]).encode("ascii"), validate=True)
    except (ValueError, TypeError) as exc:
        raise EnvelopeError("invalid_base64") from exc

    if verify_hash:
        digest = hashlib.sha256(compressed).hexdigest()
        if digest != envelope["payload_sha256_hex"]:
            raise EnvelopeError("payload_hash_mismatch")

    try:
        plaintext = lz4.frame.decompress(compressed)
    except (RuntimeError, ValueError) as exc:
        raise EnvelopeError("corrupted_lz4_payload") from exc

    try:
        batch = json.loads(plaintext.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EnvelopeError("invalid_json") from exc
    if not isinstance(batch, dict):
        raise EnvelopeError("invalid_json")
    return batch


def load_envelopes(input_dir: PathLike, *, verify_hash: bool = True) -> Iterator[dict[str, Any]]:
    """Iterate decoded batch dicts from ``{input_dir}/envelopes/*.json``.

    Args:
        input_dir: Dataset root that may contain an ``envelopes/`` subdir.
        verify_hash: Passed through to :func:`load_envelope`.

    Yields:
        Decoded batch dicts (empty iterator if no ``envelopes/`` dir).
    """
    env_dir = Path(input_dir) / "envelopes"
    if not env_dir.is_dir():
        return
    for path in sorted(env_dir.glob("*.json")):
        yield load_envelope(path, verify_hash=verify_hash)


def _sorted_samples(batch: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sensor samples sorted by ``timestamp_elapsed_nanos`` (stable).

    Args:
        batch: A raw batch dict.

    Returns:
        Sensor sample dicts sorted by elapsed timestamp.
    """
    samples = list(batch.get("sensor_samples", []))
    return sorted(samples, key=lambda sample: int(sample.get("timestamp_elapsed_nanos", 0)))


def iter_windows(
    batch: dict[str, Any],
    *,
    window_size_sec: float = 5.0,
    stride_sec: float = 1.0,
) -> Iterator[dict[str, Any]]:
    """Yield fixed-length sliding windows over a single batch's sensor stream.

    This is a light helper (the authoritative windowing lives in stage S2's
    ``preprocessing.windowing``). Windows are cut on the elapsed-time axis using
    ``base_elapsed_nanos`` as the origin. Each yielded dict carries the window's
    time bounds and the sensor samples that fall inside it.

    Args:
        batch: A raw batch dict.
        window_size_sec: Window length in seconds.
        stride_sec: Window stride in seconds.

    Yields:
        Dicts with keys ``window_index, start_elapsed_ns, end_elapsed_ns,
        samples`` (samples is the list falling in ``[start, end)``).
    """
    if window_size_sec <= 0 or stride_sec <= 0:
        raise ValueError("window_size_sec and stride_sec must be positive")

    samples = _sorted_samples(batch)
    if not samples:
        return

    window_ns = int(round(window_size_sec * 1e9))
    stride_ns = int(round(stride_sec * 1e9))
    first_ns = int(samples[0]["timestamp_elapsed_nanos"])
    last_ns = int(samples[-1]["timestamp_elapsed_nanos"])

    window_index = 0
    start_ns = first_ns
    while start_ns <= last_ns:
        end_ns = start_ns + window_ns
        in_window = [s for s in samples if start_ns <= int(s["timestamp_elapsed_nanos"]) < end_ns]
        if in_window:
            yield {
                "window_index": window_index,
                "start_elapsed_ns": start_ns,
                "end_elapsed_ns": end_ns,
                "samples": in_window,
            }
        window_index += 1
        start_ns += stride_ns


def count_batches(batches: Iterable[dict[str, Any]]) -> int:
    """Count items in a batch iterable (convenience for smoke checks).

    Args:
        batches: Any iterable of batch dicts.

    Returns:
        The number of items.
    """
    return sum(1 for _ in batches)
