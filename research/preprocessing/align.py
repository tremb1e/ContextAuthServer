"""Batch alignment: flatten sensor samples across batches into one sorted frame.

The synthetic / ingest on-disk layout stores one batch per file. Each batch
carries an interleaved list of 3-channel sensor samples whose
``timestamp_elapsed_nanos`` is relative to the batch's ``base_elapsed_nanos``
(see ``_recon_contract.md`` §b/§c). This module produces a single tidy pandas
DataFrame of ALL sensor samples across a run, sorted by
``(device_id, timestamp_elapsed_nanos)``, while keeping the per-batch events /
node snapshots accessible via :func:`index_batches`.

It also detects "clock jumps" — points where the elapsed clock moves backwards
or leaps forward far more than the sampling period would explain — which mark a
service restart and are consumed by :mod:`research.preprocessing.sessionize`.

Nothing here computes or stores any leakage column; only raw sensor axes,
timestamps, and provenance ids land in the frame.
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np
import pandas as pd

from research import SENSOR_TYPES

#: Columns of the aligned sensor sample frame (stable order).
SENSOR_FRAME_COLUMNS: list[str] = [
    "device_id",
    "batch_id",
    "session_id",
    "app_package_name",
    "collection_source",
    "task_category",
    "sensor_type",
    "timestamp_elapsed_nanos",
    "wall_time_estimated_millis",
    "x",
    "y",
    "z",
    "base_elapsed_nanos",
    "started_at_wall_millis",
    "batch_order",
]

#: Nanoseconds per second (elapsed clock unit is nanoseconds).
_NS_PER_SEC = 1_000_000_000


def attach_base_elapsed_nanos(batch: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of ``batch`` with a resolved ``base_elapsed_nanos``.

    The base is normally present on the batch. If a batch omits it (defensive),
    it is inferred as the minimum sensor ``timestamp_elapsed_nanos`` so that the
    per-sample elapsed axis remains well-defined.

    Args:
        batch: A raw batch dict.

    Returns:
        A shallow-copied batch dict guaranteed to carry an int
        ``base_elapsed_nanos``.
    """
    resolved = dict(batch)
    base = resolved.get("base_elapsed_nanos")
    if base is None:
        samples = resolved.get("sensor_samples") or []
        base = min((int(s.get("timestamp_elapsed_nanos", 0)) for s in samples), default=0)
    resolved["base_elapsed_nanos"] = int(base)
    return resolved


def align_batches(batches: Iterable[dict[str, Any]]) -> pd.DataFrame:
    """Flatten sensor samples across batches into a sorted tidy DataFrame.

    Every sensor sample from every batch becomes one row, tagged with its
    batch/session/device provenance. The batch is the atomic unit: rows are
    sorted by ``(device_id, batch_order, timestamp_elapsed_nanos)`` where
    ``batch_order`` is the per-device wall-clock order of the batch (by
    ``started_at_wall_millis``). This keeps each batch's samples CONTIGUOUS and
    in elapsed order, and orders batches by wall time — the elapsed clock is
    per-batch (its ``base_elapsed_nanos`` origin resets each batch), so a global
    elapsed sort would spuriously interleave overlapping batches. Wall time is
    the reliable cross-batch axis (``_recon_spec.md`` §6.1: elapsed within,
    wall for day/session grouping).

    Args:
        batches: Iterable of raw batch dicts (e.g. from ``load_batches``).

    Returns:
        A DataFrame with :data:`SENSOR_FRAME_COLUMNS`. Empty (with the right
        columns) if no samples exist.
    """
    materialized = [attach_base_elapsed_nanos(b) for b in batches]

    # Per-device batch ordering keyed by start wall time, then batch_id (tie
    # break). The batch is atomic; we never re-order samples across batches.
    per_device: dict[str, list[tuple[int, str]]] = {}
    for batch in materialized:
        device_id = str(batch.get("device_id", ""))
        started = int(batch.get("started_at_wall_millis", 0))
        per_device.setdefault(device_id, []).append((started, str(batch.get("batch_id", ""))))
    batch_order_map: dict[tuple[str, str], int] = {}
    for device_id, entries in per_device.items():
        for order_index, (_, batch_id) in enumerate(sorted(entries)):
            batch_order_map[(device_id, batch_id)] = order_index

    rows: list[dict[str, Any]] = []
    for batch in materialized:
        device_id = str(batch.get("device_id", ""))
        batch_id = str(batch.get("batch_id", ""))
        session_id = str(batch.get("session_id", ""))
        package = str(batch.get("app_package_name", "unknown"))
        source = str(batch.get("collection_source", ""))
        task_category = batch.get("task_category")
        base = int(batch.get("base_elapsed_nanos", 0))
        started = int(batch.get("started_at_wall_millis", 0))
        order_index = batch_order_map.get((device_id, batch_id), 0)
        for sample in batch.get("sensor_samples") or []:
            rows.append(
                {
                    "device_id": device_id,
                    "batch_id": batch_id,
                    "session_id": session_id,
                    "app_package_name": package,
                    "collection_source": source,
                    "task_category": task_category,
                    "sensor_type": str(sample.get("sensor_type", "")),
                    "timestamp_elapsed_nanos": int(sample.get("timestamp_elapsed_nanos", 0)),
                    "wall_time_estimated_millis": int(sample.get("wall_time_estimated_millis", 0)),
                    "x": float(sample.get("x", 0.0)),
                    "y": float(sample.get("y", 0.0)),
                    "z": float(sample.get("z", 0.0)),
                    "base_elapsed_nanos": base,
                    "started_at_wall_millis": started,
                    "batch_order": order_index,
                }
            )

    if not rows:
        return pd.DataFrame(columns=SENSOR_FRAME_COLUMNS)

    frame = pd.DataFrame(rows, columns=SENSOR_FRAME_COLUMNS)
    frame = frame.sort_values(
        ["device_id", "batch_order", "timestamp_elapsed_nanos", "sensor_type"],
        kind="mergesort",
    ).reset_index(drop=True)
    return frame


def detect_clock_jumps(frame: pd.DataFrame, *, max_gap_sec: float = 600.0) -> pd.DataFrame:
    """Annotate the sensor frame with per-device time-gap / restart flags.

    The frame is assumed batch-contiguous, wall-ordered per device (as produced
    by :func:`align_batches`). Two boundary signals are detected:

    * **time_gap** — the WALL-clock gap to the previous row exceeds
      ``max_gap_sec``. Wall time is the reliable cross-batch axis (the elapsed
      clock resets per batch), and within a batch the wall gap is one sample
      period, so this fires only at large inter-batch gaps.
    * **clock_backward** — a *service restart*, detected at batch boundaries
      where the batch's ``base_elapsed_nanos`` decreases relative to the
      previous batch on the same device (the elapsed realtime clock only goes
      backwards across a process restart).

    Args:
        frame: An aligned sensor frame from :func:`align_batches`.
        max_gap_sec: Wall-gap threshold (seconds) above which a row starts a new
            time segment. Defaults to 600s (10 min), matching the session rule.

    Returns:
        A copy of ``frame`` with added columns ``wall_gap_ms:int`` (0 at each
        device's first row), ``clock_backward:bool`` and ``time_gap:bool``.
    """
    if frame.empty:
        out = frame.copy()
        out["wall_gap_ms"] = pd.Series(dtype="int64")
        out["clock_backward"] = pd.Series(dtype="bool")
        out["time_gap"] = pd.Series(dtype="bool")
        return out

    out = frame.copy()
    prev_wall = out.groupby("device_id", sort=False)["wall_time_estimated_millis"].shift(1)
    wall_gap = (out["wall_time_estimated_millis"] - prev_wall).fillna(0).astype("int64")
    out["wall_gap_ms"] = wall_gap
    out["time_gap"] = wall_gap > int(max_gap_sec * 1000)

    # Restart: at a batch boundary, the batch base_elapsed decreased.
    prev_batch = out.groupby("device_id", sort=False)["batch_id"].shift(1)
    prev_base = out.groupby("device_id", sort=False)["base_elapsed_nanos"].shift(1)
    is_boundary = out["batch_id"] != prev_batch
    base_backward = out["base_elapsed_nanos"] < prev_base
    out["clock_backward"] = (is_boundary & base_backward).fillna(False).astype(bool)
    return out


def index_batches(batches: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build a ``batch_id -> batch`` index keeping events / nodes accessible.

    The aligned sensor frame intentionally carries only raw sensor rows; UI
    events, node snapshots and touch events stay on the batch object. This index
    lets windowing pull a window's events/nodes by ``batch_id`` without
    re-reading files.

    Args:
        batches: Iterable of raw batch dicts.

    Returns:
        A dict mapping ``batch_id`` to the (base-resolved) batch dict.
    """
    return {str(b.get("batch_id", "")): attach_base_elapsed_nanos(b) for b in batches}


def channel_presence(frame: pd.DataFrame) -> dict[str, bool]:
    """Report which of the 3 IMU channels appear anywhere in ``frame``.

    Args:
        frame: An aligned sensor frame.

    Returns:
        A dict mapping each of :data:`research.SENSOR_TYPES` to a bool.
    """
    present = set(frame["sensor_type"].unique()) if not frame.empty else set()
    return {sensor_type: sensor_type in present for sensor_type in SENSOR_TYPES}


def elapsed_to_seconds(elapsed_nanos: np.ndarray | pd.Series, base_elapsed_nanos: int) -> np.ndarray:
    """Convert elapsed nanoseconds to seconds relative to a base.

    Args:
        elapsed_nanos: Array/series of ``timestamp_elapsed_nanos`` values.
        base_elapsed_nanos: The origin to subtract before scaling.

    Returns:
        A float ``numpy`` array of seconds.
    """
    values = np.asarray(elapsed_nanos, dtype=np.float64)
    return (values - float(base_elapsed_nanos)) / float(_NS_PER_SEC)
