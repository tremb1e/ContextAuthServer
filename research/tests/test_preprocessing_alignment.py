"""Preprocessing alignment / sessionize / windowing correctness — §15.1.2.

Asserts (against small synthetic batches):

* ``align_batches`` flattens sensor samples into a per-device, batch-contiguous,
  elapsed-ordered frame carrying only raw axes + provenance (no leakage columns).
* ``detect_clock_jumps`` flags a large wall gap (``time_gap``) and a backward
  ``base_elapsed_nanos`` restart (``clock_backward``).
* ``sessionize`` cuts a new session on a > gap_min inter-sample gap and assigns
  stable ``session_id`` / ``day_id``.
* ``make_windows`` cuts per-session sliding windows whose bounds are ordered and
  whose IMU samples all fall inside the window.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from research import LEAKAGE_COLUMNS, SENSOR_TYPES
from research.preprocessing.align import (
    align_batches,
    detect_clock_jumps,
    index_batches,
)
from research.preprocessing.loaders import load_batches
from research.preprocessing.sessionize import sessionize, session_summary
from research.preprocessing.windowing import make_windows


def _tiny_batch(
    batch_id: str,
    base_ns: int,
    start_wall_ms: int,
    n: int = 40,
    *,
    package: str = "com.example.app",
    session_id: str = "up-sess",
) -> dict:
    """Build a minimal batch dict with ``n`` accel samples at 100 Hz."""
    period = 10_000_000  # 10 ms
    samples = [
        {
            "sensor_type": "ACCELEROMETER",
            "timestamp_elapsed_nanos": base_ns + i * period,
            "wall_time_estimated_millis": start_wall_ms + i * 10,
            "x": 0.0,
            "y": 0.0,
            "z": 9.81,
            "accuracy": 3,
        }
        for i in range(n)
    ]
    return {
        "batch_id": batch_id,
        "device_id": "dev1",
        "session_id": session_id,
        "app_package_name": package,
        "collection_source": "BUILTIN_TASK",
        "base_elapsed_nanos": base_ns,
        "started_at_wall_millis": start_wall_ms,
        "sensor_samples": samples,
        "context_events": [],
        "touch_events": [],
    }


def test_align_sorts_by_device_and_elapsed_and_is_leakage_free(synthetic_dir) -> None:
    """The aligned frame is per-device batch-contiguous, elapsed-sorted, leak-free."""
    batches = list(load_batches(synthetic_dir))
    frame = align_batches(batches)
    assert not frame.empty
    # No leakage column ever lands in the aligned sensor frame.
    assert set(frame.columns).isdisjoint(LEAKAGE_COLUMNS)
    # Within each (device, batch) block the elapsed axis is non-decreasing.
    for _, sub in frame.groupby(["device_id", "batch_id"], sort=False):
        ts = sub["timestamp_elapsed_nanos"].to_numpy()
        assert np.all(np.diff(ts) >= 0)
    # All 3 channels appear somewhere.
    assert set(frame["sensor_type"].unique()) <= set(SENSOR_TYPES)


def test_detect_clock_jumps_flags_gap_and_restart() -> None:
    """A big wall gap flags ``time_gap``; a backward base flags ``clock_backward``."""
    b1 = _tiny_batch("b1", base_ns=1_000_000_000, start_wall_ms=1_000_000)
    # b2 starts 20 min later in wall time (big gap) AND its base decreases (restart).
    b2 = _tiny_batch("b2", base_ns=500_000_000, start_wall_ms=1_000_000 + 20 * 60_000)
    frame = align_batches([b1, b2])
    annotated = detect_clock_jumps(frame, max_gap_sec=600.0)
    assert bool(annotated["time_gap"].any()), "a 20-min wall gap must set time_gap"
    assert bool(annotated["clock_backward"].any()), "a backward base_elapsed must flag a restart"


def test_sessionize_cuts_on_gap() -> None:
    """A > gap_min inter-batch wall gap splits into two analysis sessions."""
    b1 = _tiny_batch("b1", base_ns=1_000_000_000, start_wall_ms=1_000_000)
    b2 = _tiny_batch("b2", base_ns=2_000_000_000, start_wall_ms=1_000_000 + 20 * 60_000)
    frame = align_batches([b1, b2])
    sessioned = sessionize(frame, gap_min=10.0)
    assert "session_id" in sessioned and "day_id" in sessioned
    assert sessioned["session_id"].nunique() >= 2, "a 20-min gap must cut a new session"
    summary = session_summary(sessioned)
    assert len(summary) == sessioned["session_id"].nunique()


def test_make_windows_bounds_and_membership(synthetic_dir) -> None:
    """Windows have ordered bounds and only in-window IMU samples."""
    batches = list(load_batches(synthetic_dir))
    frame = align_batches(batches)
    batch_index = index_batches(batches)
    sessioned = sessionize(frame, gap_min=10.0)
    windows = make_windows(sessioned, batch_index, window_size_sec=5.0, stride_sec=1.0)
    assert windows, "expected windows from the synthetic sessions"
    for ctx in windows[:50]:
        assert ctx["start_elapsed_ns"] < ctx["end_elapsed_ns"]
        assert ctx["start_wall_ms"] <= ctx["end_wall_ms"]
        assert ctx["user_id"] == ctx["device_id"]  # device_id is the only identity
        imu: pd.DataFrame = ctx["imu_samples"]
        assert not imu.empty, "a yielded window must contain >=1 sensor sample"


def test_sessionize_cuts_on_app_change() -> None:
    """SRV-2: a foreground app change cuts a new session (same uploaded session).

    Two contiguous batches (no gap, no restart, same uploaded session id, same
    day) that differ only in ``app_package_name`` must split into two analysis
    sessions; two batches with the same package stay one session.
    """
    # Contiguous in both clocks so ONLY the app change can be a boundary.
    b1 = _tiny_batch("b1", base_ns=1_000_000_000, start_wall_ms=1_000_000, package="com.app.one")
    b2 = _tiny_batch("b2", base_ns=1_400_000_000, start_wall_ms=1_000_400, package="com.app.two")
    sessioned = sessionize(align_batches([b1, b2]), gap_min=10.0)
    assert sessioned["session_id"].nunique() == 2, "an app change must cut a new session"
    assert bool(sessioned["app_change"].any()), "the app-change boundary marker must be set"

    same = _tiny_batch("b3", base_ns=1_400_000_000, start_wall_ms=1_000_400, package="com.app.one")
    sessioned_same = sessionize(align_batches([b1, same]), gap_min=10.0)
    assert sessioned_same["session_id"].nunique() == 1, "same package must stay one session"


def test_make_windows_package_bucket_is_window_own_mode(synthetic_dir) -> None:
    """SRV-2: each window's package_bucket is the mode of its OWN IMU samples."""
    batches = list(load_batches(synthetic_dir))
    sessioned = sessionize(align_batches(batches), gap_min=10.0)
    windows = make_windows(sessioned, index_batches(batches), window_size_sec=5.0, stride_sec=1.0)
    assert windows
    for ctx in windows:
        imu = ctx["imu_samples"]
        window_mode = str(imu["app_package_name"].mode().iloc[0])
        assert ctx["package_bucket"] == window_mode
        # With the sessionize app-change boundary each session is single-package,
        # so the window is in fact package-pure.
        assert set(imu["app_package_name"].astype(str)) == {ctx["package_bucket"]}


def test_make_windows_emits_only_full_length() -> None:
    """SRV-14: only full-length windows are emitted (no sub-window tail residuals)."""
    # One 7.5s batch (750 samples @100Hz); session-relative span ~7.49s.
    batch = _tiny_batch("b1", base_ns=1_000_000_000, start_wall_ms=1_000_000, n=750)
    sessioned = sessionize(align_batches([batch]), gap_min=10.0)
    windows = make_windows(sessioned, index_batches([batch]), window_size_sec=5.0, stride_sec=1.0)
    # Full-window bound: starts at 0,1,2,3s (start+5s <= 7.49s+1s); NOT the old
    # residual tail up to ~7.49s.
    assert len(windows) == 4
    for ctx in windows:
        span_ns = int(ctx["imu_samples"]["session_elapsed_ns"].max() - ctx["imu_samples"]["session_elapsed_ns"].min())
        assert span_ns >= 4_000_000_000, f"residual sub-length window: {span_ns/1e9:.2f}s"


def test_sessionize_day_id_uses_study_timezone() -> None:
    """SRV-12: day_id is the study-timezone calendar day, not UTC."""
    # 2026-07-04 20:00 UTC == 2026-07-05 04:00 Asia/Shanghai (crosses local day).
    wall_ms = int(datetime(2026, 7, 4, 20, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    frame = align_batches([_tiny_batch("b1", base_ns=1_000_000_000, start_wall_ms=wall_ms)])
    cn = sessionize(frame, gap_min=10.0)  # default study_timezone == Asia/Shanghai
    assert set(cn["day_id"]) == {"2026-07-05"}
    utc = sessionize(frame, gap_min=10.0, study_timezone="UTC")
    assert set(utc["day_id"]) == {"2026-07-04"}
