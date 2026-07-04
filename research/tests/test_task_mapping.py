"""Task taxonomy: canonical I0..I6 gold + legacy remap of old on-disk ids.

The gold/scene space is the app's own 7 task classes ``I0..I6`` (identity, no
8->7 mapping). Legacy on-disk ids are digested by
:func:`research.canonical_scene_for_task` per ``00-common.md`` §3 — the 5 rules
below each get an assertion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from research import SCENARIOS, canonical_scene_for_task
from research.preprocessing.loaders import load_batches
from research.scripts.run_preprocess import run_preprocess


def test_canonical_identity_for_current_app_i_tasks() -> None:
    """The current app's I0..I5 map to themselves (identity gold)."""
    for scene in ("I0", "I1", "I2", "I3", "I4", "I5"):
        assert canonical_scene_for_task(scene) == scene


def test_legacy_remap_rule_i7_to_i6() -> None:
    """Rule 1: legacy I7 (old wrist rotation) -> new I6, unconditionally."""
    assert canonical_scene_for_task("I7") == "I6"
    assert canonical_scene_for_task("I7", "Wrist rotation") == "I6"
    assert canonical_scene_for_task("I7", "anything at all") == "I6"


def test_legacy_remap_rule_i6_scan_dropped() -> None:
    """Rule 2: legacy I6 with a spatial-capture "scan" name -> None (dropped)."""
    assert canonical_scene_for_task("I6", "Scan, frame, and capture") is None
    assert canonical_scene_for_task("I6", "扫描取景与拍摄") is None


def test_new_i6_wrist_is_gold() -> None:
    """Rule 3: I6 with a non-scan (wrist) name -> I6 (the new wrist task)."""
    assert canonical_scene_for_task("I6", "Wrist rotation") == "I6"
    assert canonical_scene_for_task("I6", "手腕转动") == "I6"
    # No task_name available -> default to the canonical I6 (wrist).
    assert canonical_scene_for_task("I6") == "I6"
    assert canonical_scene_for_task("I6", None) == "I6"


def test_legacy_remap_rule_c_categories_dropped() -> None:
    """Rule 4: retired C0..C6 ids -> None (no gold; C payloads removed from disk)."""
    for c in ("C0", "C1", "C2", "C3", "C4", "C5", "C6"):
        assert canonical_scene_for_task(c) is None
    # A specific C4 assertion (the spec's 5-rule list calls it out explicitly).
    assert canonical_scene_for_task("C4", "Multi-control operation") is None


def test_legacy_remap_rule_identity_and_unknown() -> None:
    """Rule 5: I0..I6 are gold; None / unknown ids (e.g. I8) -> None."""
    assert canonical_scene_for_task("I3") == "I3"
    assert canonical_scene_for_task(None) is None
    assert canonical_scene_for_task("I8") is None
    assert canonical_scene_for_task("garbage") is None


def _write_single_batch(base_batch: dict, tmp_path: Path, *, task_category: str, task_name: str) -> Path:
    """Write one BUILTIN batch with the given task id/name to a devices/ tree."""
    batch = json.loads(json.dumps(base_batch))
    batch["task_category"] = task_category
    batch["task_id"] = task_category
    batch["task_sequence"] = int(task_category[1:])
    batch["task_name"] = task_name
    batch["task_intuitive_description"] = "test"
    for feature in batch["context_features"]:
        feature["task_category"] = task_category
        feature["task_id"] = task_category
        feature["task_sequence"] = int(task_category[1:])
        feature["task_name"] = task_name
        feature["task_intuitive_description"] = "test"
    input_dir = tmp_path / "input"
    date_dir = input_dir / "devices" / batch["device_id"] / "2026-01-01"
    date_dir.mkdir(parents=True)
    (date_dir / f"{batch['batch_id']}.json").write_text(json.dumps(batch), encoding="utf-8")
    return input_dir


def test_preprocess_preserves_raw_task_and_writes_canonical_scene(synthetic_dir: Path, tmp_path: Path) -> None:
    """Preprocessing keeps raw_task_category and writes the canonical I0..I6 scene."""
    base = next(iter(load_batches(synthetic_dir)))
    input_dir = _write_single_batch(base, tmp_path, task_category="I2", task_name="Discrete taps and controls")

    out = tmp_path / "processed"
    # min_session_seconds=0: this single-batch fixture isolates task mapping, not
    # the APP-10-B short-session filter (a lone 5s batch spans ~4.99s < 5.0s).
    run_preprocess(
        input_dir, out, window_size_sec=5.0, stride_sec=1.0, feature_mode="ui_sensor", min_session_seconds=0.0
    )
    windows = pd.read_parquet(out / "windows.parquet")
    assert not windows.empty
    assert set(windows["raw_task_category"].dropna()) == {"I2"}
    assert set(windows["task_category"].dropna()) == {"I2"}


def test_preprocess_remaps_legacy_i7_to_i6(synthetic_dir: Path, tmp_path: Path) -> None:
    """A legacy I7 batch is preprocessed with gold scene I6 (raw kept as I7)."""
    base = next(iter(load_batches(synthetic_dir)))
    input_dir = _write_single_batch(base, tmp_path, task_category="I7", task_name="Wrist rotation")

    out = tmp_path / "processed_i7"
    # min_session_seconds=0: this single-batch fixture isolates task mapping, not
    # the APP-10-B short-session filter (a lone 5s batch spans ~4.99s < 5.0s).
    run_preprocess(
        input_dir, out, window_size_sec=5.0, stride_sec=1.0, feature_mode="ui_sensor", min_session_seconds=0.0
    )
    windows = pd.read_parquet(out / "windows.parquet")
    assert not windows.empty
    assert set(windows["raw_task_category"].dropna()) == {"I7"}
    assert set(windows["task_category"].dropna()) == {"I6"}


def test_preprocess_drops_legacy_scan_i6_from_gold(synthetic_dir: Path, tmp_path: Path) -> None:
    """A legacy spatial-capture I6 "scan" batch gets scene=None (not gold)."""
    base = next(iter(load_batches(synthetic_dir)))
    input_dir = _write_single_batch(base, tmp_path, task_category="I6", task_name="Scan, frame, and capture")

    out = tmp_path / "processed_scan"
    # min_session_seconds=0: this single-batch fixture isolates task mapping, not
    # the APP-10-B short-session filter (a lone 5s batch spans ~4.99s < 5.0s).
    run_preprocess(
        input_dir, out, window_size_sec=5.0, stride_sec=1.0, feature_mode="ui_sensor", min_session_seconds=0.0
    )
    windows = pd.read_parquet(out / "windows.parquet")
    assert not windows.empty
    assert set(windows["raw_task_category"].dropna()) == {"I6"}
    # scene is None for every window -> no gold rows at all.
    assert windows["task_category"].notna().sum() == 0


def test_scenarios_are_the_seven_i_classes() -> None:
    """The frozen scenario list is exactly I0..I6."""
    assert SCENARIOS == ["I0", "I1", "I2", "I3", "I4", "I5", "I6"]
