"""Dataset builder: windows.parquet -> {train,val,test}.parquet + manifests.

``build_dataset`` is the single S3 entry point (build contract §11): it loads the
preprocessed windows, applies a leakage-free split protocol
(:mod:`research.datasets.splits`), samples matched impostors for the test split
(:mod:`research.datasets.impostors`), then writes

* ``data/datasets/{name}/{train,val,test}.parquet`` — the split window rows,
  re-projected to exactly the active ``feature_mode`` columns (plus id / label /
  weak-label metadata),
* ``data/datasets/{name}/impostor_pairs.parquet`` — the matched genuine/impostor
  test pairs,
* ``data/datasets/{name}/split_manifest.json`` — the §3d manifest, whose
  ``leakage_check`` block MUST be all-True (the build asserts this and raises
  otherwise),
* ``data/datasets/{name}/feature_manifest.json`` — the models' input contract
  (``input_dim = len(feature_columns)``; never hardcoded downstream).

The dataset ``name`` defaults to ``{protocol}__{feature_mode}``.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from research import LEAKAGE_COLUMNS, SCENARIOS
from research.datasets.impostors import ImpostorPairs, sample_matched_impostors
from research.datasets.splits import (
    APP_COL,
    DAY_COL,
    SESSION_COL,
    USER_COL,
    WINDOW_COL,
    SplitResult,
    make_split,
)
from research.preprocessing.feature_extractors import (
    build_feature_columns,
    build_feature_manifest,
)
from research.utils.logging import get_logger

LOGGER = get_logger("research.datasets.builders")

# Non-feature columns preserved in every split parquet (build contract §3a).
_ID_COLUMNS = [
    "device_id",
    SESSION_COL,
    DAY_COL,
    WINDOW_COL,
    USER_COL,
    APP_COL,
    "start_elapsed_ns",
    "end_elapsed_ns",
    "start_wall_ms",
    "end_wall_ms",
]
_LABEL_COLUMNS = [
    "weak_label_top1",
    "weak_label_topk_json",
    "weak_label_probs_json",
    "weak_label_confidence",
    "weak_label_entropy",
    "weak_label_low_confidence",
    "quality_flags_json",
    "task_category",
    "raw_task_category",
]


def _load_windows(windows_parquet: str | Path) -> pd.DataFrame:
    """Load the preprocessed windows with a clean 0..N-1 integer index.

    Args:
        windows_parquet: Path to ``windows.parquet``.

    Returns:
        The window table indexed by row position.

    Raises:
        FileNotFoundError: If the parquet does not exist.
    """
    path = Path(windows_parquet)
    if not path.exists():
        raise FileNotFoundError(f"windows parquet not found: {path}")
    df = pd.read_parquet(path)
    return df.reset_index(drop=True)


def _project_columns(df: pd.DataFrame, feature_columns: list[str]) -> pd.DataFrame:
    """Return id + label + active feature columns, in a stable order.

    Missing feature columns (e.g. a mode whose feature was not materialised by
    preprocessing) are filled with ``0.0`` so the frame always matches the
    manifest's ``input_dim``. Leakage columns are never included.

    Args:
        df: A slice of the window table.
        feature_columns: The active feature-mode columns.

    Returns:
        The projected DataFrame.
    """
    keep_ids = [c for c in _ID_COLUMNS if c in df.columns]
    keep_labels = [c for c in _LABEL_COLUMNS if c in df.columns]
    meta = df[keep_ids + keep_labels]
    # Build the feature block in a single frame to avoid fragmented inserts;
    # missing columns are filled with 0.0 so the frame matches the manifest.
    feature_data = {
        col: (df[col].astype(float).to_numpy() if col in df.columns else 0.0) for col in feature_columns
    }
    features = pd.DataFrame(feature_data, index=df.index, columns=feature_columns)
    return pd.concat([meta.reset_index(drop=True), features.reset_index(drop=True)], axis=1)


def _weak_label_distribution(df: pd.DataFrame) -> dict[str, int]:
    """Count windows per weak-label top1 scenario (all 7 keys present).

    Args:
        df: A split's window frame.

    Returns:
        Mapping scenario id -> count (0 for unseen scenarios).
    """
    counts = Counter(df["weak_label_top1"].astype(str).tolist()) if "weak_label_top1" in df else Counter()
    return {scene: int(counts.get(scene, 0)) for scene in SCENARIOS}


def _category_distribution(df: pd.DataFrame, column: str) -> dict[str, int]:
    """Count a category column, returning an empty dict when absent.

    Args:
        df: The frame to count.
        column: The column name.

    Returns:
        Mapping category value -> count.
    """
    if column not in df:
        return {}
    counts = Counter(str(v) for v in df[column].dropna().tolist())
    return {key: int(value) for key, value in sorted(counts.items())}


def _sessions_of(df: pd.DataFrame, idx: list[int]) -> set[str]:
    """Return the set of session ids present in a row-index subset."""
    return set(df.loc[idx, SESSION_COL].astype(str)) if idx else set()


def _users_of(df: pd.DataFrame, idx: list[int]) -> set[str]:
    """Return the set of user ids present in a row-index subset."""
    return set(df.loc[idx, USER_COL].astype(str)) if idx else set()


def _build_impostors(df: pd.DataFrame, split: SplitResult, seed: int, n_per_genuine: int) -> ImpostorPairs:
    """Sample matched impostors for the test split, per-user-disjoint pools.

    For each tested genuine user, the impostor candidate pool is every window
    from OTHER users across the whole dataset (train ∪ val ∪ test), so the pool
    is user-level disjoint from the attacked user by construction.

    Args:
        df: The full window table.
        split: The split result (test rows are attacked).
        seed: Deterministic sampling salt.
        n_per_genuine: Impostor windows per genuine test window.

    Returns:
        The matched :class:`ImpostorPairs`.
    """
    all_idx = list(df.index)
    return sample_matched_impostors(
        df,
        genuine_idx=split.test_idx,
        pool_idx=all_idx,
        seed=seed,
        n_per_genuine=n_per_genuine,
    )


def _leakage_check(df: pd.DataFrame, split: SplitResult, impostors: ImpostorPairs) -> dict[str, bool]:
    """Compute the §3d leakage-check block for a split.

    Args:
        df: The full window table.
        split: The split result.
        impostors: The sampled matched impostors.

    Returns:
        A mapping of leakage-check name -> bool (all must be True).
    """
    train_s = _sessions_of(df, split.train_idx)
    val_s = _sessions_of(df, split.val_idx)
    test_s = _sessions_of(df, split.test_idx)
    train_d = set(df.loc[split.train_idx, DAY_COL].astype(str)) if split.train_idx else set()
    test_d = set(df.loc[split.test_idx, DAY_COL].astype(str)) if split.test_idx else set()
    train_a = set(df.loc[split.train_idx, APP_COL].astype(str)) if split.train_idx else set()
    test_a = set(df.loc[split.test_idx, APP_COL].astype(str)) if split.test_idx else set()

    # Enroll = train ∪ val sessions (prototype source); query = test sessions.
    enroll_s = train_s | val_s

    no_session_leak = train_s.isdisjoint(test_s) and val_s.isdisjoint(test_s)
    # Day / app leakage only asserted for the protocols that hold those axes out.
    if DAY_COL in split.group_cols:
        no_day_leak = train_d.isdisjoint(test_d)
    else:
        no_day_leak = True
    if APP_COL in split.group_cols:
        no_app_leak = train_a.isdisjoint(test_a)
    else:
        no_app_leak = True

    return {
        "no_session_leak": bool(no_session_leak),
        "no_day_leak": bool(no_day_leak),
        "no_app_leak": bool(no_app_leak),
        "enroll_query_sessions_disjoint": bool(enroll_s.isdisjoint(test_s)),
        "impostor_pool_user_disjoint": bool(impostors.impostor_pool_disjoint()),
    }


def build_dataset(
    windows_parquet: str | Path,
    protocol: str,
    out_dir: str | Path,
    feature_mode: str = "ui_sensor",
    *,
    seed: int = 42,
    n_impostor_per_genuine: int = 1,
    name: str | None = None,
) -> Path:
    """Build a leakage-checked dataset from preprocessed windows.

    Args:
        windows_parquet: Path to ``data/processed/windows.parquet`` (a file) or a
            directory containing it.
        protocol: Split protocol name (see :data:`research.datasets.splits.PROTOCOLS`).
        out_dir: Root output dir; the dataset is written under ``out_dir/{name}``.
        feature_mode: Feature mode selecting the active feature columns.
        seed: Deterministic split / impostor-sampling salt.
        n_impostor_per_genuine: Impostor windows sampled per genuine test window.
        name: Dataset dir name; defaults to ``{protocol}__{feature_mode}``.

    Returns:
        The dataset directory path.

    Raises:
        AssertionError: If any ``leakage_check`` is False, or if any emitted
            feature column collides with a leakage column.
    """
    src = Path(windows_parquet)
    if src.is_dir():
        src = src / "windows.parquet"
    df = _load_windows(src)

    feature_columns = build_feature_columns(feature_mode)
    leaked = sorted(set(feature_columns) & LEAKAGE_COLUMNS)
    assert not leaked, f"feature_mode {feature_mode!r} would emit leakage columns: {leaked}"

    split = make_split(df, protocol, seed=seed)
    impostors = _build_impostors(df, split, seed=seed, n_per_genuine=n_impostor_per_genuine)
    leakage_check = _leakage_check(df, split, impostors)

    dataset_name = name or f"{protocol}__{feature_mode}"
    ds_dir = Path(out_dir) / dataset_name
    ds_dir.mkdir(parents=True, exist_ok=True)

    # Write the split parquet files (projected to the active feature columns).
    split_frames = {
        "train": _project_columns(df.loc[split.train_idx], feature_columns),
        "val": _project_columns(df.loc[split.val_idx], feature_columns),
        "test": _project_columns(df.loc[split.test_idx], feature_columns),
    }
    for split_name, frame in split_frames.items():
        frame.to_parquet(ds_dir / f"{split_name}.parquet", index=False)
    impostors.to_frame().to_parquet(ds_dir / "impostor_pairs.parquet", index=False)

    # Feature manifest (models read input_dim from here — never hardcoded).
    feature_manifest = build_feature_manifest(feature_mode)
    (ds_dir / "feature_manifest.json").write_text(
        json.dumps(feature_manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Observability for degenerate (single-user / single-day) builds. The build
    # must still SUCCEED (the smoke pipeline depends on it) but must not look
    # validated when it is not: expose n_users, whether any impostor pairs exist,
    # whether the impostor-pool leakage check was vacuously True (all([]) is True
    # when there are 0 pairs), and any split fallback notes — as explicit warnings.
    users = sorted(set(df[USER_COL].astype(str)))
    n_users = len(users)
    has_impostor_pairs = len(impostors) > 0
    impostor_pool_check_vacuous = not has_impostor_pairs
    warnings: list[str] = list(split.notes)
    if n_users < 2:
        warnings.append("single_user_dataset")
    if not has_impostor_pairs:
        warnings.append(
            "no_impostor_pairs_single_user_dataset"
            if n_users < 2
            else "no_impostor_pairs_no_cross_user_scene_match"
        )
    for warning in warnings:
        LOGGER.warning("dataset %s: %s", dataset_name, warning)

    # Split manifest (§3d) — assert every leakage check True.
    manifest: dict[str, Any] = {
        "protocol": protocol,
        "feature_mode": feature_mode,
        "scene_taxonomy": "I0..I6",
        "dataset_name": dataset_name,
        "seed": int(seed),
        "input_dim": feature_manifest["input_dim"],
        "users": users,
        "n_users": n_users,
        "has_impostor_pairs": bool(has_impostor_pairs),
        "impostor_pool_check_vacuous": bool(impostor_pool_check_vacuous),
        "warnings": warnings,
        "devices": sorted(set(df["device_id"].astype(str))) if "device_id" in df else [],
        "sessions": sorted(set(df[SESSION_COL].astype(str))),
        "days": sorted(set(df[DAY_COL].astype(str))),
        "package_buckets": sorted(set(df[APP_COL].astype(str))),
        "n_windows_train": len(split.train_idx),
        "n_windows_val": len(split.val_idx),
        "n_windows_test": len(split.test_idx),
        "weak_label_distribution": {
            "train": _weak_label_distribution(split_frames["train"]),
            "val": _weak_label_distribution(split_frames["val"]),
            "test": _weak_label_distribution(split_frames["test"]),
        },
        "task_category_distribution": {
            "train": _category_distribution(split_frames["train"], "task_category"),
            "val": _category_distribution(split_frames["val"], "task_category"),
            "test": _category_distribution(split_frames["test"], "task_category"),
        },
        "raw_task_category_distribution": {
            "train": _category_distribution(split_frames["train"], "raw_task_category"),
            "val": _category_distribution(split_frames["val"], "raw_task_category"),
            "test": _category_distribution(split_frames["test"], "raw_task_category"),
        },
        "n_genuine_pairs": len(impostors),
        "n_impostor_pairs": len(impostors),
        "n_impostor_exact_matches": int(sum(impostors.matched_exact)),
        "train_users": sorted(_users_of(df, split.train_idx)),
        "test_users": sorted(_users_of(df, split.test_idx)),
        "leakage_check": leakage_check,
        "kstar_selection_split": "val",
    }
    (ds_dir / "split_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    failed = [name for name, ok in leakage_check.items() if not ok]
    assert not failed, f"leakage checks failed for {dataset_name!r}: {failed} (manifest: {manifest['leakage_check']})"
    return ds_dir
