"""3-channel IMU feature parity + missing-channel flagging — §15.1.3.

Asserts:

* the ``sensor_only`` feature schema contains a fully symmetric set of columns
  across the 3 channels (acc / gyro / mag) for every time + freq + magnitude
  feature, plus per-channel ``sample_count`` / ``missing`` flags;
* extracting features on a full 3-channel window sets every ``{ch}_missing`` to
  0 and populates non-trivial values;
* dropping the magnetometer sets ``mag_missing = 1.0`` (and zero-fills its
  cells) while the surviving channels are unaffected — a MISSING channel is
  flagged, never silently zero-filled;
* NO leakage column appears in the feature schema.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research import LEAKAGE_COLUMNS, SENSOR_TYPES
from research.preprocessing.feature_extractors import (
    build_feature_columns,
    extract_window_features,
)

_CHANNELS = ("acc", "gyro", "mag")
# Suffixes that must exist identically for every channel (parity check).
_PER_AXIS = (
    "mean", "std", "min", "max", "rms", "energy", "zcr", "jerk", "skew", "kurt",
    "domfreq", "speccentroid", "specentropy", "band0_3", "band3_8", "band8_15",
)
_PER_CHANNEL = ("mag_mean", "mag_std", "mag_energy", "sample_count", "missing")


def _imu_frame(channels: tuple[str, ...], n: int = 200) -> pd.DataFrame:
    """Build an IMU sample frame for the given channels (100 Hz, moving signal)."""
    period = 10_000_000
    rng = np.random.default_rng(0)
    rows = []
    type_for = {"acc": "ACCELEROMETER", "gyro": "GYROSCOPE", "mag": "MAGNETIC_FIELD"}
    for ch in channels:
        for i in range(n):
            rows.append(
                {
                    "sensor_type": type_for[ch],
                    "timestamp_elapsed_nanos": i * period,
                    "x": float(np.sin(i / 5.0) + rng.normal(0, 0.05)),
                    "y": float(np.cos(i / 7.0) + rng.normal(0, 0.05)),
                    "z": float((9.81 if ch == "acc" else 0.5) + rng.normal(0, 0.05)),
                }
            )
    return pd.DataFrame(rows)


def _ctx(imu: pd.DataFrame) -> dict:
    """Wrap an IMU frame in a minimal window context."""
    return {"imu_samples": imu, "events": [], "nodes_snapshots": [], "window_duration_sec": 5.0}


def test_three_channel_column_parity() -> None:
    """Every per-axis / per-channel feature exists identically for all 3 channels."""
    cols = set(build_feature_columns("sensor_only"))
    for ch in _CHANNELS:
        for axis in ("x", "y", "z"):
            for feat in _PER_AXIS:
                assert f"{ch}_{axis}_{feat}" in cols, f"missing {ch}_{axis}_{feat}"
        for feat in _PER_CHANNEL:
            assert f"{ch}_{feat}" in cols, f"missing {ch}_{feat}"
    # Orientation landscape is the ALLOWED IMU-derived boolean.
    assert "orient_landscape" in cols
    # Leakage-free by construction.
    assert cols.isdisjoint(LEAKAGE_COLUMNS)


def test_full_three_channel_window_has_no_missing() -> None:
    """A full 3-channel window sets every ``{ch}_missing`` to 0 with real values."""
    feats = extract_window_features(_ctx(_imu_frame(_CHANNELS)), feature_mode="sensor_only")
    for ch in _CHANNELS:
        assert feats[f"{ch}_missing"] == 0.0
        assert feats[f"{ch}_sample_count"] > 0
        assert feats[f"{ch}_mag_mean"] != 0.0
    # Every feature column is present and float-valued.
    assert set(feats) == set(build_feature_columns("sensor_only"))
    assert all(isinstance(v, float) for v in feats.values())


def test_missing_channel_is_flagged_not_silent() -> None:
    """Dropping the magnetometer flags mag_missing and zero-fills its cells only."""
    full = extract_window_features(_ctx(_imu_frame(_CHANNELS)), feature_mode="sensor_only")
    no_mag = extract_window_features(_ctx(_imu_frame(("acc", "gyro"))), feature_mode="sensor_only")

    assert no_mag["mag_missing"] == 1.0, "missing channel MUST be flagged"
    assert no_mag["mag_sample_count"] == 0.0
    # All mag_* feature cells are zero-filled (never silently populated).
    for axis in ("x", "y", "z"):
        for feat in _PER_AXIS:
            assert no_mag[f"mag_{axis}_{feat}"] == 0.0
    assert no_mag["mag_mag_mean"] == 0.0

    # Surviving channels stay present and flagged as present.
    assert no_mag["acc_missing"] == 0.0 and no_mag["gyro_missing"] == 0.0
    assert full["acc_missing"] == 0.0 and full["gyro_missing"] == 0.0


def test_symmetry_channels_are_interchangeable() -> None:
    """Feeding identical signals to two channels yields identical per-axis stats."""
    # Build acc and gyro from the exact same waveform -> their features must match,
    # proving the channels are treated fully symmetrically (no accel-privileged path).
    n = 128
    period = 10_000_000
    rng = np.random.default_rng(1)
    wave = [(float(np.sin(i / 4.0)), float(np.cos(i / 6.0)), float(np.sin(i / 9.0))) for i in range(n)]
    rows = []
    for stype in ("ACCELEROMETER", "GYROSCOPE"):
        for i, (x, y, z) in enumerate(wave):
            rows.append({"sensor_type": stype, "timestamp_elapsed_nanos": i * period, "x": x, "y": y, "z": z})
    feats = extract_window_features(_ctx(pd.DataFrame(rows)), feature_mode="sensor_only")
    for axis in ("x", "y", "z"):
        for feat in ("mean", "std", "energy", "domfreq", "band3_8"):
            assert feats[f"acc_{axis}_{feat}"] == feats[f"gyro_{axis}_{feat}"], f"asymmetry at {axis}_{feat}"


def _gravity_window(gx: float, gy: float, gz: float, n: int = 200) -> dict:
    """A 3-channel window whose accelerometer reads a CONSTANT gravity vector.

    ``orient_landscape`` is derived purely from the mean accelerometer vector, so the
    accel channel is pinned to ``(gx, gy, gz)`` exactly (no noise) to keep the
    assertion deterministic — for the flat case gx==gy==0 must hold exactly, or the
    tie-break would be at the mercy of noise. Gyro/mag carry tiny motionless sinusoids
    only so the frame is a well-formed 3-channel window; they never feed orient_landscape.
    """
    period = 10_000_000
    rows = []
    for i in range(n):
        rows.append({"sensor_type": "ACCELEROMETER", "timestamp_elapsed_nanos": i * period,
                     "x": gx, "y": gy, "z": gz})
        rows.append({"sensor_type": "GYROSCOPE", "timestamp_elapsed_nanos": i * period,
                     "x": 0.01 * float(np.sin(i / 5.0)), "y": 0.01 * float(np.cos(i / 5.0)), "z": 0.0})
        rows.append({"sensor_type": "MAGNETIC_FIELD", "timestamp_elapsed_nanos": i * period,
                     "x": 30.0 + 0.1 * float(np.sin(i / 8.0)), "y": 0.1 * float(np.cos(i / 8.0)), "z": 5.0})
    return _ctx(pd.DataFrame(rows))


def _landscape(gx: float, gy: float, gz: float) -> float:
    """``orient_landscape`` for a window whose mean gravity vector is (gx, gy, gz)."""
    return extract_window_features(_gravity_window(gx, gy, gz), feature_mode="sensor_only")["orient_landscape"]


def test_orient_landscape_follows_synthetic_gravity() -> None:
    """orient_landscape tracks the mean gravity vector (2026-07-03 inversion fix).

    Regression guard for the bug where the old ``|roll|>pi/4`` test flagged upright
    portrait as landscape==1 and true landscape as ~0.44. The corrected criterion is
    ``landscape = |mean(acc_x)| > |mean(acc_y)|``:

    * portrait upright   -> gravity on -y             -> 0.0
    * portrait top-down  -> gravity on +y             -> 0.0
    * landscape (either rotation sense) -> gravity on +-x -> 1.0
    * flat on a table    -> gravity on +z, gx==gy==0  -> 0.0 (documented tie -> portrait)
    """
    g = 9.81
    assert _landscape(0.0, -g, 0.0) == 0.0            # portrait, upright
    assert _landscape(0.0, g, 0.0) == 0.0             # portrait, upside-down
    assert _landscape(g, 0.0, 0.0) == 1.0             # landscape, one sense
    assert _landscape(-g, 0.0, 0.0) == 1.0            # landscape, other sense
    assert _landscape(0.0, 0.0, g) == 0.0             # flat: ambiguous tie -> 0.0


def test_orient_landscape_tilt_dominant_axis_wins() -> None:
    """Under a tilt, the screen axis with the larger gravity projection wins."""
    g = 9.81
    # x-projection beats y (device still partly pitched up on z) -> landscape.
    assert _landscape(g * 0.7, g * 0.2, g * 0.5) == 1.0
    # y-projection beats x -> portrait.
    assert _landscape(g * 0.2, g * 0.7, g * 0.5) == 0.0
