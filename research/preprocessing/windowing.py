"""Windowing: cut fixed-length sliding windows per session and build contexts.

Each analysis session (from :mod:`research.preprocessing.sessionize`) is sliced
into overlapping windows on the elapsed-time axis (default 5s window, 1s stride
⇒ 4s overlap, ``_recon_spec.md`` §6.2). A window context bundles everything the
feature extractor / weak labeler / quality flags need:

* provenance ids (``device_id, session_id, day_id, window_id, user_id,
  package_bucket``);
* time bounds in BOTH clocks (elapsed ns + wall ms);
* the raw IMU samples inside the window (already sorted, tidy DataFrame);
* the accessibility events inside the window (by wall time), pulled from the
  batch index;
* the node snapshots (the ``root_nodes`` of the in-window events) and the
  previous window's last snapshot (for tree-diff features).

``user_id`` == ``device_id`` (the only stable identity in the contract; there
is no user_id field — see ``_recon_spec.md`` §19). ``package_bucket`` is the
foreground ``app_package_name`` (used as the leave-app-out bucket). No leakage
column is read here.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

_NS_PER_SEC = 1_000_000_000

#: Guard inserted between concatenated batches on the session-relative axis so
#: consecutive batches never share a timestamp (one nominal 10ms sample period).
_INTER_BATCH_GUARD_NS = 10_000_000


def _events_for_batches(
    batch_ids: list[str],
    batch_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect and time-sort all context events for a set of batches.

    Args:
        batch_ids: Batch ids overlapping the session.
        batch_index: ``batch_id -> batch`` map (from ``align.index_batches``).

    Returns:
        A list of context-event dicts sorted by ``event_time_wall_millis``.
    """
    events: list[dict[str, Any]] = []
    for batch_id in batch_ids:
        batch = batch_index.get(batch_id)
        if batch is None:
            continue
        for event in batch.get("context_events") or []:
            events.append(event)
    events.sort(key=lambda e: int(e.get("event_time_wall_millis", 0)))
    return events


def _touch_for_batches(
    batch_ids: list[str],
    batch_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect and time-sort all touch events for a set of batches.

    Args:
        batch_ids: Batch ids overlapping the session.
        batch_index: ``batch_id -> batch`` map.

    Returns:
        A list of touch-event dicts sorted by ``event_time_wall_millis``.
    """
    touches: list[dict[str, Any]] = []
    for batch_id in batch_ids:
        batch = batch_index.get(batch_id)
        if batch is None:
            continue
        for touch in batch.get("touch_events") or []:
            touches.append(touch)
    touches.sort(key=lambda e: int(e.get("event_time_wall_millis", 0)))
    return touches


def _session_relative_axis(sub: pd.DataFrame) -> tuple[np.ndarray, int]:
    """Build a monotonic session-relative nanosecond axis over batches.

    Each batch's ``timestamp_elapsed_nanos`` is relative to that batch's own
    ``base_elapsed_nanos`` origin (which resets per batch). To window across a
    whole session we lay the batches end-to-end: batch ``b``'s samples are
    offset by the cumulative span of all earlier batches plus a one-sample
    inter-batch guard, so the axis is strictly increasing and each 5s batch
    occupies its own contiguous stretch.

    Args:
        sub: A session's rows, pre-sorted by ``(batch_order,
            timestamp_elapsed_nanos)``.

    Returns:
        A tuple ``(session_elapsed_ns, wall_anchor_ms)`` where the first is an
        ``int64`` array aligned to ``sub`` rows and the second is the session's
        earliest wall timestamp (ms).
    """
    elapsed = sub["timestamp_elapsed_nanos"].to_numpy(dtype=np.int64)
    orders = sub["batch_order"].to_numpy()
    wall_anchor = int(sub["wall_time_estimated_millis"].min())

    session_rel = np.zeros(elapsed.shape[0], dtype=np.int64)
    offset = 0
    prev_order: int | None = None
    prev_batch_start = 0
    prev_batch_end = 0
    for i in range(elapsed.shape[0]):
        order = int(orders[i])
        if prev_order is None:
            prev_order = order
            prev_batch_start = int(elapsed[i])
        elif order != prev_order:
            # New batch: advance the offset past the previous batch's span.
            offset += (prev_batch_end - prev_batch_start) + _INTER_BATCH_GUARD_NS
            prev_order = order
            prev_batch_start = int(elapsed[i])
        session_rel[i] = offset + (int(elapsed[i]) - prev_batch_start)
        prev_batch_end = int(elapsed[i])
    return session_rel, wall_anchor


def make_windows(
    session_stream: pd.DataFrame,
    batch_index: dict[str, dict[str, Any]],
    *,
    window_size_sec: float = 5.0,
    stride_sec: float = 1.0,
) -> list[dict[str, Any]]:
    """Build window contexts for every session in a sessionized frame.

    Windows are cut per ``(device_id, session_id)`` on the elapsed axis, from
    the session's first to last sample, stepping by ``stride_sec``. A window is
    emitted only if it contains at least one sensor sample. Events / node
    snapshots are attached by wall-time overlap with the window's wall bounds.

    Args:
        session_stream: A sessionized sensor frame (output of
            :func:`research.preprocessing.sessionize.sessionize`).
        batch_index: ``batch_id -> batch`` map (from ``align.index_batches``),
            used to pull events / node snapshots / touch events.
        window_size_sec: Window length in seconds.
        stride_sec: Window stride in seconds.

    Returns:
        A list of window-context dicts, each with keys:
        ``device_id, session_id, day_id, window_id, user_id, package_bucket,
        start_elapsed_ns, end_elapsed_ns, start_wall_ms, end_wall_ms,
        imu_samples (DataFrame), events (list), nodes_snapshots (list of node
        lists), prev_snapshot (list of nodes | None), touch_events (list),
        n_batches, service_restart (bool), session_gap (bool)``.

    Raises:
        ValueError: If ``window_size_sec`` or ``stride_sec`` is not positive.
    """
    if window_size_sec <= 0 or stride_sec <= 0:
        raise ValueError("window_size_sec and stride_sec must be positive")
    if session_stream.empty:
        return []

    window_ns = int(round(window_size_sec * _NS_PER_SEC))
    stride_ns = int(round(stride_sec * _NS_PER_SEC))

    contexts: list[dict[str, Any]] = []
    grouped = session_stream.groupby(["device_id", "session_id"], sort=True)

    for (device_id, session_id), sub in grouped:
        # Batch is atomic: order batches by (batch_order, elapsed) and build a
        # MONOTONIC session-relative timeline by offsetting each batch by the
        # cumulative span of prior batches. The raw elapsed clock resets per
        # batch (~same base each batch), so a single global elapsed sort would
        # overlap batches; the session-relative axis makes windows contiguous
        # and keeps each 5s scenario coherent.
        sub = sub.sort_values(["batch_order", "timestamp_elapsed_nanos"], kind="mergesort").reset_index(drop=True)
        day_id = str(sub["day_id"].iloc[0])
        package_bucket = str(sub["app_package_name"].mode().iloc[0]) if not sub.empty else "unknown"

        session_rel, wall_anchor = _session_relative_axis(sub)
        sub = sub.assign(session_elapsed_ns=session_rel)
        rel = sub["session_elapsed_ns"].to_numpy(dtype=np.int64)
        wall_ms_arr = sub["wall_time_estimated_millis"].to_numpy(dtype=np.int64)
        first_ns = int(rel[0])
        last_ns = int(rel[-1])

        batch_ids = list(dict.fromkeys(sub["batch_id"].tolist()))
        session_events = _events_for_batches(batch_ids, batch_index)
        session_touches = _touch_for_batches(batch_ids, batch_index)
        event_walls = np.array([int(e.get("event_time_wall_millis", 0)) for e in session_events], dtype=np.int64)
        touch_walls = np.array([int(t.get("event_time_wall_millis", 0)) for t in session_touches], dtype=np.int64)

        session_restart = bool(sub["service_restart"].any()) if "service_restart" in sub else False
        session_had_gap = bool(sub["session_gap"].any()) if "session_gap" in sub else False

        prev_snapshot: list[dict[str, Any]] | None = None
        window_index = 0
        start_ns = first_ns
        while start_ns <= last_ns:
            end_ns = start_ns + window_ns
            mask = (rel >= start_ns) & (rel < end_ns)
            if mask.any():
                imu_samples = sub.loc[mask].reset_index(drop=True)
                # Wall bounds from the actual in-window sample wall times (robust
                # since wall time is monotonic across the session).
                win_walls = wall_ms_arr[mask]
                start_wall = int(win_walls.min())
                end_wall = int(win_walls.max()) + 1

                if event_walls.size:
                    ev_mask = (event_walls >= start_wall) & (event_walls < end_wall)
                    window_events = [session_events[j] for j in np.nonzero(ev_mask)[0]]
                else:
                    window_events = []
                if touch_walls.size:
                    t_mask = (touch_walls >= start_wall) & (touch_walls < end_wall)
                    window_touches = [session_touches[j] for j in np.nonzero(t_mask)[0]]
                else:
                    window_touches = []

                nodes_snapshots = [e.get("root_nodes") or [] for e in window_events]
                window_id = f"{device_id}:{session_id}:{window_index}"
                contexts.append(
                    {
                        "device_id": device_id,
                        "session_id": session_id,
                        "day_id": day_id,
                        "window_id": window_id,
                        "user_id": device_id,  # device_id is the only stable identity
                        "package_bucket": package_bucket,
                        "start_elapsed_ns": int(start_ns),
                        "end_elapsed_ns": int(end_ns),
                        "start_wall_ms": int(start_wall),
                        "end_wall_ms": int(end_wall),
                        "imu_samples": imu_samples,
                        "events": window_events,
                        "nodes_snapshots": nodes_snapshots,
                        "prev_snapshot": prev_snapshot,
                        "touch_events": window_touches,
                        "n_batches": int(imu_samples["batch_id"].nunique()),
                        "service_restart": session_restart,
                        "session_gap": session_had_gap,
                        "window_duration_sec": float(window_size_sec),
                    }
                )
                if nodes_snapshots:
                    prev_snapshot = nodes_snapshots[-1]
                window_index += 1
            start_ns += stride_ns

    return contexts
