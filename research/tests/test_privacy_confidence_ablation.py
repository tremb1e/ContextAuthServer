"""Real privacy coarsening + confidence-threshold gate (SRV-10).

Asserts the KL confidence gate (0.0 is a no-op; higher thresholds zero the
low-confidence weight), and that the privacy dataset views are GENUINELY
different (value quantization + distinct column subsets) rather than the old
no-op that ran the same feature set three times.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch

from research.experiments.runner import _dataset_view
from research.models.losses import kl_weak
from research.preprocessing.feature_extractors import build_feature_columns


def test_kl_weak_confidence_gate() -> None:
    """thr=0.0 == legacy; thr gates out low-confidence windows; all-zero -> finite 0."""
    torch.manual_seed(0)
    logprobs = torch.log_softmax(torch.randn(4, 7), dim=-1)
    weak = torch.softmax(torch.randn(4, 7), dim=-1)
    conf = torch.tensor([0.1, 0.3, 0.5, 0.7])
    # thr=0.0 is bit-for-bit the ungated behaviour.
    assert torch.allclose(kl_weak(logprobs, weak, conf, 0.0), kl_weak(logprobs, weak, conf))
    # thr=0.4 keeps only windows with conf>=0.4 -> equals the ungated loss on {0.5,0.7}.
    gated = kl_weak(logprobs, weak, conf, 0.4)
    manual = kl_weak(logprobs[2:], weak[2:], conf[2:], 0.0)
    assert torch.allclose(gated, manual, atol=1e-6)
    # A threshold above every confidence -> all weight zero -> finite 0 (no NaN).
    allzero = kl_weak(logprobs, weak, conf, 0.9)
    assert torch.isfinite(allzero) and float(allzero) == 0.0


def _tiny_source_dataset(root: Path) -> Path:
    """Write a minimal ui_sensor_no_package dataset dir (with the needed UI cols)."""
    cols = build_feature_columns("ui_sensor_no_package")
    rng = np.random.default_rng(0)
    frame = pd.DataFrame({c: rng.random(12) for c in cols})
    frame["ui_bounds_occupancy"] = np.linspace(0.02, 0.98, 12)  # spread for quantization
    for extra in ("window_id", "user_id", "session_id", "day_id"):
        frame[extra] = [f"{extra}{i}" for i in range(12)]
    src = root / "src"
    src.mkdir(parents=True)
    for split in ("train", "val", "test"):
        frame.to_parquet(src / f"{split}.parquet", index=False)
    manifest = {"feature_columns": cols, "input_dim": len(cols), "package_columns": [], "feature_mode": "ui_sensor_no_package"}
    (src / "feature_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return src


def test_privacy_view_quantization_rewrites_values(tmp_path: Path) -> None:
    """quantize rewrites the parquet values (not a hard link) + records the transform."""
    src = _tiny_source_dataset(tmp_path)
    view = _dataset_view(src, tmp_path / "views", "privacy__coarse_bounds",
                         feature_mode="ui_sensor_no_package",
                         quantize={"ui_bounds_occupancy": 0.25}, drop_columns=["ui_treediff_boundsl1"])
    train = pd.read_parquet(view / "train.parquet")
    # Values are quantized to the 0.25 grid.
    assert set(np.round(train["ui_bounds_occupancy"].to_numpy(), 6)).issubset({0.0, 0.25, 0.5, 0.75, 1.0})
    # The parquet was rewritten (NOT hard-linked to the source inode).
    assert (view / "train.parquet").stat().st_ino != (src / "train.parquet").stat().st_ino
    manifest = json.loads((view / "feature_manifest.json").read_text(encoding="utf-8"))
    assert "privacy_transform" in manifest
    assert "ui_treediff_boundsl1" not in manifest["feature_columns"]


def test_privacy_levels_have_distinct_column_sets(tmp_path: Path) -> None:
    """The three privacy levels differ (column set and/or values) — no no-op."""
    src = _tiny_source_dataset(tmp_path)
    ui_cols = [c for c in build_feature_columns("ui_sensor_no_package") if c.startswith("ui_")]
    keep = {"ui_webview", "ui_list", "ui_form_like_control_count", "ui_treediff_categoryl1"}
    category_drop = [c for c in ui_cols if c not in keep]

    baseline = _dataset_view(src, tmp_path / "v", "no_resource_id", feature_mode="ui_sensor_no_package")
    category = _dataset_view(src, tmp_path / "v", "category_only", feature_mode="ui_sensor_no_package", drop_columns=category_drop)

    base_cols = json.loads((baseline / "feature_manifest.json").read_text(encoding="utf-8"))["feature_columns"]
    cat_cols = json.loads((category / "feature_manifest.json").read_text(encoding="utf-8"))["feature_columns"]
    # category-only keeps IMU + event + only the 4 category-derived UI columns.
    assert set(keep).issubset(set(cat_cols))
    assert len(cat_cols) == len(base_cols) - len(category_drop)
    assert set(cat_cols) != set(base_cols)
