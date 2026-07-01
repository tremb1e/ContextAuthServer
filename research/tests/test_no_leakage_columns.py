"""No leakage columns anywhere in the ML feature path — §15.1.10.

Asserts the 4 leakage columns (``estimated_context_category``,
``game_like_score``, ``viewIdResourceName``, ``coarse_orientation``) never appear
in:

* the feature-column vocabulary of ANY feature mode (and each mode's manifest is
  marked ``leakage_free``);
* the built dataset's ``feature_manifest.json`` feature columns;
* the ``windows.parquet`` produced by preprocessing;
* any of the dataset split parquets (train/val/test).

The IMU-derived ``orient_landscape`` boolean IS allowed and is present in the
IMU-including modes.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from research import LEAKAGE_COLUMNS
from research.preprocessing.feature_extractors import (
    build_feature_columns,
    build_feature_manifest,
)

_ALL_MODES = (
    "sensor_only",
    "ui_sensor",
    "ui_sensor_no_package",
    "package_only",
    "ui_only",
    "privacy_coarse_ui",
)

# Exact set the contract requires excluded (build contract §2).
_EXPECTED_LEAKAGE = {
    "estimated_context_category",
    "game_like_score",
    "viewIdResourceName",
    "coarse_orientation",
}


def test_leakage_constant_matches_contract() -> None:
    """The frozen ``LEAKAGE_COLUMNS`` set matches the contract verbatim."""
    assert LEAKAGE_COLUMNS == _EXPECTED_LEAKAGE


def test_every_feature_mode_is_leakage_free() -> None:
    """No feature mode's column vocabulary intersects the leakage set."""
    for mode in _ALL_MODES:
        cols = set(build_feature_columns(mode))
        assert cols.isdisjoint(LEAKAGE_COLUMNS), f"{mode} leaks {cols & LEAKAGE_COLUMNS}"
        manifest = build_feature_manifest(mode)
        assert manifest["leakage_free"] is True
        assert set(manifest["feature_columns"]).isdisjoint(LEAKAGE_COLUMNS)
    # The ALLOWED IMU-derived orientation boolean is present in IMU modes.
    assert "orient_landscape" in build_feature_columns("sensor_only")


def test_dataset_feature_manifest_is_leakage_free(feature_manifest: dict) -> None:
    """The built dataset's feature manifest feature columns exclude leakage."""
    cols = set(feature_manifest["feature_columns"])
    assert cols.isdisjoint(LEAKAGE_COLUMNS)
    assert feature_manifest["leakage_free"] is True
    assert feature_manifest["input_dim"] == len(feature_manifest["feature_columns"])


def test_windows_parquet_has_no_leakage(windows_parquet: Path) -> None:
    """The preprocessed windows parquet contains no leakage column."""
    cols = set(pd.read_parquet(windows_parquet).columns)
    assert cols.isdisjoint(LEAKAGE_COLUMNS)


def test_split_parquets_have_no_leakage(dataset_dir: Path) -> None:
    """No train/val/test split parquet contains a leakage column."""
    for split in ("train", "val", "test"):
        cols = set(pd.read_parquet(dataset_dir / f"{split}.parquet").columns)
        assert cols.isdisjoint(LEAKAGE_COLUMNS), f"{split} leaks {cols & LEAKAGE_COLUMNS}"
