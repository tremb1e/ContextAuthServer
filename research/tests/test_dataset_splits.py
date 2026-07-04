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
from research.datasets.splits import (
    PROTOCOLS,
    _assign_by_fraction,
    combined_day_app,
    leave_session_out,
)


def _read_split(dataset_dir: Path, split: str) -> pd.DataFrame:
    """Load a split parquet from a dataset dir."""
    return pd.read_parquet(dataset_dir / f"{split}.parquet")


def _windows_df(sessions_per_user: dict[str, int], *, days: int = 1) -> pd.DataFrame:
    """A minimal window table: ``k`` sessions per user, a few windows each."""
    rows = []
    for user, k in sessions_per_user.items():
        for s in range(k):
            day = f"d{s % days}"
            session_id = f"{user}:{day}:{s}"
            for w in range(3):
                rows.append(
                    {
                        "user_id": user,
                        "session_id": session_id,
                        "day_id": day,
                        "package_bucket": "com.app",
                        "window_id": f"{session_id}:{w}",
                    }
                )
    return pd.DataFrame(rows).reset_index(drop=True)


def _user_sessions(df: pd.DataFrame, idx: list[int], user: str) -> set[str]:
    """Sessions of ``user`` present in a row-index subset."""
    sub = df.loc[idx]
    return set(sub[sub["user_id"] == user]["session_id"].astype(str))


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


def test_leave_session_out_per_user_coverage() -> None:
    """SRV-5: sessions are stratified WITHIN each user (no user strands one-sided)."""
    df = _windows_df({"uA": 4, "uB": 3, "uC": 2, "uD": 1})
    for seed in range(6):
        split = leave_session_out(df, seed=seed)
        train_s = set(df.loc[split.train_idx, "session_id"].astype(str))
        val_s = set(df.loc[split.val_idx, "session_id"].astype(str))
        test_s = set(df.loc[split.test_idx, "session_id"].astype(str))
        # Whole-session split: no session in two splits.
        assert train_s.isdisjoint(test_s) and val_s.isdisjoint(test_s)
        # Every >=2-session user has BOTH an enroll (train/val) and a query (test).
        for user in ("uA", "uB", "uC"):
            enroll = _user_sessions(df, split.train_idx, user) | _user_sessions(df, split.val_idx, user)
            query = _user_sessions(df, split.test_idx, user)
            assert enroll and query, f"user {user} not covered on both sides (seed {seed})"
        # The 1-session user is train-only and explicitly flagged (never in test).
        assert not _user_sessions(df, split.test_idx, "uD")
        assert "user_single_session_not_testable:uD" in split.notes


def test_leave_app_out_window_level_app_disjoint() -> None:
    """SRV-2: with package-pure windows, leave_app_out holds real apps out per window."""
    from research.datasets.splits import leave_app_out

    df = _windows_df({"uA": 3, "uB": 3, "uC": 3, "uD": 3})
    buckets = ["com.a", "com.b", "com.c", "com.d", "com.e"]
    sess_ids = sorted(df["session_id"].astype(str).unique())
    # One pure bucket per session -> each window's bucket IS its real app.
    bucket_of = {s: buckets[i % len(buckets)] for i, s in enumerate(sess_ids)}
    df["package_bucket"] = df["session_id"].astype(str).map(bucket_of)
    for seed in range(4):
        split = leave_app_out(df, seed=seed)
        train_a = set(df.loc[split.train_idx, "package_bucket"].astype(str))
        val_a = set(df.loc[split.val_idx, "package_bucket"].astype(str))
        test_a = set(df.loc[split.test_idx, "package_bucket"].astype(str))
        assert train_a.isdisjoint(test_a), f"train/test app leak (seed {seed}): {train_a & test_a}"
        assert train_a.isdisjoint(val_a) and val_a.isdisjoint(test_a)


def test_assign_by_fraction_single_group_is_train() -> None:
    """SRV-16: a lone group is assigned train (never test -> never an empty train)."""
    assert _assign_by_fraction(["only"], seed=1, val_frac=0.2, test_frac=0.2) == {"only": "train"}
    two = _assign_by_fraction(["a", "b"], seed=1, val_frac=0.2, test_frac=0.2)
    assert set(two.values()) == {"train", "test"}


def test_combined_day_app_single_day_falls_back_to_app_only() -> None:
    """SRV-16: single-day combined degrades to app-only (noted, no day-leak assert)."""
    df = _windows_df({"uA": 3, "uB": 3}, days=1)
    # Two package buckets so an app can actually be held out.
    df.loc[df.index % 2 == 0, "package_bucket"] = "com.app.b"
    split = combined_day_app(df, seed=3)
    assert "combined_fell_back_to_app_only" in split.notes
    # Day axis is NOT in group_cols on single-day data (so no_day_leak isn't asserted).
    assert "day_id" not in split.group_cols
    assert "package_bucket" in split.group_cols


def test_impostor_windows_from_test_split_only(dataset_dir: Path, split_manifest: dict) -> None:
    """SRV-6: impostor windows are drawn from the held-out test split only."""
    pairs = pd.read_parquet(dataset_dir / "impostor_pairs.parquet")
    test_windows = set(_read_split(dataset_dir, "test")["window_id"].astype(str))
    assert len(pairs) > 0, "expected matched impostor pairs from the test split"
    assert set(pairs["impostor_window_id"].astype(str)).issubset(test_windows)
    assert split_manifest["leakage_check"]["impostor_windows_test_only"] is True


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
