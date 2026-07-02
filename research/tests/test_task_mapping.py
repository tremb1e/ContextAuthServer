"""Task taxonomy compatibility: raw app task ids map to canonical scenes."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from research import canonical_scene_for_task
from research.preprocessing.loaders import load_batches
from research.scripts.run_preprocess import run_preprocess


def test_canonical_scene_for_current_app_i_tasks() -> None:
    """The current Android app's I0..I7 taxonomy maps into C0..C6."""
    assert canonical_scene_for_task("I0") == "C0"
    assert canonical_scene_for_task("I1") == "C1"
    assert canonical_scene_for_task("I2") == "C3"
    assert canonical_scene_for_task("I3") == "C2"
    assert canonical_scene_for_task("I4") == "C2"
    assert canonical_scene_for_task("I5") == "C6"
    assert canonical_scene_for_task("I6") == "C6"
    assert canonical_scene_for_task("I7") == "C6"


def test_preprocess_preserves_raw_task_and_writes_canonical_scene(synthetic_dir: Path, tmp_path: Path) -> None:
    """Preprocessing keeps raw_task_category and maps task_category to C scenes."""
    batch = next(iter(load_batches(synthetic_dir)))
    batch = json.loads(json.dumps(batch))
    batch["task_category"] = "I2"
    batch["task_id"] = "I2"
    batch["task_sequence"] = 2
    batch["task_name"] = "Discrete taps and controls"
    batch["task_intuitive_description"] = "Discrete touch"
    for feature in batch["context_features"]:
        feature["task_category"] = "I2"
        feature["task_id"] = "I2"
        feature["task_sequence"] = 2
        feature["task_name"] = batch["task_name"]
        feature["task_intuitive_description"] = batch["task_intuitive_description"]

    input_dir = tmp_path / "input"
    date_dir = input_dir / "devices" / batch["device_id"] / "2026-01-01"
    date_dir.mkdir(parents=True)
    (date_dir / f"{batch['batch_id']}.json").write_text(json.dumps(batch), encoding="utf-8")

    out = tmp_path / "processed"
    run_preprocess(input_dir, out, window_size_sec=5.0, stride_sec=1.0, feature_mode="ui_sensor")
    windows = pd.read_parquet(out / "windows.parquet")
    assert not windows.empty
    assert set(windows["raw_task_category"].dropna()) == {"I2"}
    assert set(windows["task_category"].dropna()) == {"C3"}
