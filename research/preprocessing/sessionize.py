"""Sessionization: assign ``session_id`` / ``day_id`` and cut on breaks.

Given the aligned per-sample sensor frame (see :mod:`research.preprocessing.align`),
this module segments each device's stream into sessions. A session boundary is
cut whenever ANY of the following holds between consecutive samples of a device
(``_recon_spec.md`` §6.1):

* inter-sample **gap > gap_min** (default 10 minutes) on the elapsed clock;
* a **day boundary** crossing (UTC calendar day of the wall clock changes);
* a **service restart** — detected as a backward jump of the elapsed clock
  (``timestamp_elapsed_nanos`` decreases), which only happens across a process
  restart because the elapsed clock is monotonic within a process.

The batch's own ``session_id`` is respected as an additional hard boundary (a
new uploaded session always starts a new analysis session), but the gap/day/
restart rules can further split a single uploaded session.

The output is a stable ``session_id`` string
``"{device_id}:{day_id}:{seg}"`` plus a ``day_id`` (UTC ``YYYY-MM-DD``) per row.
No leakage columns are read or produced.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np
import pandas as pd

from research.preprocessing.align import detect_clock_jumps

#: Nanoseconds per minute.
_NS_PER_MIN = 60 * 1_000_000_000


def _day_id_from_wall_millis(wall_millis: int) -> str:
    """UTC ``YYYY-MM-DD`` day id for a wall-clock ms timestamp.

    Matches ``app.storage`` date-dir derivation (``time.gmtime``), so analysis
    days line up with on-disk date directories.

    Args:
        wall_millis: Wall-clock time in milliseconds.

    Returns:
        A ``YYYY-MM-DD`` string.
    """
    return time.strftime("%Y-%m-%d", time.gmtime(int(wall_millis) / 1000.0))


def sessionize(
    frame: pd.DataFrame,
    *,
    gap_min: float = 10.0,
) -> pd.DataFrame:
    """Assign ``session_id`` / ``day_id`` to an aligned sensor frame.

    Rows must already be sorted by ``(device_id, timestamp_elapsed_nanos)`` (as
    produced by :func:`research.preprocessing.align.align_batches`). A new
    session id is emitted at every boundary (gap / day / restart / new uploaded
    ``session_id``). Session ids are stable strings scoped per device+day.

    Args:
        frame: Aligned per-sample sensor frame. Must contain ``device_id``,
            ``timestamp_elapsed_nanos``, ``wall_time_estimated_millis`` and the
            uploaded ``session_id`` column.
        gap_min: Elapsed-gap threshold in minutes; a larger inter-sample gap
            cuts a new session.

    Returns:
        A copy of ``frame`` with columns ``day_id:str``, ``session_id:str``
        (analysis session; overwrites the uploaded one), ``uploaded_session_id``
        (the original), ``service_restart:bool`` and ``session_gap:bool``
        (per-row boundary markers, True on the first row of a new segment for
        the restart/gap reasons respectively).
    """
    if frame.empty:
        out = frame.copy()
        for col, dtype in (
            ("day_id", "object"),
            ("uploaded_session_id", "object"),
            ("session_id", "object"),
            ("service_restart", "bool"),
            ("session_gap", "bool"),
        ):
            out[col] = pd.Series(dtype=dtype)
        return out

    annotated = detect_clock_jumps(frame, max_gap_sec=gap_min * 60.0)
    out = annotated.copy()

    day_ids = [_day_id_from_wall_millis(w) for w in out["wall_time_estimated_millis"].tolist()]
    out["day_id"] = day_ids
    out["uploaded_session_id"] = out["session_id"].astype(str)

    devices = out["device_id"].tolist()
    uploaded = out["uploaded_session_id"].tolist()
    clock_backward = out["clock_backward"].tolist()
    big_gap = out["time_gap"].tolist()

    session_ids: list[str] = []
    service_restart: list[bool] = []
    session_gap: list[bool] = []

    prev_device: str | None = None
    prev_day: str | None = None
    prev_uploaded: str | None = None
    seg_index = -1
    current_day_for_seg: str | None = None

    for i in range(len(out)):
        device = devices[i]
        day = day_ids[i]
        up = uploaded[i]

        new_device = device != prev_device
        new_day = new_device or (day != prev_day)
        new_uploaded = new_device or (up != prev_uploaded)
        restart = bool(clock_backward[i]) and not new_device
        gap = bool(big_gap[i]) and not new_device

        boundary = new_device or new_day or new_uploaded or restart or gap
        if boundary:
            if new_device or new_day or current_day_for_seg != day:
                seg_index = 0
                current_day_for_seg = day
            else:
                seg_index += 1

        session_ids.append(f"{device}:{day}:{seg_index}")
        service_restart.append(restart)
        session_gap.append(gap)

        prev_device, prev_day, prev_uploaded = device, day, up

    out["session_id"] = session_ids
    out["service_restart"] = service_restart
    out["session_gap"] = session_gap
    return out


def session_summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Summarize sessionized rows into one row per ``(device_id, session_id)``.

    Args:
        frame: A sessionized sensor frame (output of :func:`sessionize`).

    Returns:
        A DataFrame with columns ``device_id, session_id, day_id, n_samples,
        start_elapsed_ns, end_elapsed_ns, start_wall_ms, end_wall_ms,
        n_batches`` (one row per analysis session). Empty (right columns) when
        the input is empty.
    """
    columns = [
        "device_id",
        "session_id",
        "day_id",
        "n_samples",
        "start_elapsed_ns",
        "end_elapsed_ns",
        "start_wall_ms",
        "end_wall_ms",
        "n_batches",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    grouped = frame.groupby(["device_id", "session_id"], sort=True)
    records: list[dict[str, Any]] = []
    for (device_id, session_id), sub in grouped:
        records.append(
            {
                "device_id": device_id,
                "session_id": session_id,
                "day_id": sub["day_id"].iloc[0],
                "n_samples": int(len(sub)),
                "start_elapsed_ns": int(sub["timestamp_elapsed_nanos"].min()),
                "end_elapsed_ns": int(sub["timestamp_elapsed_nanos"].max()),
                "start_wall_ms": int(sub["wall_time_estimated_millis"].min()),
                "end_wall_ms": int(sub["wall_time_estimated_millis"].max()),
                "n_batches": int(sub["batch_id"].nunique()),
            }
        )
    return pd.DataFrame.from_records(records, columns=columns)
