"""Privacy sanity: no raw text on disk + schema contract on records — §15.2.

Asserts:

* every synthetic batch record satisfies the server schema contract:
  ``diagnostics.redaction_applied is True``, ``diagnostics.encryption == "none"``,
  ``diagnostics.compression == "lz4_frame"``, ``task_category`` in I0..I6, and a
  64-hex ``device_id``;
* no batch node leaks forbidden content (no password node, no surviving text
  field) — checked via the pipeline's own defensive
  :func:`research.preprocessing.quality._has_privacy_violation`;
* no per-event ``event_detail`` carries the text-length / keystroke telemetry
  keys, and no text event carries a non-(-1) cursor index (SRV-1 red-line — a
  regression guard so the generator / on-disk data never re-grows the side
  channel);
* the forbidden ``<EDITABLE_TEXT_DROPPED>`` placeholder sentinel appears in NO
  on-disk pipeline artifact (raw batch JSON, windows parquet, split parquets);
* the on-disk parquet artifacts carry no free-text column values (their string
  columns are structural ids / JSON of numbers / scenario codes only).
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from research import SCENARIOS
from research.preprocessing.loaders import load_batches
from research.preprocessing.quality import _has_privacy_violation

_VALID_TASKS = set(SCENARIOS)
_PLACEHOLDER_SENTINEL = "<EDITABLE_TEXT_DROPPED>"
# Text keys that must never carry a surviving value on disk.
_FORBIDDEN_TEXT_KEYS = ("text", "text_redacted", "content_desc_redacted", "window_title_redacted")
# event_detail red-line (SRV-1), encoded independently of app.schemas as a guard.
_FORBIDDEN_EVENT_DETAIL_KEYS = frozenset(
    {
        "before_text_length",
        "text_total_length",
        "content_description_length",
        "text_entry_count",
        "added_count",
        "removed_count",
    }
)
_TEXT_TELEMETRY_EVENT_TYPES = frozenset(
    {
        "TYPE_VIEW_TEXT_CHANGED",
        "TYPE_VIEW_TEXT_SELECTION_CHANGED",
        "TYPE_VIEW_TEXT_TRAVERSED_AT_MOVEMENT_GRANULARITY",
    }
)
_TEXT_INDEX_KEYS = ("from_index", "to_index", "item_count", "current_item_index")


def test_synthetic_records_satisfy_schema_contract(synthetic_dir: Path) -> None:
    """Every synthetic batch honours redaction / encryption / compression / task."""
    batches = list(load_batches(synthetic_dir))
    assert batches
    for batch in batches:
        diag = batch["diagnostics"]
        assert diag["redaction_applied"] is True
        assert diag["encryption"] == "none"
        assert diag["compression"] == "lz4_frame"
        assert batch["task_category"] in _VALID_TASKS
        # device_id is a 64-hex salted hash (no PII).
        device_id = str(batch["device_id"])
        assert len(device_id) == 64 and all(c in "0123456789abcdef" for c in device_id)


def test_no_forbidden_node_content(synthetic_dir: Path) -> None:
    """No batch node leaks a password or a surviving text field."""
    for batch in load_batches(synthetic_dir):
        snapshots = [event.get("root_nodes") or [] for event in batch.get("context_events", [])]
        assert not _has_privacy_violation(snapshots), f"privacy violation in batch {batch['batch_id']}"
        # And explicitly: every forbidden text key is null on every node.
        for snapshot in snapshots:
            for node in snapshot:
                for key in _FORBIDDEN_TEXT_KEYS:
                    assert node.get(key) in (None, ""), f"node leaks {key}={node.get(key)!r}"
                assert node.get("password") is False


def test_no_event_detail_text_telemetry(synthetic_dir: Path) -> None:
    """No loaded batch's event_detail re-grows the text-length / cursor side channel."""
    for batch in load_batches(synthetic_dir):
        for event in batch.get("context_events", []):
            detail = event.get("event_detail")
            if not isinstance(detail, dict):
                continue
            leaked = _FORBIDDEN_EVENT_DETAIL_KEYS & detail.keys()
            assert not leaked, f"event_detail leaks {sorted(leaked)} in batch {batch['batch_id']}"
            if event.get("event_type") in _TEXT_TELEMETRY_EVENT_TYPES:
                for key in _TEXT_INDEX_KEYS:
                    value = detail.get(key)
                    assert value in (None, -1), f"text event leaks {key}={value!r} in batch {batch['batch_id']}"


def test_no_placeholder_sentinel_in_on_disk_artifacts(
    synthetic_dir: Path, windows_parquet: Path, dataset_dir: Path
) -> None:
    """The drop-all-text placeholder never appears in any pipeline artifact."""
    # Raw batch JSON files.
    for path in (synthetic_dir / "devices").rglob("*.json"):
        assert _PLACEHOLDER_SENTINEL not in path.read_text(encoding="utf-8")
    # Envelope files (if present).
    env_dir = synthetic_dir / "envelopes"
    if env_dir.is_dir():
        for path in env_dir.glob("*.json"):
            assert _PLACEHOLDER_SENTINEL not in path.read_text(encoding="utf-8")
    # Parquet artifacts: no string cell equals / contains the sentinel.
    for parquet in [windows_parquet] + [dataset_dir / f"{s}.parquet" for s in ("train", "val", "test")]:
        frame = pd.read_parquet(parquet)
        for col in frame.select_dtypes(include=["object"]).columns:
            joined = frame[col].astype(str)
            assert not joined.str.contains(_PLACEHOLDER_SENTINEL, regex=False).any(), f"{col} contains sentinel"


def test_windows_parquet_string_columns_are_structural(windows_parquet: Path) -> None:
    """The windows parquet's string columns are ids / JSON-of-structure only."""
    frame = pd.read_parquet(windows_parquet)
    # weak_label_top1 is always a scenario code.
    assert frame["weak_label_top1"].astype(str).isin(_VALID_TASKS).all()
    # JSON columns decode to lists (never free text).
    for _, cell in frame["weak_label_probs_json"].head(5).items():
        parsed = json.loads(cell)
        assert isinstance(parsed, list) and len(parsed) == len(SCENARIOS)
    for _, cell in frame["quality_flags_json"].head(5).items():
        assert isinstance(json.loads(cell), list)
