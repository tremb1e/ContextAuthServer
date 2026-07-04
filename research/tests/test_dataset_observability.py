"""Degenerate-dataset observability — P0-3 + P2-c.

A single-user (no cross-user impostor pairs) or single-day (leave_day_out cannot
hold the day axis out) build must still SUCCEED — the smoke pipeline depends on
it — but must not silently look validated. These tests assert the
``split_manifest.json`` now exposes:

* ``n_users`` / ``has_impostor_pairs`` / ``impostor_pool_check_vacuous`` (the
  impostor-pool leakage check is vacuously True when there are 0 pairs), and a
  ``warnings`` list naming the degeneracy;
* the leave_day_out -> leave_session_out fallback on single-day data;

and that a normal MULTI-user, multi-day dataset carries NONE of these warnings
and has real impostor pairs.
"""

from __future__ import annotations

import json
from pathlib import Path

from research.datasets.builders import build_dataset
from research.scripts.generate_synthetic_data import generate
from research.scripts.run_preprocess import run_preprocess


def _windows_for(root: Path, *, users: int, days: int, sessions_per_day: int, seed: int = 7) -> Path:
    """Generate a tiny synthetic run and preprocess it to a windows.parquet."""
    synthetic = root / "synthetic"
    processed = root / "processed"
    generate(users=users, days=days, sessions_per_day=sessions_per_day, out=synthetic, seed=seed)
    run_preprocess(synthetic, processed, window_size_sec=5.0, stride_sec=1.0, feature_mode="ui_sensor")
    return processed / "windows.parquet"


def _manifest(ds_dir: Path) -> dict:
    """Load a built dataset's split manifest."""
    return json.loads((ds_dir / "split_manifest.json").read_text(encoding="utf-8"))


def test_single_user_build_succeeds_and_warns(tmp_path: Path) -> None:
    """A single-user dataset builds, but flags no impostor pairs explicitly."""
    windows = _windows_for(tmp_path / "one_user", users=1, days=2, sessions_per_day=2)
    ds_dir = build_dataset(windows, protocol="leave_session_out", out_dir=tmp_path / "ds1", feature_mode="ui_sensor")
    manifest = _manifest(ds_dir)

    # Build still passes the leakage assertion (all True — impostor check vacuous).
    assert all(manifest["leakage_check"].values())
    assert manifest["n_users"] == 1
    assert manifest["has_impostor_pairs"] is False
    assert manifest["impostor_pool_check_vacuous"] is True
    assert "single_user_dataset" in manifest["warnings"]
    assert "no_impostor_pairs_single_user_dataset" in manifest["warnings"]
    assert manifest["n_impostor_pairs"] == 0


def test_single_day_leave_day_out_flags_fallback(tmp_path: Path) -> None:
    """leave_day_out on single-day data falls back to leave_session_out + warns."""
    windows = _windows_for(tmp_path / "one_day", users=3, days=1, sessions_per_day=3)
    ds_dir = build_dataset(windows, protocol="leave_day_out", out_dir=tmp_path / "dsd", feature_mode="ui_sensor")
    manifest = _manifest(ds_dir)

    assert all(manifest["leakage_check"].values())
    assert "leave_day_out_fell_back_to_leave_session_out" in manifest["warnings"]
    # Multi-user, so the single-user warnings must NOT be present.
    assert "single_user_dataset" not in manifest["warnings"]


def test_multi_user_dataset_has_no_degeneracy_warnings(split_manifest: dict) -> None:
    """The normal 5-user fixture dataset carries real pairs and no warnings."""
    assert split_manifest["n_users"] >= 2
    assert split_manifest["has_impostor_pairs"] is True
    assert split_manifest["impostor_pool_check_vacuous"] is False
    assert split_manifest["warnings"] == []
