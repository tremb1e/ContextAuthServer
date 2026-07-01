"""Per-window quality flags (``_BUILD_CONTRACT.md`` §3a / ``_recon_spec.md`` §3).

``quality_flags(window_ctx)`` returns a list of string flags describing data
quality issues for a window. The vocabulary is fixed:

* ``missing_sensor`` — one or more of the 3 IMU channels is absent in the window.
* ``missing_ui`` — no UI node snapshot is present in the window.
* ``low_record_count`` — too few IMU samples for a reliable window.
* ``service_restart`` — the window's session was cut by an elapsed-clock reset.
* ``app_transition_window`` — more than one foreground package inside the window.
* ``time_gap`` — the window's session contained a large inter-sample gap.
* ``privacy_violation`` — a node leaked forbidden content (password node, or a
  non-null text field survived) — MUST never happen for contract-valid data,
  but is checked defensively so on-disk artifacts can be audited.
* ``low_confidence_label`` — attached later by the weak labeler (kept in the
  vocabulary and appended by the caller); :func:`quality_flags` does not set it
  because it has no access to weak-label probabilities.

The ``low_confidence_label`` flag is documented here but populated in
``scripts/run_preprocess`` after weak labeling, keeping this module free of any
labeling dependency.
"""

from __future__ import annotations

from typing import Any

from research import SENSOR_TYPES

#: Minimum IMU sample count (across channels) below which a window is sparse.
#: At 100 Hz a full 5s window has ~1500 samples (3 channels); a partial window
#: near a session edge is expected, so this is a conservative floor.
_LOW_RECORD_THRESHOLD = 30

#: The full quality-flag vocabulary (stable order for reporting).
QUALITY_FLAG_VOCAB: tuple[str, ...] = (
    "missing_sensor",
    "missing_ui",
    "low_record_count",
    "service_restart",
    "app_transition_window",
    "time_gap",
    "privacy_violation",
    "low_confidence_label",
)


def _has_privacy_violation(nodes_snapshots: list[list[dict[str, Any]]]) -> bool:
    """Return True if any node leaks forbidden content.

    A contract-valid batch never contains a password node or a surviving
    non-null text field; this defensively re-checks the on-disk data so the
    privacy sanity tests can assert the pipeline never ingests a violation.

    Args:
        nodes_snapshots: The window's per-event node lists.

    Returns:
        True if a forbidden field is present.
    """
    text_keys = ("text", "text_redacted", "content_desc_redacted", "window_title_redacted")
    for snapshot in nodes_snapshots:
        for node in snapshot:
            if node.get("password"):
                return True
            for key in text_keys:
                value = node.get(key)
                if value is not None and value != "" and value != "<EDITABLE_TEXT_DROPPED>":
                    return True
    return False


def quality_flags(window_ctx: dict[str, Any]) -> list[str]:
    """Compute the quality flags for a window context.

    Args:
        window_ctx: A window context from
            :func:`research.preprocessing.windowing.make_windows`.

    Returns:
        A sorted-by-vocabulary list of flag strings (may be empty). The
        ``low_confidence_label`` flag is NOT set here (see module docstring).
    """
    flags: set[str] = set()
    imu = window_ctx.get("imu_samples")

    # missing_sensor: any of the 3 channels absent from this window.
    present_channels: set[str] = set()
    n_samples = 0
    if imu is not None and not imu.empty:
        present_channels = set(imu["sensor_type"].unique())
        n_samples = int(len(imu))
    if any(sensor_type not in present_channels for sensor_type in SENSOR_TYPES):
        flags.add("missing_sensor")

    # low_record_count: too few IMU samples overall.
    if n_samples < _LOW_RECORD_THRESHOLD:
        flags.add("low_record_count")

    # missing_ui: no UI node snapshot present.
    nodes_snapshots = window_ctx.get("nodes_snapshots") or []
    if not any(snapshot for snapshot in nodes_snapshots):
        flags.add("missing_ui")

    # service_restart / time_gap: propagated from sessionization.
    if window_ctx.get("service_restart"):
        flags.add("service_restart")
    if window_ctx.get("session_gap"):
        flags.add("time_gap")

    # app_transition_window: >1 distinct foreground package in the window.
    if imu is not None and not imu.empty and "app_package_name" in imu:
        if imu["app_package_name"].nunique() > 1:
            flags.add("app_transition_window")
    # Also detect a package change across the window's events.
    event_pkgs = {
        e.get("app_package_name")
        for e in (window_ctx.get("events") or [])
        if e.get("app_package_name") is not None
    }
    if len(event_pkgs) > 1:
        flags.add("app_transition_window")

    # privacy_violation: forbidden node content (defensive).
    if _has_privacy_violation(nodes_snapshots):
        flags.add("privacy_violation")

    return [flag for flag in QUALITY_FLAG_VOCAB if flag in flags]
