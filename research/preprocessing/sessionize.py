"""Sessionization: assign ``session_id`` / ``day_id`` and cut on breaks.

Given the aligned per-sample sensor frame (see :mod:`research.preprocessing.align`),
this module segments each device's stream into sessions. A session boundary is
cut whenever ANY of the following holds between consecutive samples of a device
(``_recon_spec.md`` §6.1):

* inter-sample **gap > gap_min** (default 10 minutes) on the elapsed clock;
* a **day boundary** crossing (study-timezone calendar day of the wall clock
  changes — see :func:`_day_id_from_wall_millis` / SRV-12);
* a **service restart** — detected as a backward jump of the elapsed clock
  (``timestamp_elapsed_nanos`` decreases), which only happens across a process
  restart because the elapsed clock is monotonic within a process;
* a **foreground app change** — the batch-level ``app_package_name`` differs from
  the previous sample's (SRV-2). Without this, a single uploaded session that
  switches app mid-stream would carry windows from several apps under one
  ``package_bucket``, corrupting ``leave_app_out`` and the package feature.

The batch's own ``session_id`` is respected as an additional hard boundary (a
new uploaded session always starts a new analysis session), but the gap/day/
restart/app rules can further split a single uploaded session.

The output is a stable ``session_id`` string
``"{device_id}:{day_id}:{seg}"`` plus a ``day_id`` (study-timezone
``YYYY-MM-DD``) per row. No leakage columns are read or produced.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from research.preprocessing.align import detect_clock_jumps

#: Nanoseconds per minute.
_NS_PER_MIN = 60 * 1_000_000_000

#: Default study timezone (SRV-12). The research 'day' (``day_id`` and the
#: ``leave_day_out`` axis) is the participant's LOCAL calendar day in this zone,
#: not UTC. The server's on-disk date directories (``app.storage``) stay UTC by
#: design; the two can differ for a session crossing the local midnight (16:00
#: UTC for UTC+8). ``day_id`` is authoritative for the research day axis.
DEFAULT_STUDY_TIMEZONE = "Asia/Shanghai"


def _day_id_from_wall_millis(wall_millis: int, tz: ZoneInfo) -> str:
    """Study-timezone ``YYYY-MM-DD`` day id for a wall-clock ms timestamp.

    Uses the study timezone (default Asia/Shanghai) so the research 'day' is the
    participant's local calendar day. This deliberately DIFFERS from
    ``app.storage``'s UTC date-dir derivation: a session straddling the local
    midnight would otherwise be split on the wrong boundary, and two local
    evenings either side of 16:00 UTC would merge into one UTC day (SRV-12).

    Args:
        wall_millis: Wall-clock time in milliseconds.
        tz: The study timezone.

    Returns:
        A ``YYYY-MM-DD`` string in the study timezone.
    """
    return datetime.fromtimestamp(int(wall_millis) / 1000.0, tz=tz).strftime("%Y-%m-%d")


def sessionize(
    frame: pd.DataFrame,
    *,
    gap_min: float = 10.0,
    study_timezone: str = DEFAULT_STUDY_TIMEZONE,
) -> pd.DataFrame:
    """Assign ``session_id`` / ``day_id`` to an aligned sensor frame.

    Rows must already be sorted by ``(device_id, timestamp_elapsed_nanos)`` (as
    produced by :func:`research.preprocessing.align.align_batches`). A new
    session id is emitted at every boundary (gap / day / restart / foreground app
    change / new uploaded ``session_id``). Session ids are stable strings scoped
    per device+day.

    Args:
        frame: Aligned per-sample sensor frame. Must contain ``device_id``,
            ``timestamp_elapsed_nanos``, ``wall_time_estimated_millis``,
            ``app_package_name`` and the uploaded ``session_id`` column.
        gap_min: Elapsed-gap threshold in minutes; a larger inter-sample gap
            cuts a new session.
        study_timezone: IANA timezone name for the ``day_id`` calendar day
            (SRV-12; default Asia/Shanghai).

    Returns:
        A copy of ``frame`` with columns ``day_id:str``, ``session_id:str``
        (analysis session; overwrites the uploaded one), ``uploaded_session_id``
        (the original), ``service_restart:bool``, ``session_gap:bool`` and
        ``app_change:bool`` (per-row boundary markers, True on the first row of a
        new segment for the restart / gap / app-change reasons respectively).
    """
    if frame.empty:
        out = frame.copy()
        for col, dtype in (
            ("day_id", "object"),
            ("uploaded_session_id", "object"),
            ("session_id", "object"),
            ("service_restart", "bool"),
            ("session_gap", "bool"),
            ("app_change", "bool"),
        ):
            out[col] = pd.Series(dtype=dtype)
        return out

    tz = ZoneInfo(study_timezone)
    annotated = detect_clock_jumps(frame, max_gap_sec=gap_min * 60.0)
    out = annotated.copy()

    day_ids = [_day_id_from_wall_millis(w, tz) for w in out["wall_time_estimated_millis"].tolist()]
    out["day_id"] = day_ids
    out["uploaded_session_id"] = out["session_id"].astype(str)

    devices = out["device_id"].tolist()
    uploaded = out["uploaded_session_id"].tolist()
    clock_backward = out["clock_backward"].tolist()
    big_gap = out["time_gap"].tolist()
    # Batch-level foreground package per sample; align always provides it.
    packages = (
        out["app_package_name"].astype(str).tolist()
        if "app_package_name" in out.columns
        else ["" for _ in range(len(out))]
    )

    session_ids: list[str] = []
    service_restart: list[bool] = []
    session_gap: list[bool] = []
    app_change: list[bool] = []

    prev_device: str | None = None
    prev_day: str | None = None
    prev_uploaded: str | None = None
    prev_pkg: str | None = None
    seg_index = -1
    current_day_for_seg: str | None = None

    for i in range(len(out)):
        device = devices[i]
        day = day_ids[i]
        up = uploaded[i]
        pkg = packages[i]

        new_device = device != prev_device
        new_day = new_device or (day != prev_day)
        new_uploaded = new_device or (up != prev_uploaded)
        restart = bool(clock_backward[i]) and not new_device
        gap = bool(big_gap[i]) and not new_device
        # Unconditional foreground-app-change boundary (SRV-2). Empty for the
        # first row of each device (prev_pkg is None). BUILTIN gold sessions are
        # single-package, so this is a no-op there; it only splits mixed
        # THIRD_PARTY sessions.
        new_app = (not new_device) and (prev_pkg is not None) and (pkg != prev_pkg)

        boundary = new_device or new_day or new_uploaded or restart or gap or new_app
        if boundary:
            if new_device or new_day or current_day_for_seg != day:
                seg_index = 0
                current_day_for_seg = day
            else:
                seg_index += 1

        session_ids.append(f"{device}:{day}:{seg_index}")
        service_restart.append(restart)
        session_gap.append(gap)
        app_change.append(new_app)

        prev_device, prev_day, prev_uploaded, prev_pkg = device, day, up, pkg

    out["session_id"] = session_ids
    out["service_restart"] = service_restart
    out["session_gap"] = session_gap
    out["app_change"] = app_change
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
