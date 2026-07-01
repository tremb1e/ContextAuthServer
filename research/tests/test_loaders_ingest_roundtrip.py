"""Loaders read the ingest / synthetic layout (raw batches + envelopes) — §15.1.1.

Asserts that:

* :func:`research.preprocessing.loaders.load_batches` reads the synthetic
  ``devices/`` tree, skipping ``*.meta.json`` sidecars and ``by_category``
  symlinks, and validates the batch key contract.
* :func:`research.preprocessing.loaders.load_envelope` /
  :func:`load_envelopes` decode the 8-field LZ4_FRAME+JSON envelopes back to the
  same batch dicts (hash-over-compressed integrity verified).
* :func:`iter_windows` yields non-empty sliding windows over a batch's sorted
  sensor stream.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research.preprocessing.loaders import (
    ENVELOPE_KEYS,
    REQUIRED_BATCH_KEYS,
    BatchValidationError,
    EnvelopeError,
    count_batches,
    iter_windows,
    load_batches,
    load_envelope,
    load_envelopes,
    validate_batch_keys,
)


def test_load_batches_reads_devices_tree(synthetic_dir: Path) -> None:
    """``load_batches`` yields validated batch dicts from ``devices/``."""
    batches = list(load_batches(synthetic_dir))
    assert batches, "expected at least one batch from the synthetic devices/ tree"
    for batch in batches:
        # Every yielded batch satisfies the loose key contract + record_type.
        assert REQUIRED_BATCH_KEYS <= batch.keys()
        assert batch["record_type"] == "collection"
        assert batch["collection_source"] == "BUILTIN_TASK"


def test_load_batches_skips_meta_and_symlinks(synthetic_dir: Path) -> None:
    """Batch count equals the number of real ``{batch_id}.json`` files only."""
    real_batch_files = [
        p
        for p in (synthetic_dir / "devices").rglob("*.json")
        if not p.name.endswith(".meta.json") and "by_category" not in p.parts
    ]
    assert count_batches(load_batches(synthetic_dir)) == len(real_batch_files)


def test_envelope_roundtrip_matches_batch(synthetic_dir: Path) -> None:
    """A decoded envelope reproduces a real batch dict (by batch_id)."""
    batches_by_id = {b["batch_id"]: b for b in load_batches(synthetic_dir)}
    decoded = list(load_envelopes(synthetic_dir))
    assert decoded, "expected envelopes (generator ran with emit_envelopes=True)"
    for batch in decoded:
        assert ENVELOPE_KEYS  # sanity: the 8-field contract exists
        assert batch["batch_id"] in batches_by_id
        # The decoded payload is the same batch the server would have stored.
        assert batch["device_id"] == batches_by_id[batch["batch_id"]]["device_id"]
        assert batch["diagnostics"]["encryption"] == "none"
        assert batch["diagnostics"]["compression"] == "lz4_frame"


def test_envelope_hash_mismatch_raises(synthetic_dir: Path) -> None:
    """Corrupting the payload hash makes ``load_envelope`` raise EnvelopeError."""
    env_dir = synthetic_dir / "envelopes"
    env_path = sorted(env_dir.glob("*.json"))[0]
    import json

    envelope = json.loads(env_path.read_text(encoding="utf-8"))
    envelope["payload_sha256_hex"] = "00" * 32  # wrong digest
    with pytest.raises(EnvelopeError):
        load_envelope(envelope, verify_hash=True)


def test_validate_batch_keys_rejects_missing() -> None:
    """A batch missing a required key is rejected by the loose validator."""
    with pytest.raises(BatchValidationError):
        validate_batch_keys({"batch_id": "x"})


def test_iter_windows_yields_windows(synthetic_dir: Path) -> None:
    """``iter_windows`` slides non-empty windows over a batch sensor stream."""
    batch = next(iter(load_batches(synthetic_dir)))
    windows = list(iter_windows(batch, window_size_sec=1.0, stride_sec=1.0))
    assert windows, "expected at least one populated window"
    for window in windows:
        assert window["samples"], "each yielded window must contain samples"
        assert window["start_elapsed_ns"] < window["end_elapsed_ns"]
