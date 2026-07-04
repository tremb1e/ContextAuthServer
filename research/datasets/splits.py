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
        notes: Human-readable diagnostic flags about how the split was formed
            (e.g. a degenerate-data fallback). Surfaced verbatim into the dataset
            ``split_manifest.json`` ``warnings`` list so a silent fallback is
            observable downstream.
    """

    protocol: str
    train_idx: list[int]
    val_idx: list[int]
    test_idx: list[int]
    group_cols: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

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
    if n == 1:
        # Single group: keep it TRAIN (SRV-5 / SRV-16). An empty train would leave
        # the model no data; an empty val/test is tolerated downstream (the
        # evaluator guards empty test), so degrade on the safer side.
        return {ordered[0]: "train"}
    if n == 2:
        # Two groups: one train, one test (val borrows elsewhere / stays empty).
        return {ordered[0]: "train", ordered[1]: "test"}

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


def _assign_sessions_per_user(
    sessions_by_user: dict[str, set[str]],
    seed: int,
    val_frac: float,
    test_frac: float,
) -> tuple[dict[str, str], list[str]]:
    """Stratify each user's OWN sessions across train/val/test (SRV-5).

    A global session hash lets whole users strand entirely in test (silently
    dropped at eval — no enroll prototype) or entirely in train (never queried).
    Splitting per user guarantees every user with >= 2 sessions has both an
    enroll (train/val) and a query (test) session.

    Rules, per user with ``k`` sessions:

    * ``k >= 3``: ``n_test = max(1, round(k*test_frac))`` test, ``n_val`` val, the
      rest train (a shrink loop keeps >= 1 train);
    * ``k == 2``: 1 train + 1 test (val comes from other users);
    * ``k == 1``: all train + a ``user_single_session_not_testable`` note (a lone
      session cannot be both enrolled and queried, and must never sit in test
      alone — there would be no prototype).

    Args:
        sessions_by_user: Map user id -> set of that user's session ids.
        seed: Stable-hash salt (keeps ``(protocol, seed)`` reproducible).
        val_frac: Target validation fraction per user.
        test_frac: Target test fraction per user.

    Returns:
        ``(assignment, notes)`` where assignment maps session id -> split and
        notes flags single-session users.
    """
    assignment: dict[str, str] = {}
    notes: list[str] = []
    for user in sorted(sessions_by_user):
        ordered = sorted(sessions_by_user[user], key=lambda s: (_stable_bucket(s, seed, 1000), s))
        k = len(ordered)
        if k == 1:
            assignment[ordered[0]] = "train"
            notes.append(f"user_single_session_not_testable:{user}")
            continue
        if k == 2:
            assignment[ordered[0]] = "train"
            assignment[ordered[1]] = "test"
            continue
        n_test = max(1, round(k * test_frac))
        n_val = max(1, round(k * val_frac))
        while n_test + n_val >= k:  # keep >= 1 train
            if n_val > 1:
                n_val -= 1
            elif n_test > 1:
                n_test -= 1
            else:
                break
        for i, session in enumerate(ordered):
            if i < n_test:
                assignment[session] = "test"
            elif i < n_test + n_val:
                assignment[session] = "val"
            else:
                assignment[session] = "train"
    return assignment, notes


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
    """Split by session id, STRATIFIED PER USER; a session is wholly in one split.

    Adjacent overlapping windows share a ``session_id`` and therefore never
    straddle a split boundary. Sessions are partitioned WITHIN each user (SRV-5)
    so no user strands entirely in test (no enroll prototype -> silently dropped)
    or entirely in train (never queried); every user with >= 2 sessions gets both
    an enroll and a query session. Falls back to the legacy global split (with a
    note) when the table lacks a ``user_id`` column.

    Args:
        windows: Window table with ``session_id`` and ``user_id`` columns.
        seed: Deterministic assignment salt.
        val_frac: Fraction of sessions for validation.
        test_frac: Fraction of sessions for test.

    Returns:
        A :class:`SplitResult` grouped by ``session_id``.
    """
    if USER_COL not in windows.columns:
        assignment = _assign_by_fraction(windows[SESSION_COL].astype(str).tolist(), seed, val_frac, test_frac)
        result = _split_from_group_assignment(
            windows, windows[SESSION_COL].astype(str), assignment, "leave_session_out", [SESSION_COL]
        )
        result.notes.append("leave_session_out_no_user_col_global_fallback")
        return result

    pairs = windows[[USER_COL, SESSION_COL]].astype(str).drop_duplicates()
    sessions_by_user: dict[str, set[str]] = {}
    for user, session in zip(pairs[USER_COL], pairs[SESSION_COL]):
        sessions_by_user.setdefault(user, set()).add(session)
    assignment, notes = _assign_sessions_per_user(sessions_by_user, seed, val_frac, test_frac)
    result = _split_from_group_assignment(
        windows, windows[SESSION_COL].astype(str), assignment, "leave_session_out", [SESSION_COL]
    )
    result.notes.extend(notes)
    return result


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
        # still non-empty and no session leaks (documented degenerate case). Flag
        # the fallback so the manifest can record that the day axis was NOT held
        # out (otherwise a single-day dataset silently looks leave_day_out-valid).
        fallback = leave_session_out(windows, seed=seed, val_frac=val_frac, test_frac=test_frac)
        fallback.notes.append("leave_day_out_fell_back_to_leave_session_out")
        return fallback
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
    single_day = len(days) <= 1
    seen_days = set(days[:-1]) if len(days) > 1 else {days[-1]}

    notes: list[str] = []
    # On single-day data the day axis CANNOT be held out: this degrades to a pure
    # leave_app_out. Record it AND drop DAY_COL from group_cols so the builder does
    # not assert no_day_leak (train and test share the only day) — SRV-16.
    group_cols = [DAY_COL, APP_COL]
    if single_day:
        notes.append("combined_fell_back_to_app_only")
        group_cols = [APP_COL]

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
    # Degenerate-data fallbacks. NEVER borrow a train session into test: one
    # session in both train and test crashes the leakage assert with an opaque
    # error. Use val rows for an empty test, else leave test empty + warn. An
    # empty val may borrow from TRAIN (enroll side only -> no query leak). SRV-16.
    if not test_idx:
        if val_idx:
            test_idx, val_idx = val_idx, []
            notes.append("combined_empty_test_used_val_rows")
        else:
            notes.append("combined_empty_test_returned_empty")
    if not val_idx and train_idx:
        val_idx = train_idx[-1:]
        notes.append("combined_empty_val_borrowed_train")
    return SplitResult(
        protocol="combined_day_app",
        train_idx=train_idx,
        val_idx=val_idx,
        test_idx=test_idx,
        group_cols=group_cols,
        notes=notes,
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
