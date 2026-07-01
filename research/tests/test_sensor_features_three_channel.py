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
