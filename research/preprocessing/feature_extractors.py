"""Window feature extraction — manifest-driven, 3-channel parity, leakage-free.

``build_feature_columns(feature_mode)`` returns the ORDERED list of feature
column names for a mode; ``extract_window_features(window_ctx)`` returns a dict
keyed by exactly those columns (the anti-drift rule of ``_BUILD_CONTRACT.md``
§3b). Families implemented (reduced-but-representative per §10):

* **IMU** per channel ∈ {acc, gyro, mag}, per axis ∈ {x, y, z}: time-domain
  ``mean/std/min/max/rms/energy/zcr/jerk/skew/kurt`` and frequency
  ``domfreq/speccentroid/specentropy/band0_3/band3_8/band8_15`` (numpy rfft);
  per-channel magnitude ``mag_mean/mag_std/mag_energy``; per-channel
  ``sample_count`` and ``missing`` flag.
* **Orientation** (accel + mag derived, ALLOWED): ``orient_pitch_mean/pitch_std/
  roll_mean/roll_std/heading_stability/landscape``. ``orient_landscape`` is our
  OWN IMU-derived boolean — it is NOT the uploaded ``coarse_orientation`` and is
  explicitly permitted by the contract.
* **Cross-channel** ``corr_acc_gyro/acc_mag/gyro_mag``; motion-energy bins
  ``motion_energy_low/mid/high``; ``gyro_burst_count``.
* **Events**: ``evt_click/longclick/scroll/textchanged/focus/windowstate/
  windowcontent_count``, ``evt_rate``, ``evt_entropy``.
* **UI**: node counts, depth, clickable/editable/scrollable/focusable counts +
  ratios, checked/selected counts, surface-like, webview/list/scroll indicators,
  form-like control count, bounds occupancy, UI stable ms, and tree-diff
  ``ui_treediff_nodedelta/categoryl1/boundsl1/hashchanged``.
* **Package** (ONLY in package-including modes): ``pkg_bucket_hash`` (a small
  integer hash of ``package_bucket``, float-encoded).

A MISSING channel sets ``{ch}_missing = 1.0`` and fills every one of that
channel's feature cells with ``0.0`` (never a silent zero — the flag is set).

NONE of the leakage columns (``estimated_context_category``, ``game_like_score``,
``viewIdResourceName``, ``coarse_orientation``) is ever read or emitted.
"""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
import pandas as pd

from research import LEAKAGE_COLUMNS

# --- Channel / axis vocab ---------------------------------------------------

#: Short channel key -> raw sensor_type.
_CHANNELS: dict[str, str] = {
    "acc": "ACCELEROMETER",
    "gyro": "GYROSCOPE",
    "mag": "MAGNETIC_FIELD",
}
_AXES = ("x", "y", "z")

#: Per-axis time-domain feature suffixes.
_TIME_FEATS = ("mean", "std", "min", "max", "rms", "energy", "zcr", "jerk", "skew", "kurt")
#: Per-axis frequency-domain feature suffixes.
_FREQ_FEATS = ("domfreq", "speccentroid", "specentropy", "band0_3", "band3_8", "band8_15")

#: Feature modes and which family groups they include.
_FEATURE_MODES = {
    "sensor_only": {"imu": True, "ui": False, "event": False, "package": False},
    "ui_sensor": {"imu": True, "ui": True, "event": True, "package": True},
    "ui_sensor_no_package": {"imu": True, "ui": True, "event": True, "package": False},
    "package_only": {"imu": False, "ui": False, "event": False, "package": True},
    "ui_only": {"imu": False, "ui": True, "event": True, "package": False},
    "privacy_coarse_ui": {"imu": True, "ui": True, "event": True, "package": False},
}

_SAMPLING_RATE_HZ = 100.0
_NS_PER_SEC = 1_000_000_000.0


# --- Column builders --------------------------------------------------------


def _imu_columns() -> list[str]:
    """Return the ordered IMU + orientation + cross-channel column names."""
    cols: list[str] = []
    for ch in _CHANNELS:
        for ax in _AXES:
            for feat in _TIME_FEATS:
                cols.append(f"{ch}_{ax}_{feat}")
            for feat in _FREQ_FEATS:
                cols.append(f"{ch}_{ax}_{feat}")
        cols.extend([f"{ch}_mag_mean", f"{ch}_mag_std", f"{ch}_mag_energy"])
        cols.extend([f"{ch}_sample_count", f"{ch}_missing"])
    cols.extend(
        [
            "orient_pitch_mean",
            "orient_pitch_std",
            "orient_roll_mean",
            "orient_roll_std",
            "orient_heading_stability",
            "orient_landscape",
        ]
    )
    cols.extend(["corr_acc_gyro", "corr_acc_mag", "corr_gyro_mag"])
    cols.extend(["motion_energy_low", "motion_energy_mid", "motion_energy_high", "gyro_burst_count"])
    return cols


def _event_columns() -> list[str]:
    """Return the ordered event-family column names."""
    return [
        "evt_click_count",
        "evt_longclick_count",
        "evt_scroll_count",
        "evt_textchanged_count",
        "evt_focus_count",
        "evt_windowstate_count",
        "evt_windowcontent_count",
        "evt_rate",
        "evt_entropy",
    ]


def _ui_columns() -> list[str]:
    """Return the ordered UI-family (incl. tree-diff) column names."""
    return [
        "ui_node_count_mean",
        "ui_node_count_max",
        "ui_max_depth",
        "ui_clickable_count",
        "ui_editable_count",
        "ui_scrollable_count",
        "ui_focusable_count",
        "ui_editable_ratio",
        "ui_scrollable_ratio",
        "ui_checked_count",
        "ui_selected_count",
        "ui_surface_like",
        "ui_webview",
        "ui_list",
        "ui_scroll_indicator",
        "ui_form_like_control_count",
        "ui_bounds_occupancy",
        "ui_stable_ms",
        "ui_treediff_nodedelta",
        "ui_treediff_categoryl1",
        "ui_treediff_boundsl1",
        "ui_treediff_hashchanged",
    ]


def _package_columns() -> list[str]:
    """Return the ordered package-family column names."""
    return ["pkg_bucket_hash"]


def build_feature_columns(feature_mode: str) -> list[str]:
    """Return the ordered feature column names for a feature mode.

    Args:
        feature_mode: One of ``sensor_only, ui_sensor, ui_sensor_no_package,
            package_only, ui_only, privacy_coarse_ui``.

    Returns:
        Ordered list of feature column names (all float-valued downstream).

    Raises:
        ValueError: If ``feature_mode`` is unknown.
    """
    if feature_mode not in _FEATURE_MODES:
        raise ValueError(f"unknown feature_mode: {feature_mode!r} (valid: {sorted(_FEATURE_MODES)})")
    groups = _FEATURE_MODES[feature_mode]
    cols: list[str] = []
    if groups["imu"]:
        cols.extend(_imu_columns())
    if groups["event"]:
        cols.extend(_event_columns())
    if groups["ui"]:
        cols.extend(_ui_columns())
    if groups["package"]:
        cols.extend(_package_columns())
    # Anti-leakage guard: no emitted column may collide with a leakage name.
    leaked = [c for c in cols if c in LEAKAGE_COLUMNS]
    if leaked:  # pragma: no cover - defensive; column vocab is fixed above
        raise AssertionError(f"leakage columns present in feature set: {leaked}")
    return cols


def build_package_columns(feature_mode: str) -> list[str]:
    """Return only the package columns active for a mode (possibly empty).

    Args:
        feature_mode: The feature mode.

    Returns:
        The package column names if the mode includes package, else ``[]``.
    """
    if feature_mode not in _FEATURE_MODES:
        raise ValueError(f"unknown feature_mode: {feature_mode!r}")
    return list(_package_columns()) if _FEATURE_MODES[feature_mode]["package"] else []


def build_feature_manifest(feature_mode: str) -> dict[str, Any]:
    """Return the feature manifest for a mode (the models' input contract).

    Args:
        feature_mode: The feature mode.

    Returns:
        A dict ``{feature_columns, package_columns, leakage_free: True,
        feature_mode, input_dim}`` where ``input_dim == len(feature_columns)``.
    """
    columns = build_feature_columns(feature_mode)
    return {
        "feature_mode": feature_mode,
        "feature_columns": columns,
        "package_columns": build_package_columns(feature_mode),
        "input_dim": len(columns),
        "leakage_free": True,
    }


# --- Numeric helpers --------------------------------------------------------


def _safe_skew(values: np.ndarray) -> float:
    """Sample skewness with a zero-variance guard.

    Args:
        values: 1-D float array.

    Returns:
        Skewness (0.0 when fewer than 3 points or ~zero variance).
    """
    if values.size < 3:
        return 0.0
    mean = float(values.mean())
    std = float(values.std())
    if std < 1e-12:
        return 0.0
    return float(np.mean(((values - mean) / std) ** 3))


def _safe_kurt(values: np.ndarray) -> float:
    """Excess kurtosis with a zero-variance guard.

    Args:
        values: 1-D float array.

    Returns:
        Excess kurtosis (0.0 when fewer than 4 points or ~zero variance).
    """
    if values.size < 4:
        return 0.0
    mean = float(values.mean())
    std = float(values.std())
    if std < 1e-12:
        return 0.0
    return float(np.mean(((values - mean) / std) ** 4) - 3.0)


def _zero_crossing_rate(values: np.ndarray) -> float:
    """Fraction of adjacent samples that cross the (mean-removed) zero line.

    Args:
        values: 1-D float array.

    Returns:
        Zero-crossing rate in ``[0, 1]``.
    """
    if values.size < 2:
        return 0.0
    centered = values - float(values.mean())
    signs = np.signbit(centered)
    return float(np.mean(signs[1:] != signs[:-1]))


def _axis_time_features(values: np.ndarray) -> dict[str, float]:
    """Compute the time-domain feature suffixes for one axis.

    Args:
        values: 1-D float array of one axis' samples.

    Returns:
        Dict keyed by :data:`_TIME_FEATS`.
    """
    n = values.size
    if n == 0:
        return {feat: 0.0 for feat in _TIME_FEATS}
    diff = np.diff(values) if n > 1 else np.zeros(1)
    return {
        "mean": float(values.mean()),
        "std": float(values.std()),
        "min": float(values.min()),
        "max": float(values.max()),
        "rms": float(np.sqrt(np.mean(values**2))),
        "energy": float(np.sum(values**2) / n),
        "zcr": _zero_crossing_rate(values),
        "jerk": float(np.sum(diff**2) / max(1, diff.size)),
        "skew": _safe_skew(values),
        "kurt": _safe_kurt(values),
    }


def _axis_freq_features(values: np.ndarray, fs: float) -> dict[str, float]:
    """Compute frequency-domain feature suffixes for one axis via numpy rfft.

    Args:
        values: 1-D float array of one axis' samples.
        fs: Sampling frequency (Hz).

    Returns:
        Dict keyed by :data:`_FREQ_FEATS`.
    """
    n = values.size
    if n < 2:
        return {feat: 0.0 for feat in _FREQ_FEATS}
    centered = values - float(values.mean())
    spectrum = np.abs(np.fft.rfft(centered))
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    power = spectrum**2
    total = float(power.sum())
    if total < 1e-12:
        return {feat: 0.0 for feat in _FREQ_FEATS}
    dom_idx = int(np.argmax(power))
    centroid = float(np.sum(freqs * power) / total)
    prob = power / total
    nz = prob[prob > 0]
    entropy = float(-np.sum(nz * np.log(nz)))

    def band_ratio(lo: float, hi: float) -> float:
        band = power[(freqs >= lo) & (freqs < hi)]
        return float(band.sum() / total)

    return {
        "domfreq": float(freqs[dom_idx]),
        "speccentroid": centroid,
        "specentropy": entropy,
        "band0_3": band_ratio(0.0, 3.0),
        "band3_8": band_ratio(3.0, 8.0),
        "band8_15": band_ratio(8.0, 15.0),
    }


def _pivot_channel(imu: pd.DataFrame, sensor_type: str) -> np.ndarray | None:
    """Return an ``(n, 3)`` array of x/y/z for one channel, or ``None``.

    Args:
        imu: The window's IMU sample DataFrame.
        sensor_type: Raw sensor type string.

    Returns:
        The ``(n, 3)`` xyz array (rows are samples), or ``None`` if the channel
        is absent from this window.
    """
    if imu is None or imu.empty:
        return None
    sel = imu[imu["sensor_type"] == sensor_type]
    if sel.empty:
        return None
    return sel[["x", "y", "z"]].to_numpy(dtype=np.float64)


def _sampling_rate(imu: pd.DataFrame) -> float:
    """Estimate the effective per-channel sampling rate for a window.

    Falls back to the contract's 100 Hz when it cannot be estimated.

    Args:
        imu: The window's IMU sample DataFrame.

    Returns:
        Sampling frequency in Hz.
    """
    if imu is None or imu.empty:
        return _SAMPLING_RATE_HZ
    for sensor_type in _CHANNELS.values():
        sel = imu[imu["sensor_type"] == sensor_type]
        if len(sel) >= 2:
            ts = np.sort(sel["timestamp_elapsed_nanos"].to_numpy(dtype=np.float64))
            dt = np.diff(ts)
            dt = dt[dt > 0]
            if dt.size:
                return float(_NS_PER_SEC / np.median(dt))
    return _SAMPLING_RATE_HZ


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation of two equal-length magnitude series (guarded).

    Args:
        a: First series.
        b: Second series.

    Returns:
        Correlation in ``[-1, 1]``; 0.0 if degenerate / mismatched length.
    """
    n = min(a.size, b.size)
    if n < 2:
        return 0.0
    a2, b2 = a[:n], b[:n]
    if a2.std() < 1e-12 or b2.std() < 1e-12:
        return 0.0
    return float(np.clip(np.corrcoef(a2, b2)[0, 1], -1.0, 1.0))


# --- IMU feature block ------------------------------------------------------


def _extract_imu(imu: pd.DataFrame) -> dict[str, float]:
    """Compute the full IMU + orientation + cross-channel feature block.

    Missing channels are flagged (``{ch}_missing=1.0``) with all their cells
    zero-filled. All emitted keys are exactly :func:`_imu_columns`.

    Args:
        imu: The window's IMU sample DataFrame.

    Returns:
        Dict keyed by the IMU column names.
    """
    out: dict[str, float] = {c: 0.0 for c in _imu_columns()}
    fs = _sampling_rate(imu)

    channel_arrays: dict[str, np.ndarray | None] = {}
    for ch, sensor_type in _CHANNELS.items():
        arr = _pivot_channel(imu, sensor_type)
        channel_arrays[ch] = arr
        if arr is None:
            out[f"{ch}_missing"] = 1.0
            out[f"{ch}_sample_count"] = 0.0
            continue
        out[f"{ch}_missing"] = 0.0
        out[f"{ch}_sample_count"] = float(arr.shape[0])
        for axis_idx, ax in enumerate(_AXES):
            col = arr[:, axis_idx]
            for feat, value in _axis_time_features(col).items():
                out[f"{ch}_{ax}_{feat}"] = value
            for feat, value in _axis_freq_features(col, fs).items():
                out[f"{ch}_{ax}_{feat}"] = value
        magnitude = np.sqrt(np.sum(arr**2, axis=1))
        out[f"{ch}_mag_mean"] = float(magnitude.mean())
        out[f"{ch}_mag_std"] = float(magnitude.std())
        out[f"{ch}_mag_energy"] = float(np.sum(magnitude**2) / magnitude.size)

    acc = channel_arrays["acc"]
    gyro = channel_arrays["gyro"]
    mag = channel_arrays["mag"]

    # Orientation from accelerometer (pitch/roll) + magnetometer heading.
    if acc is not None:
        ax_m, ay_m, az_m = acc[:, 0], acc[:, 1], acc[:, 2]
        pitch = np.arctan2(-ax_m, np.sqrt(ay_m**2 + az_m**2))
        roll = np.arctan2(ay_m, az_m)
        out["orient_pitch_mean"] = float(pitch.mean())
        out["orient_pitch_std"] = float(pitch.std())
        out["orient_roll_mean"] = float(roll.mean())
        out["orient_roll_std"] = float(roll.std())
        # IMU-derived landscape bool (ALLOWED): |roll| near +-90 deg.
        roll_abs_mean = float(np.mean(np.abs(roll)))
        out["orient_landscape"] = 1.0 if roll_abs_mean > (np.pi / 4.0) else 0.0

    if mag is not None:
        heading = np.arctan2(mag[:, 1], mag[:, 0])
        # Stability = 1 - circular dispersion of heading (1.0 == perfectly stable).
        resultant = np.sqrt(np.mean(np.cos(heading)) ** 2 + np.mean(np.sin(heading)) ** 2)
        out["orient_heading_stability"] = float(resultant)

    # Cross-channel correlations of per-sample magnitude (length-matched).
    def _mag_series(arr: np.ndarray | None) -> np.ndarray:
        return np.sqrt(np.sum(arr**2, axis=1)) if arr is not None else np.empty(0)

    acc_mag, gyro_mag, mag_mag = _mag_series(acc), _mag_series(gyro), _mag_series(mag)
    out["corr_acc_gyro"] = _corr(acc_mag, gyro_mag)
    out["corr_acc_mag"] = _corr(acc_mag, mag_mag)
    out["corr_gyro_mag"] = _corr(gyro_mag, mag_mag)

    # Motion energy bins from gyroscope magnitude (rotation is the cleanest
    # motion proxy; falls back to accel deviation from gravity if gyro absent).
    if gyro is not None:
        motion = gyro_mag
    elif acc is not None:
        motion = np.abs(acc_mag - 9.81)
    else:
        motion = np.empty(0)
    if motion.size:
        low = float(np.mean(motion < 0.1))
        mid = float(np.mean((motion >= 0.1) & (motion < 0.5)))
        high = float(np.mean(motion >= 0.5))
        out["motion_energy_low"] = low
        out["motion_energy_mid"] = mid
        out["motion_energy_high"] = high
        out["gyro_burst_count"] = float(np.sum(motion > 0.5))

    return out


# --- Event feature block ----------------------------------------------------

_EVENT_MAP = {
    "TYPE_VIEW_CLICKED": "evt_click_count",
    "TYPE_VIEW_LONG_CLICKED": "evt_longclick_count",
    "TYPE_VIEW_SCROLLED": "evt_scroll_count",
    "TYPE_VIEW_TEXT_CHANGED": "evt_textchanged_count",
    "TYPE_VIEW_FOCUSED": "evt_focus_count",
    "TYPE_WINDOW_STATE_CHANGED": "evt_windowstate_count",
    "TYPE_WINDOW_CONTENT_CHANGED": "evt_windowcontent_count",
}


def _extract_events(events: list[dict[str, Any]], window_sec: float) -> dict[str, float]:
    """Compute the event-family features from a window's context events.

    Args:
        events: Context-event dicts in the window.
        window_sec: Window length (seconds), for the event rate.

    Returns:
        Dict keyed by :func:`_event_columns`.
    """
    out: dict[str, float] = {c: 0.0 for c in _event_columns()}
    counts: dict[str, int] = {}
    for event in events:
        et = str(event.get("event_type", ""))
        counts[et] = counts.get(et, 0) + 1
        col = _EVENT_MAP.get(et)
        if col is not None:
            out[col] += 1.0
    total = float(len(events))
    out["evt_rate"] = total / max(1e-9, window_sec)
    if total > 0:
        probs = np.array([c / total for c in counts.values()], dtype=np.float64)
        nz = probs[probs > 0]
        out["evt_entropy"] = float(-np.sum(nz * np.log(nz)))
    return out


# --- UI feature block -------------------------------------------------------


def _class_category(class_name: str | None) -> str:
    """Bucket an Android widget class into a coarse structural category.

    Args:
        class_name: The node's ``class_name`` (may be ``None``).

    Returns:
        A short category string (``edit/list/switch/button/surface/webview/
        text/other``).
    """
    name = (class_name or "").lower()
    if "edit" in name:
        return "edit"
    if "recycler" in name or "listview" in name or "scrollview" in name:
        return "list"
    if "switch" in name or "checkbox" in name or "radio" in name or "seekbar" in name or "spinner" in name:
        return "switch"
    if "webview" in name:
        return "webview"
    if "surface" in name or "texture" in name:
        return "surface"
    if "button" in name or "imagebutton" in name:
        return "button"
    if "text" in name:
        return "text"
    return "other"


def _node_category_histogram(nodes: list[dict[str, Any]]) -> dict[str, int]:
    """Category histogram over a node list (uses class_name only).

    Args:
        nodes: A list of node dicts.

    Returns:
        Category -> count.
    """
    hist: dict[str, int] = {}
    for node in nodes:
        cat = _class_category(node.get("class_name"))
        hist[cat] = hist.get(cat, 0) + 1
    return hist


def _structural_hash(nodes: list[dict[str, Any]]) -> int:
    """A stable structural hash of a node list (class categories + flags).

    Excludes ``viewIdResourceName`` (leakage) and all text — only structural
    booleans and categories participate.

    Args:
        nodes: A list of node dicts.

    Returns:
        A 32-bit int hash.
    """
    parts: list[str] = []
    for node in nodes:
        parts.append(
            "|".join(
                [
                    _class_category(node.get("class_name")),
                    str(int(bool(node.get("clickable")))),
                    str(int(bool(node.get("editable")))),
                    str(int(bool(node.get("scrollable")))),
                    str(int(bool(node.get("checkable")))),
                    str(int(node.get("depth", 0))),
                ]
            )
        )
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _bounds_area(node: dict[str, Any]) -> float:
    """Normalized area of a node's bounds grid (fraction of a 1080x1920 screen).

    Args:
        node: A node dict.

    Returns:
        Area fraction in ``[0, ~1]`` (0.0 if bounds missing/degenerate).
    """
    bounds = node.get("bounds_grid")
    if not isinstance(bounds, dict):
        return 0.0
    width = max(0, int(bounds.get("right", 0)) - int(bounds.get("left", 0)))
    height = max(0, int(bounds.get("bottom", 0)) - int(bounds.get("top", 0)))
    return float(width * height) / float(1080 * 1920)


def _extract_ui(
    nodes_snapshots: list[list[dict[str, Any]]],
    prev_snapshot: list[dict[str, Any]] | None,
    events: list[dict[str, Any]],
    window_sec: float,
) -> dict[str, float]:
    """Compute the UI-family features (incl. tree-diff) for a window.

    Args:
        nodes_snapshots: List of per-event node lists in the window.
        prev_snapshot: The previous window's last node list (tree-diff ref).
        events: The window's context events (for UI-stable timing).
        window_sec: Window length (seconds).

    Returns:
        Dict keyed by :func:`_ui_columns`.
    """
    out: dict[str, float] = {c: 0.0 for c in _ui_columns()}
    non_empty = [snap for snap in nodes_snapshots if snap]
    if not non_empty:
        # No UI snapshot in this window -> stable for the whole window, no nodes.
        out["ui_stable_ms"] = float(window_sec * 1000.0)
        # Compare emptiness against prev for tree-diff (a full teardown counts).
        if prev_snapshot:
            out["ui_treediff_nodedelta"] = float(len(prev_snapshot))
            out["ui_treediff_hashchanged"] = 1.0
        return out

    node_counts = [len(snap) for snap in non_empty]
    out["ui_node_count_mean"] = float(np.mean(node_counts))
    out["ui_node_count_max"] = float(np.max(node_counts))

    # Use the last snapshot as the representative UI state of the window.
    last = non_empty[-1]
    out["ui_max_depth"] = float(max((int(n.get("depth", 0)) for n in last), default=0))
    out["ui_clickable_count"] = float(sum(1 for n in last if n.get("clickable")))
    out["ui_editable_count"] = float(sum(1 for n in last if n.get("editable")))
    out["ui_scrollable_count"] = float(sum(1 for n in last if n.get("scrollable")))
    out["ui_focusable_count"] = float(sum(1 for n in last if n.get("focused")))
    out["ui_checked_count"] = float(sum(1 for n in last if n.get("checked")))
    out["ui_selected_count"] = float(sum(1 for n in last if n.get("selected")))
    n_last = max(1, len(last))
    out["ui_editable_ratio"] = out["ui_editable_count"] / n_last
    out["ui_scrollable_ratio"] = out["ui_scrollable_count"] / n_last

    categories = _node_category_histogram(last)
    out["ui_webview"] = 1.0 if categories.get("webview", 0) > 0 else 0.0
    out["ui_list"] = 1.0 if categories.get("list", 0) > 0 else 0.0
    out["ui_scroll_indicator"] = 1.0 if out["ui_scrollable_count"] > 0 else 0.0
    out["ui_form_like_control_count"] = float(
        categories.get("switch", 0) + categories.get("edit", 0)
    )
    # Surface-like: any node covering a large fraction of the screen.
    out["ui_surface_like"] = 1.0 if any(_bounds_area(n) > 0.5 for n in last) else 0.0
    out["ui_bounds_occupancy"] = float(min(1.0, sum(_bounds_area(n) for n in last)))

    # UI stability: window time minus a penalty per window-content change event.
    n_changes = sum(1 for e in events if e.get("event_type") in {"TYPE_WINDOW_CONTENT_CHANGED", "TYPE_WINDOW_STATE_CHANGED"})
    stable_ms = window_sec * 1000.0 / float(1 + n_changes)
    out["ui_stable_ms"] = float(stable_ms)

    # Tree diff vs the previous window's last snapshot.
    ref = prev_snapshot if prev_snapshot else (non_empty[0] if len(non_empty) > 1 else None)
    if ref is not None:
        out["ui_treediff_nodedelta"] = float(abs(len(last) - len(ref)))
        cats_last = _node_category_histogram(last)
        cats_ref = _node_category_histogram(ref)
        keys = set(cats_last) | set(cats_ref)
        out["ui_treediff_categoryl1"] = float(sum(abs(cats_last.get(k, 0) - cats_ref.get(k, 0)) for k in keys))
        occ_last = sum(_bounds_area(n) for n in last)
        occ_ref = sum(_bounds_area(n) for n in ref)
        out["ui_treediff_boundsl1"] = float(abs(occ_last - occ_ref))
        out["ui_treediff_hashchanged"] = 1.0 if _structural_hash(last) != _structural_hash(ref) else 0.0
    return out


# --- Package feature block --------------------------------------------------


def _package_bucket_hash(package_bucket: str) -> float:
    """Small stable integer hash of the package bucket, float-encoded.

    Args:
        package_bucket: The foreground package name.

    Returns:
        A float in ``[0, 1024)`` (a fingerprint-free bucket id, not the name).
    """
    digest = hashlib.sha256(str(package_bucket).encode("utf-8")).hexdigest()
    return float(int(digest[:8], 16) % 1024)


def _extract_package(package_bucket: str) -> dict[str, float]:
    """Compute the package-family features.

    Args:
        package_bucket: The foreground package name.

    Returns:
        Dict keyed by :func:`_package_columns`.
    """
    return {"pkg_bucket_hash": _package_bucket_hash(package_bucket)}


# --- Public entry point -----------------------------------------------------


def extract_window_features(
    window_ctx: dict[str, Any],
    *,
    feature_mode: str = "ui_sensor",
) -> dict[str, float]:
    """Extract the feature dict for a window context, per ``feature_mode``.

    The returned dict is keyed by exactly ``build_feature_columns(feature_mode)``
    (every column present, in order after ``dict`` insertion). Missing IMU
    channels are flagged and zero-filled; no leakage column is read or emitted.

    Args:
        window_ctx: A window context from
            :func:`research.preprocessing.windowing.make_windows`.
        feature_mode: The feature mode selecting which families to compute.

    Returns:
        A ``dict[str, float]`` covering exactly the mode's feature columns.

    Raises:
        ValueError: If ``feature_mode`` is unknown.
    """
    if feature_mode not in _FEATURE_MODES:
        raise ValueError(f"unknown feature_mode: {feature_mode!r}")
    groups = _FEATURE_MODES[feature_mode]
    window_sec = float(window_ctx.get("window_duration_sec", 5.0))

    features: dict[str, float] = {}
    if groups["imu"]:
        features.update(_extract_imu(window_ctx.get("imu_samples")))
    if groups["event"]:
        features.update(_extract_events(window_ctx.get("events") or [], window_sec))
    if groups["ui"]:
        features.update(
            _extract_ui(
                window_ctx.get("nodes_snapshots") or [],
                window_ctx.get("prev_snapshot"),
                window_ctx.get("events") or [],
                window_sec,
            )
        )
    if groups["package"]:
        features.update(_extract_package(str(window_ctx.get("package_bucket", "unknown"))))

    # Reindex to the canonical column order and guarantee full coverage.
    ordered = build_feature_columns(feature_mode)
    return {col: float(features.get(col, 0.0)) for col in ordered}
