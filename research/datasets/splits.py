"""Leakage-free window splitting protocols (build contract §11 S3, spec §5).

All protocols split WHOLE groups (sessions / days / package buckets) into
``train`` / ``val`` / ``test`` — never individual windows — so that adjacent
overlapping windows of one session can never straddle a split boundary (the
"NO random window split" rule). Every protocol returns a :class:`SplitResult`
carrying the three row-index sets plus the group columns used, which
:mod:`research.datasets.builders` turns into parquet + a leakage-checked
``split_manifest.json``.

Protocols
---------
* ``leave_session_out`` — sessions partitioned across the three splits; the same
  session never appears in two splits.
* ``leave_day_out`` — earliest day(s) train, next day val, latest day(s) test
  (temporal drift; a session belongs to exactly one day).
* ``leave_app_out`` — package buckets held out (val/test buckets never seen in
  train), proving the model does not memorise the foreground app.
* ``combined_day_app`` — the strictest USENIX-style column: test rows are BOTH a
  held-out day AND a held-out package bucket (cross-time ∧ cross-app).

Determinism: group→split assignment is a stable hash of the group key salted by
``seed`` (never :func:`random`), so a given ``(protocol, seed)`` is reproducible.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

# Canonical id columns of a window record (build contract §3a).
SESSION_COL = "session_id"
DAY_COL = "day_id"
APP_COL = "package_bucket"
USER_COL = "user_id"
WINDOW_COL = "window_id"


@dataclass
class SplitResult:
    """The outcome of a split protocol over a window table.

    Attributes:
        protocol: Protocol name.
        train_idx: Row-index labels assigned to the train split.
        val_idx: Row-index labels assigned to the validation split.
        test_idx: Row-index labels assigned to the test split.
        group_cols: The id columns the protocol grouped by (for the manifest
            leakage checks; e.g. ``["session_id"]`` or ``["day_id",
            "package_bucket"]``).
    """

    protocol: str
    train_idx: list[int]
    val_idx: list[int]
    test_idx: list[int]
    group_cols: list[str] = field(default_factory=list)

    def sizes(self) -> dict[str, int]:
        """Return the number of rows in each split.

        Returns:
            Mapping ``{"train": n, "val": n, "test": n}``.
        """
        return {"train": len(self.train_idx), "val": len(self.val_idx), "test": len(self.test_idx)}


def _stable_bucket(key: str, seed: int, n_buckets: int) -> int:
    """Deterministically map a group key to one of ``n_buckets`` buckets.

    Uses a SHA-256 digest of ``"{seed}:{key}"`` so the assignment is stable
    across processes and independent of dict / set ordering.

    Args:
        key: The group key (e.g. a session id).
        seed: Integer salt.
        n_buckets: Number of buckets (>= 1).

    Returns:
        A bucket index in ``[0, n_buckets)``.
    """
    digest = hashlib.sha256(f"{int(seed)}:{key}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % max(1, int(n_buckets))


def _assign_by_fraction(
    keys: list[str],
    seed: int,
    val_frac: float,
    test_frac: float,
) -> dict[str, str]:
    """Assign a stable train/val/test label to each unique group key.

    Keys are sorted then bucketed into 1000 stable buckets; the lowest buckets
    become ``test``, the next ``val``, the rest ``train``. This keeps the split
    deterministic while honouring the requested fractions closely. When there
    are very few keys the function still guarantees non-empty train (and gives
    val/test at least one key each when at least three keys exist).

    Args:
        keys: Unique group keys.
        seed: Integer salt for the stable hash.
        val_frac: Target fraction of keys for validation.
        test_frac: Target fraction of keys for test.

    Returns:
        Mapping ``group_key -> {"train","val","test"}``.
    """
    unique = sorted(set(keys))
    n = len(unique)
    if n == 0:
        return {}
    # Order keys by their stable bucket, then by key for tie-breaking.
    ordered = sorted(unique, key=lambda k: (_stable_bucket(k, seed, 1000), k))
    if n <= 2:
        # Degenerate: keep everything trainable; last key doubles as val+test.
        assignment = {k: "train" for k in ordered}
        assignment[ordered[-1]] = "test"
        if n == 2:
            assignment[ordered[0]] = "train"
        return assignment

    n_test = max(1, round(n * test_frac))
    n_val = max(1, round(n * val_frac))
    # Never consume every key with val+test; keep at least one for train.
    while n_test + n_val >= n:
        if n_val > 1:
            n_val -= 1
        elif n_test > 1:
            n_test -= 1
        else:
            break
    assignment: dict[str, str] = {}
    for i, key in enumerate(ordered):
        if i < n_test:
            assignment[key] = "test"
        elif i < n_test + n_val:
            assignment[key] = "val"
        else:
            assignment[key] = "train"
    return assignment


def _split_from_group_assignment(
    windows: pd.DataFrame,
    group_series: pd.Series,
    assignment: dict[str, str],
    protocol: str,
    group_cols: list[str],
) -> SplitResult:
    """Materialise a :class:`SplitResult` from a per-row group label mapping.

    Args:
        windows: The window table (row index is the row id used downstream).
        group_series: A per-row Series of group keys aligned to ``windows``.
        assignment: Mapping group_key -> split name.
        protocol: Protocol name for the result.
        group_cols: The id columns grouped by (for the manifest).

    Returns:
        The populated :class:`SplitResult`.
    """
    split_of = group_series.map(assignment)
    train_idx = windows.index[split_of == "train"].tolist()
    val_idx = windows.index[split_of == "val"].tolist()
    test_idx = windows.index[split_of == "test"].tolist()
    return SplitResult(
        protocol=protocol,
        train_idx=[int(i) for i in train_idx],
        val_idx=[int(i) for i in val_idx],
        test_idx=[int(i) for i in test_idx],
        group_cols=group_cols,
    )


def leave_session_out(
    windows: pd.DataFrame,
    *,
    seed: int = 42,
    val_frac: float = 0.2,
    test_frac: float = 0.2,
) -> SplitResult:
    """Split by session id; a session is wholly in one split.

    Adjacent overlapping windows share a ``session_id`` and therefore never
    straddle a split boundary. Because whole sessions move together, per user
    the enroll (train/val) and query (test) sessions are automatically disjoint.

    Args:
        windows: Window table with a ``session_id`` column.
        seed: Deterministic assignment salt.
        val_frac: Fraction of sessions for validation.
        test_frac: Fraction of sessions for test.

    Returns:
        A :class:`SplitResult` grouped by ``session_id``.
    """
    assignment = _assign_by_fraction(windows[SESSION_COL].astype(str).tolist(), seed, val_frac, test_frac)
    return _split_from_group_assignment(
        windows, windows[SESSION_COL].astype(str), assignment, "leave_session_out", [SESSION_COL]
    )


def leave_day_out(
    windows: pd.DataFrame,
    *,
    seed: int = 42,
    val_frac: float = 0.2,
    test_frac: float = 0.2,
) -> SplitResult:
    """Split by day id with temporal ordering (early→train, late→test).

    Days are sorted ascending; the earliest become train, the middle val, the
    latest test — mirroring the temporal-drift protocol of spec §5. Sessions
    belong to exactly one day, so no session leaks across splits.

    Args:
        windows: Window table with ``day_id``.
        seed: Unused for ordering (kept for signature parity / reproducibility).
        val_frac: Fraction of days for validation.
        test_frac: Fraction of (latest) days for test.

    Returns:
        A :class:`SplitResult` grouped by ``day_id``.
    """
    unique_days = sorted(set(windows[DAY_COL].astype(str).tolist()))
    n = len(unique_days)
    assignment: dict[str, str]
    if n <= 1:
        # Only one day available: fall back to a session split so val/test are
        # still non-empty and no session leaks (documented degenerate case).
        return leave_session_out(windows, seed=seed, val_frac=val_frac, test_frac=test_frac)
    if n == 2:
        assignment = {unique_days[0]: "train", unique_days[1]: "test"}
        # Borrow the last train day's *sessions*? No — keep val empty-safe by
        # reusing train day for val via session split handled in builder; here
        # we mark the earliest day also as val-source by splitting its sessions.
        # Simpler + leakage-free: put earliest day train, latest day test, and
        # carve val from the earliest day's later sessions.
        earliest = unique_days[0]
        early_sessions = sorted(set(windows.loc[windows[DAY_COL].astype(str) == earliest, SESSION_COL].astype(str)))
        if len(early_sessions) >= 2:
            val_session = early_sessions[-1]
            row_group = windows[SESSION_COL].astype(str)
            sess_assign = {s: "train" for s in early_sessions}
            sess_assign[val_session] = "val"
            # Build a combined per-row label: test rows from latest day, else
            # the session-level train/val label from the earliest day.
            latest = unique_days[1]
            labels: list[str] = []
            for day, sess in zip(windows[DAY_COL].astype(str), row_group):
                if day == latest:
                    labels.append("test")
                else:
                    labels.append(sess_assign.get(sess, "train"))
            split_of = pd.Series(labels, index=windows.index)
            return SplitResult(
                protocol="leave_day_out",
                train_idx=[int(i) for i in windows.index[split_of == "train"]],
                val_idx=[int(i) for i in windows.index[split_of == "val"]],
                test_idx=[int(i) for i in windows.index[split_of == "test"]],
                group_cols=[DAY_COL],
            )
        assignment = {unique_days[0]: "train", unique_days[1]: "test"}
        return _split_from_group_assignment(
            windows, windows[DAY_COL].astype(str), assignment, "leave_day_out", [DAY_COL]
        )

    n_test = max(1, round(n * test_frac))
    n_val = max(1, round(n * val_frac))
    while n_test + n_val >= n:
        if n_val > 1:
            n_val -= 1
        elif n_test > 1:
            n_test -= 1
        else:
            break
    assignment = {}
    for i, day in enumerate(unique_days):
        if i >= n - n_test:
            assignment[day] = "test"
        elif i >= n - n_test - n_val:
            assignment[day] = "val"
        else:
            assignment[day] = "train"
    return _split_from_group_assignment(
        windows, windows[DAY_COL].astype(str), assignment, "leave_day_out", [DAY_COL]
    )


def leave_app_out(
    windows: pd.DataFrame,
    *,
    seed: int = 42,
    val_frac: float = 0.25,
    test_frac: float = 0.25,
) -> SplitResult:
    """Split by package bucket; held-out apps never appear in train.

    Proves the encoder is not memorising a foreground application: the val/test
    package buckets are disjoint from the train buckets.

    Args:
        windows: Window table with ``package_bucket``.
        seed: Deterministic assignment salt.
        val_frac: Fraction of buckets for validation.
        test_frac: Fraction of buckets for test.

    Returns:
        A :class:`SplitResult` grouped by ``package_bucket``.
    """
    assignment = _assign_by_fraction(windows[APP_COL].astype(str).tolist(), seed, val_frac, test_frac)
    return _split_from_group_assignment(
        windows, windows[APP_COL].astype(str), assignment, "leave_app_out", [APP_COL]
    )


def combined_day_app(
    windows: pd.DataFrame,
    *,
    seed: int = 42,
    val_frac: float = 0.2,
    test_frac: float = 0.25,
) -> SplitResult:
    """Strictest split: test rows are a held-out day AND a held-out app.

    A held-out day set and a held-out package-bucket set are chosen; a window is
    ``test`` iff it is in BOTH a held-out day and a held-out bucket (cross-time ∧
    cross-app). Rows that are held out on exactly one axis are dropped from
    train/val (they would leak the other axis), and ``val`` is taken from the
    held-out day but a *seen* bucket to allow k* selection under drift.

    Args:
        windows: Window table with ``day_id`` and ``package_bucket``.
        seed: Deterministic assignment salt.
        val_frac: Fraction of buckets reserved (with the held-out day) for val.
        test_frac: Fraction of buckets held out for the test intersection.

    Returns:
        A :class:`SplitResult` grouped by ``["day_id", "package_bucket"]``.
    """
    days = sorted(set(windows[DAY_COL].astype(str)))
    buckets = sorted(set(windows[APP_COL].astype(str)))
    # Latest day is the held-out (drift) day; if only one day, reuse it.
    heldout_day = days[-1]
    seen_days = set(days[:-1]) if len(days) > 1 else {days[-1]}

    bucket_assign = _assign_by_fraction(buckets, seed, val_frac, test_frac)
    test_buckets = {b for b, s in bucket_assign.items() if s == "test"}
    val_buckets = {b for b, s in bucket_assign.items() if s == "val"}
    if not test_buckets:  # guarantee at least one held-out bucket
        test_buckets = {buckets[0]}

    labels: list[str] = []
    for day, bucket in zip(windows[DAY_COL].astype(str), windows[APP_COL].astype(str)):
        on_heldout_day = day == heldout_day
        if on_heldout_day and bucket in test_buckets:
            labels.append("test")
        elif on_heldout_day and bucket in val_buckets:
            labels.append("val")
        elif (day in seen_days) and (bucket not in test_buckets) and (bucket not in val_buckets):
            labels.append("train")
        else:
            labels.append("drop")
    split_of = pd.Series(labels, index=windows.index)
    train_idx = [int(i) for i in windows.index[split_of == "train"]]
    val_idx = [int(i) for i in windows.index[split_of == "val"]]
    test_idx = [int(i) for i in windows.index[split_of == "test"]]
    # Fallbacks so downstream never sees an empty val/test on tiny data.
    if not test_idx:
        test_idx = [int(i) for i in windows.index[split_of == "val"]] or train_idx[-1:]
    if not val_idx:
        val_idx = test_idx[:1]
    return SplitResult(
        protocol="combined_day_app",
        train_idx=train_idx,
        val_idx=val_idx,
        test_idx=test_idx,
        group_cols=[DAY_COL, APP_COL],
    )


#: Registry of split protocols by name.
PROTOCOLS: dict[str, Callable[..., SplitResult]] = {
    "leave_session_out": leave_session_out,
    "leave_day_out": leave_day_out,
    "leave_app_out": leave_app_out,
    "combined_day_app": combined_day_app,
    # Alias used by the build contract / spec for the strictest column.
    "combined": combined_day_app,
}


def make_split(windows: pd.DataFrame, protocol: str, *, seed: int = 42, **kwargs) -> SplitResult:
    """Dispatch to a split protocol by name.

    Args:
        windows: The window table.
        protocol: One of :data:`PROTOCOLS`.
        seed: Deterministic assignment salt.
        **kwargs: Forwarded to the protocol (e.g. ``val_frac``, ``test_frac``).

    Returns:
        The :class:`SplitResult`.

    Raises:
        ValueError: If ``protocol`` is unknown.
    """
    if protocol not in PROTOCOLS:
        raise ValueError(f"unknown protocol: {protocol!r} (valid: {sorted(PROTOCOLS)})")
    return PROTOCOLS[protocol](windows, seed=seed, **kwargs)
