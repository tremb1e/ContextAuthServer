"""Dataset splits: leakage-free protocols + matched impostors — §15.1.5.

Asserts:

* the built ``leave_session_out`` dataset's ``split_manifest.leakage_check`` is
  ALL True (no session/day/app leak, enroll/query disjoint, impostor pool
  user-disjoint), and train/test sessions are actually disjoint on disk;
* the matched impostor pairs are cross-user and scene-matched
  (``impostor_pairs.parquet``);
* the other protocols (``leave_day_out``, ``leave_app_out``,
  ``combined_day_app``) build a valid, leakage-checked dataset too;
* no split parquet ever contains a leakage column.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from research import LEAKAGE_COLUMNS
from research.datasets.builders import build_dataset
from research.datasets.splits import PROTOCOLS


def _read_split(dataset_dir: Path, split: str) -> pd.DataFrame:
    """Load a split parquet from a dataset dir."""
    return pd.read_parquet(dataset_dir / f"{split}.parquet")


def test_leave_session_out_leakage_check_all_true(dataset_dir: Path, split_manifest: dict) -> None:
    """Every leakage-check flag in the built manifest is True."""
    leak = split_manifest["leakage_check"]
    assert leak, "leakage_check block must be present"
    assert all(leak.values()), f"leakage checks not all True: {leak}"
    assert split_manifest["kstar_selection_split"] == "val"


def test_train_test_sessions_disjoint_on_disk(dataset_dir: Path) -> None:
    """Train and test split parquets share no session id (whole-session split)."""
    train = _read_split(dataset_dir, "train")
    test = _read_split(dataset_dir, "test")
    train_sessions = set(train["session_id"].astype(str))
    test_sessions = set(test["session_id"].astype(str))
    assert train_sessions.isdisjoint(test_sessions)
    # Enroll (train ∪ val) vs query (test) disjoint too.
    val = _read_split(dataset_dir, "val")
    enroll = train_sessions | set(val["session_id"].astype(str))
    assert enroll.isdisjoint(test_sessions)


def test_no_leakage_columns_in_any_split(dataset_dir: Path) -> None:
    """No split parquet contains any of the 4 leakage columns."""
    for split in ("train", "val", "test"):
        cols = set(_read_split(dataset_dir, split).columns)
        assert cols.isdisjoint(LEAKAGE_COLUMNS), f"{split} leaks {cols & LEAKAGE_COLUMNS}"


def test_matched_impostors_cross_user_and_scene(dataset_dir: Path) -> None:
    """Impostor pairs are cross-user and carry the matched scene."""
    pairs = pd.read_parquet(dataset_dir / "impostor_pairs.parquet")
    assert len(pairs) > 0, "expected matched impostor pairs"
    assert (pairs["genuine_user_id"].astype(str) != pairs["impostor_user_id"].astype(str)).all()
    # Scene is one of I0..I6.
    assert pairs["scene"].astype(str).str.match(r"^I[0-6]$").all()


def test_other_protocols_build_and_pass_leakage(windows_parquet: Path, tmp_path: Path) -> None:
    """leave_day_out / leave_app_out / combined_day_app all build leakage-checked."""
    assert set(["leave_day_out", "leave_app_out", "combined_day_app"]).issubset(PROTOCOLS)
    for protocol in ("leave_day_out", "leave_app_out", "combined_day_app"):
        ds_dir = build_dataset(
            windows_parquet,
            protocol=protocol,
            out_dir=tmp_path / protocol,
            feature_mode="ui_sensor",
            seed=42,
            n_impostor_per_genuine=1,
        )
        manifest = json.loads((ds_dir / "split_manifest.json").read_text(encoding="utf-8"))
        assert manifest["protocol"] == protocol
        assert all(manifest["leakage_check"].values()), f"{protocol}: {manifest['leakage_check']}"
        # Each split parquet is leakage-free.
        for split in ("train", "val", "test"):
            cols = set(pd.read_parquet(ds_dir / f"{split}.parquet").columns)
            assert cols.isdisjoint(LEAKAGE_COLUMNS)
