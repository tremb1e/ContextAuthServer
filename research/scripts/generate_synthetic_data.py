"""Deterministic synthetic ContextAuth data generator.

Simulates multi-user / multi-day / multi-session collection across the 7
interaction scenarios (I0..I6, the app's own task classes), emitting RAW
3-channel sensor samples
(accelerometer / gyroscope / magnetic field @ 100 Hz), UI ``context_events``
with drop-all-text ``NodeSnapshot`` trees, ``context_features`` (INCLUDING the
leakage columns, which exist in real data), timing-only ``touch_events`` and a
consistent ``diagnostics`` block. Every emitted batch satisfies
``app/schemas.py`` (BUILTIN_TASK task-label contract, ``encryption="none"``,
``compression="lz4_frame"``, ``redaction_applied=True``, counts consistent).

Determinism: all randomness flows from ``numpy.random.default_rng(seed)`` and
derived per-context seeds (via :func:`research.utils.seed.stable_int_seed`).
No wall clock or ``random`` module is used for data content.

Layout written:
    {out}/devices/{device_id}/{date}/{batch_id}.json          (accepted batch)
With ``--emit-envelopes`` also:
    {out}/envelopes/{batch_id}.json                           (8-field envelope)

Run:
    python -m research.scripts.generate_synthetic_data \
        --users 3 --days 2 --sessions-per-day 2 --out data/synthetic --seed 42
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import lz4.frame
import numpy as np

from research import SCENARIO_TASK_META, SCENARIOS, SENSOR_TYPES
from research.utils.seed import stable_int_seed

# --- Constants --------------------------------------------------------------

SAMPLING_RATE_HZ = 100
CONSENT_VERSION = "1"
RULE_VERSION = "1"
RULE_HASH_ZEROS = "0" * 64  # baseline rule hash (64 zeros), per _recon_contract.
APP_VERSION = "1.0.0"
ALGORITHM = "LZ4_FRAME+JSON"

#: A pool of plausible foreground packages, bucketed so leave_app_out has signal.
PACKAGE_POOL: list[tuple[str, str]] = [
    ("com.android.chrome", "com.google.android.apps.chrome.Main"),
    ("com.google.android.youtube", "com.google.android.youtube.WatchWhileActivity"),
    ("com.android.settings", "com.android.settings.Settings"),
    ("com.example.reader", "com.example.reader.ReaderActivity"),
    ("com.example.notes", "com.example.notes.EditorActivity"),
    ("com.example.canvas", "com.example.canvas.DrawActivity"),
]

#: Accessibility event types we emit (structural only, never text).
EVENT_TYPES = [
    "TYPE_VIEW_CLICKED",
    "TYPE_VIEW_LONG_CLICKED",
    "TYPE_VIEW_SCROLLED",
    "TYPE_VIEW_TEXT_CHANGED",
    "TYPE_VIEW_FOCUSED",
    "TYPE_WINDOW_STATE_CHANGED",
    "TYPE_WINDOW_CONTENT_CHANGED",
]

#: Touch event types (timing only, no coordinates).
TOUCH_TYPES = [
    "TOUCH_INTERACTION_START",
    "TOUCH_DOWN",
    "TOUCH_UP",
    "TOUCH_INTERACTION_END",
]

ORIENTATIONS = ["portrait", "landscape", "portrait_reverse", "landscape_reverse", "unknown"]


# --- Per-scenario behavioural profile --------------------------------------


@dataclass(frozen=True)
class ScenarioProfile:
    """Motion + UI + event characteristics for one interaction scenario.

    Attributes are used to shape synthetic sensor streams and UI/event mixes so
    the downstream weak labeler and features have realistic signal. Motion is
    expressed as (mean_magnitude, jitter_std) per channel in SI-ish units.
    """

    accel: tuple[float, float]
    gyro: tuple[float, float]
    mag: tuple[float, float]
    event_types: tuple[str, ...]
    event_rate_hz: float
    editable: bool
    scrollable: bool
    clickable: bool
    checkable: bool
    surface_like: bool
    ime_visible: bool
    landscape: bool
    node_count: tuple[int, int]  # (min, max)
    touch_rate_hz: float
    # webview: emit the scrollable container as a WebView (doc) instead of a
    # RecyclerView (list) — this is what separates I4 long-form review (webview)
    # from I3 list browsing (list) for the weak labeler.
    webview: bool = False


#: Scenario id -> behavioural profile. Values chosen so per-class synthetic
#: windows are separable by the weak-label scoring rules
#: (:mod:`research.labeling.interaction_states`). Keyed by the app's own 7 task
#: classes I0..I6 (2026-07-03 taxonomy).
PROFILES: dict[str, ScenarioProfile] = {
    "I0": ScenarioProfile(  # STATIC_VIEWING: quiet watching + video, very low motion
        accel=(9.81, 0.05), gyro=(0.0, 0.02), mag=(30.0, 0.3),
        event_types=("TYPE_WINDOW_CONTENT_CHANGED",), event_rate_hz=0.2,
        editable=False, scrollable=False, clickable=False, checkable=False,
        surface_like=False, ime_visible=False, landscape=False,
        node_count=(8, 16), touch_rate_hz=0.1),
    "I1": ScenarioProfile(  # TEXT_ENTRY: typing, editable + IME
        accel=(9.81, 0.12), gyro=(0.0, 0.06), mag=(30.0, 0.5),
        event_types=("TYPE_VIEW_TEXT_CHANGED", "TYPE_VIEW_FOCUSED", "TYPE_WINDOW_CONTENT_CHANGED"),
        event_rate_hz=3.0, editable=True, scrollable=False, clickable=True, checkable=False,
        surface_like=False, ime_visible=True, landscape=False,
        node_count=(12, 24), touch_rate_hz=3.0),
    "I2": ScenarioProfile(  # DISCRETE_TOUCH: taps, menus + structured controls
        accel=(9.81, 0.18), gyro=(0.0, 0.12), mag=(30.0, 0.6),
        event_types=("TYPE_VIEW_CLICKED", "TYPE_WINDOW_STATE_CHANGED", "TYPE_WINDOW_CONTENT_CHANGED"),
        event_rate_hz=1.5, editable=False, scrollable=False, clickable=True, checkable=True,
        surface_like=False, ime_visible=False, landscape=False,
        node_count=(12, 24), touch_rate_hz=1.0),
    "I3": ScenarioProfile(  # LIST_BROWSING: list scroll + item selection
        accel=(9.81, 0.2), gyro=(0.0, 0.15), mag=(30.0, 0.6),
        event_types=("TYPE_VIEW_SCROLLED", "TYPE_VIEW_CLICKED", "TYPE_WINDOW_CONTENT_CHANGED"),
        event_rate_hz=4.0, editable=False, scrollable=True, clickable=True, checkable=False,
        surface_like=False, ime_visible=False, landscape=False,
        node_count=(20, 40), touch_rate_hz=1.5),
    "I4": ScenarioProfile(  # LONG_FORM_REVIEW: continuous doc/webview scroll, ~no clicks
        accel=(9.81, 0.15), gyro=(0.0, 0.1), mag=(30.0, 0.55),
        event_types=("TYPE_VIEW_SCROLLED",), event_rate_hz=2.0,
        editable=False, scrollable=True, clickable=False, checkable=False,
        surface_like=False, ime_visible=False, landscape=False,
        node_count=(15, 30), touch_rate_hz=0.5, webview=True),
    "I5": ScenarioProfile(  # OBJECT_MANIPULATION: annotate/draw/drag on a canvas
        accel=(9.81, 0.35), gyro=(0.0, 0.2), mag=(30.0, 0.8),
        event_types=("TYPE_WINDOW_CONTENT_CHANGED",), event_rate_hz=0.3,
        editable=False, scrollable=False, clickable=False, checkable=False,
        surface_like=True, ime_visible=False, landscape=True,
        node_count=(4, 10), touch_rate_hz=4.0),
    "I6": ScenarioProfile(  # WRIST_ROTATION: high rotation energy, ~no touch
        accel=(9.81, 1.2), gyro=(0.0, 1.5), mag=(30.0, 3.0),
        event_types=("TYPE_WINDOW_CONTENT_CHANGED",), event_rate_hz=0.3,
        editable=False, scrollable=False, clickable=False, checkable=False,
        surface_like=False, ime_visible=False, landscape=False,
        node_count=(3, 6), touch_rate_hz=0.05),
}


@dataclass
class GenStats:
    """Accumulated generation counters for the summary printout."""

    users: int = 0
    days: int = 0
    sessions: int = 0
    batches: int = 0
    sensor_samples: int = 0
    envelopes: int = 0
    scenario_counts: dict[str, int] = field(default_factory=lambda: {s: 0 for s in SCENARIOS})


# --- Helpers ----------------------------------------------------------------


def _device_id_for_user(user_index: int, seed: int) -> str:
    """Return a deterministic 64-hex device id for a user (salted hash).

    Mirrors the real device_id shape (64-hex salted hash, no PII).

    Args:
        user_index: Zero-based user index.
        seed: Base run seed (folded in so different runs differ).

    Returns:
        A 64-character lowercase hex string.
    """
    material = f"contextauth-synth-user::{seed}::{user_index}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _rng_for(*parts: Any) -> np.random.Generator:
    """Build a deterministic numpy Generator seeded from ``parts``.

    Args:
        *parts: Context identifiers (user, day, session, ...).

    Returns:
        A seeded :class:`numpy.random.Generator`.
    """
    return np.random.default_rng(stable_int_seed(*parts))


def _uuid_from(rng: np.random.Generator) -> str:
    """Draw a deterministic RFC-4122 UUID string from a seeded generator.

    ``uuid.uuid4`` reads ``os.urandom`` and is therefore NOT reproducible. To
    keep generation fully deterministic (contract §13: numpy ``default_rng``
    only, no ``time``/OS randomness), we draw 16 bytes from ``rng`` and set the
    version (4) and variant (RFC-4122) bits ourselves. The server accepts any
    value that parses via ``uuid.UUID(...)``, so this is contract-valid.

    Args:
        rng: The seeded generator to draw randomness from.

    Returns:
        A canonical UUID string (e.g. ``"xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx"``).
    """
    raw = bytearray(int(b) for b in rng.integers(0, 256, size=16, dtype=np.uint8))
    raw[6] = (raw[6] & 0x0F) | 0x40  # version 4
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC-4122 variant
    return str(uuid.UUID(bytes=bytes(raw)))


def _date_dir_for(wall_millis: int) -> str:
    """UTC ``YYYY-MM-DD`` directory name for a wall-clock ms timestamp.

    Matches ``app.storage._date_dir`` (``time.gmtime``).

    Args:
        wall_millis: Wall-clock time in milliseconds.

    Returns:
        A ``YYYY-MM-DD`` string.
    """
    return time.strftime("%Y-%m-%d", time.gmtime(wall_millis / 1000))


def _build_sensor_samples(
    rng: np.random.Generator,
    profile: ScenarioProfile,
    *,
    base_elapsed_nanos: int,
    started_wall_millis: int,
    duration_sec: int,
    drop_channel: str | None,
) -> list[dict[str, Any]]:
    """Generate 3-channel raw sensor samples for one batch window.

    Each channel is sampled at ``SAMPLING_RATE_HZ``. Gravity (~9.81) loads the
    accel z-ish magnitude; per-scenario jitter shapes motion energy. If
    ``drop_channel`` is set that channel is omitted entirely (simulating a
    missing stream; downstream must flag it, never silently zero-fill).

    Args:
        rng: Seeded generator.
        profile: Scenario behavioural profile.
        base_elapsed_nanos: Origin for ``timestamp_elapsed_nanos``.
        started_wall_millis: Wall-clock origin for ``wall_time_estimated_millis``.
        duration_sec: Batch duration in seconds.
        drop_channel: Sensor type to omit, or ``None``.

    Returns:
        A list of :class:`app.schemas.SensorSample`-shaped dicts.
    """
    n_per_channel = max(1, int(round(duration_sec * SAMPLING_RATE_HZ)))
    period_ns = int(round(1e9 / SAMPLING_RATE_HZ))
    samples: list[dict[str, Any]] = []

    channel_params = {
        "ACCELEROMETER": profile.accel,
        "GYROSCOPE": profile.gyro,
        "MAGNETIC_FIELD": profile.mag,
    }

    for sensor_type in SENSOR_TYPES:
        if sensor_type == drop_channel:
            continue
        mean_mag, jitter = channel_params[sensor_type]
        # Distribute the nominal magnitude across a dominant axis + noise so the
        # window has non-trivial per-axis structure and cross-axis correlation.
        for i in range(n_per_channel):
            elapsed = base_elapsed_nanos + i * period_ns
            wall = started_wall_millis + int(i * 1000 / SAMPLING_RATE_HZ)
            if sensor_type == "ACCELEROMETER":
                x = float(rng.normal(0.0, jitter))
                y = float(rng.normal(0.0, jitter))
                z = float(mean_mag + rng.normal(0.0, jitter))
            elif sensor_type == "GYROSCOPE":
                x = float(rng.normal(0.0, jitter))
                y = float(rng.normal(0.0, jitter))
                z = float(rng.normal(0.0, jitter))
            else:  # MAGNETIC_FIELD
                x = float(mean_mag / np.sqrt(3.0) + rng.normal(0.0, jitter))
                y = float(mean_mag / np.sqrt(3.0) + rng.normal(0.0, jitter))
                z = float(mean_mag / np.sqrt(3.0) + rng.normal(0.0, jitter))
            samples.append(
                {
                    "sensor_type": sensor_type,
                    "timestamp_elapsed_nanos": int(elapsed),
                    "wall_time_estimated_millis": int(wall),
                    "x": round(x, 6),
                    "y": round(y, 6),
                    "z": round(z, 6),
                    "accuracy": 3,
                }
            )
    return samples


def _build_nodes(rng: np.random.Generator, profile: ScenarioProfile) -> list[dict[str, Any]]:
    """Build a drop-all-text NodeSnapshot tree for a context event.

    Text fields are constant-null; only presence booleans (``has_text``,
    ``has_content_description``) survive. ``viewIdResourceName`` is emitted (it
    exists in real data) and is a LEAKAGE column excluded downstream. No node is
    a password node.

    Args:
        rng: Seeded generator.
        profile: Scenario behavioural profile.

    Returns:
        A list of :class:`app.schemas.NodeSnapshot`-shaped dicts.
    """
    n_nodes = int(rng.integers(profile.node_count[0], profile.node_count[1] + 1))
    nodes: list[dict[str, Any]] = []
    for depth in range(n_nodes):
        editable = bool(profile.editable and rng.random() < 0.35)
        scrollable = bool(profile.scrollable and rng.random() < 0.5)
        clickable = bool(profile.clickable and rng.random() < 0.4)
        checkable = bool(profile.checkable and rng.random() < 0.4)
        checked = bool(checkable and rng.random() < 0.5)
        # A large surface-like region for media/canvas scenarios.
        if profile.surface_like and depth == 0:
            bounds = {"left": 0, "top": 0, "right": 1080, "bottom": 1920}
            class_name = "android.view.SurfaceView"
        else:
            left = int(rng.integers(0, 400))
            top = int(rng.integers(0, 800))
            bounds = {
                "left": left,
                "top": top,
                "right": left + int(rng.integers(80, 680)),
                "bottom": top + int(rng.integers(40, 240)),
            }
            class_name = _class_name_for(editable, scrollable, checkable, clickable)
            # Long-form review scrolls a document/webview, not a list — emit the
            # scrollable container as a WebView so ui_webview (not ui_list) fires.
            if profile.webview and scrollable and not editable:
                class_name = "android.webkit.WebView"
        has_text = bool(rng.random() < 0.6)
        nodes.append(
            {
                "node_id": f"node-{depth}-{uuid.UUID(int=int(rng.integers(0, 2**63))).hex[:8]}",
                "class_name": class_name,
                # LEAKAGE column (kept in raw only): a fingerprintable resource id.
                "viewIdResourceName": f"com.example:id/widget_{depth}",
                "bounds_grid": bounds,
                "clickable": clickable,
                "editable": editable,
                "scrollable": scrollable,
                "checkable": checkable,
                "checked": checked,
                "enabled": True,
                "focused": bool(editable and depth == 0 and profile.ime_visible),
                "selected": False,
                "visible_to_user": True,
                "long_clickable": bool(clickable and rng.random() < 0.2),
                "password": False,
                "input_type_category": "text" if editable else None,
                "child_count": int(rng.integers(0, 4)),
                # Text is ALWAYS dropped on-device -> constant null; presence-only bools.
                "has_text": has_text,
                "has_content_description": bool(rng.random() < 0.4),
                "text": None,
                "text_redacted": None,
                "content_desc_redacted": None,
                "actions_summary": ["ACTION_CLICK"] if clickable else [],
                "depth": depth,
            }
        )
    return nodes


def _class_name_for(editable: bool, scrollable: bool, checkable: bool, clickable: bool) -> str:
    """Pick a plausible Android widget class name from node booleans.

    Args:
        editable: Node is editable.
        scrollable: Node is scrollable.
        checkable: Node is checkable.
        clickable: Node is clickable.

    Returns:
        An Android widget class name string.
    """
    if editable:
        return "android.widget.EditText"
    if scrollable:
        return "androidx.recyclerview.widget.RecyclerView"
    if checkable:
        return "android.widget.Switch"
    if clickable:
        return "android.widget.Button"
    return "android.widget.TextView"


def _build_context_event(
    rng: np.random.Generator,
    profile: ScenarioProfile,
    *,
    event_time_wall_millis: int,
    package: str,
    activity: str,
    orientation: str,
) -> dict[str, Any]:
    """Build a single ``context_event`` with a NodeSnapshot tree.

    Args:
        rng: Seeded generator.
        profile: Scenario behavioural profile.
        event_time_wall_millis: Event wall-clock time (ms).
        package: Foreground package name.
        activity: Foreground activity class name.
        orientation: Coarse orientation string (LEAKAGE, kept in raw).

    Returns:
        A :class:`app.schemas.ContextEvent`-shaped dict.
    """
    event_type = str(rng.choice(profile.event_types))
    return {
        "event_id": _uuid_from(rng),
        "event_type": event_type,
        "event_time_wall_millis": int(event_time_wall_millis),
        "app_package_name": package,
        "foreground_activity_class_name": activity,
        "foreground_component_name": f"{package}/{activity}",
        "input_method_visible": bool(profile.ime_visible),
        # LEAKAGE (client-uploaded coarse orientation); excluded downstream.
        "coarse_orientation": orientation,
        "window_title_redacted": None,  # text dropped on device
        "root_nodes": _build_nodes(rng, profile),
        "redaction_summary": {
            "dropped_password_nodes": 0,
            "dropped_editable_texts": 0,
            "replaced_email": 0,
            "replaced_phone": 0,
            "replaced_url": 0,
            "replaced_number": 0,
            "replaced_card": 0,
            "replaced_id_number": 0,
        },
    }


def _summarize_nodes(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate node booleans into counts for a ``context_feature``.

    Args:
        nodes: The event's node list.

    Returns:
        Dict with editable/scrollable/clickable counts, password_seen, and a
        class histogram (short class name -> count).
    """
    editable = sum(1 for n in nodes if n.get("editable"))
    scrollable = sum(1 for n in nodes if n.get("scrollable"))
    clickable = sum(1 for n in nodes if n.get("clickable"))
    histogram: dict[str, int] = {}
    for node in nodes:
        cls = (node.get("class_name") or "Unknown").split(".")[-1]
        histogram[cls] = histogram.get(cls, 0) + 1
    return {
        "editable_count": editable,
        "scrollable_count": scrollable,
        "clickable_count": clickable,
        "password_node_seen": False,
        "node_class_histogram": histogram,
    }


def _build_context_feature(
    rng: np.random.Generator,
    profile: ScenarioProfile,
    *,
    event: dict[str, Any],
    computed_at_wall_millis: int,
    task_fields: dict[str, Any],
    scenario: str,
    orientation: str,
) -> dict[str, Any]:
    """Build a ``context_feature`` referencing an event, incl. leakage columns.

    The feature MUST echo the batch's task fields exactly (schema cross-check).
    It also carries the LEAKAGE columns ``game_like_score``,
    ``estimated_context_category`` and ``coarse_orientation`` (they exist in
    real data; the pipeline excludes them).

    Args:
        rng: Seeded generator.
        profile: Scenario behavioural profile.
        event: The referenced context event.
        computed_at_wall_millis: Feature computation wall time (ms).
        task_fields: The batch task-label fields (echoed here).
        scenario: The ground-truth scenario id (I0..I6).
        orientation: Coarse orientation string (LEAKAGE).

    Returns:
        A :class:`app.schemas.ContextFeature`-shaped dict.
    """
    node_summary = _summarize_nodes(event["root_nodes"])
    editable_count = node_summary["editable_count"]
    ime_visible = bool(profile.ime_visible)
    # LEAKAGE column raw values (excluded downstream) — plausible per I0..I6 class:
    # high-motion manipulation/rotation (I5/I6) look "game-like"; I0 is media;
    # I3/I4 are list/doc scrolling; I1/I2 are text-entry / control forms.
    game_like = 0.85 + 0.1 * rng.random() if scenario in {"I5", "I6"} else 0.05 * rng.random()
    media_like = 0.8 + 0.15 * rng.random() if scenario == "I0" else 0.1 * rng.random()
    list_like = 0.8 + 0.15 * rng.random() if scenario in {"I3", "I4"} else 0.1 * rng.random()
    form_like = 0.7 + 0.2 * rng.random() if scenario in {"I1", "I2"} else 0.1 * rng.random()
    return {
        "feature_id": _uuid_from(rng),
        "event_id": event["event_id"],
        "computed_at_wall_millis": int(computed_at_wall_millis),
        "collection_source": task_fields["collection_source"],
        "task_sequence": task_fields["task_sequence"],
        "task_id": task_fields["task_id"],
        "task_name": task_fields["task_name"],
        "task_intuitive_description": task_fields["task_intuitive_description"],
        "task_category": task_fields["task_category"],
        "task_session_id": task_fields["task_session_id"],
        "input_method_visible": ime_visible,
        "keyboard_visible_estimated": bool(ime_visible or editable_count > 0),
        "editable_count": editable_count,
        "scrollable_count": node_summary["scrollable_count"],
        "clickable_count": node_summary["clickable_count"],
        "password_node_seen": False,
        "media_like_score": round(float(media_like), 4),
        "list_like_score": round(float(list_like), 4),
        "form_like_score": round(float(form_like), 4),
        # --- LEAKAGE columns (present in raw; excluded downstream) ---
        "game_like_score": round(float(game_like), 4),
        "node_class_histogram": node_summary["node_class_histogram"],
        "event_type": event["event_type"],
        "coarse_orientation": orientation,
        "estimated_context_category": scenario,
    }


def _build_touch_events(
    rng: np.random.Generator,
    profile: ScenarioProfile,
    *,
    started_wall_millis: int,
    duration_sec: int,
) -> list[dict[str, Any]]:
    """Build timing-only touch events (no coordinates).

    Args:
        rng: Seeded generator.
        profile: Scenario behavioural profile.
        started_wall_millis: Wall-clock origin (ms).
        duration_sec: Batch duration (seconds).

    Returns:
        A list of :class:`app.schemas.TouchEvent`-shaped dicts, sorted by time.
    """
    n_touches = int(rng.poisson(max(0.0, profile.touch_rate_hz * duration_sec)))
    events: list[dict[str, Any]] = []
    for _ in range(n_touches):
        offset_ms = int(rng.integers(0, max(1, duration_sec * 1000)))
        wall = started_wall_millis + offset_ms
        uptime = 1_000_000 + offset_ms
        events.append(
            {
                "event_id": _uuid_from(rng),
                "event_type": str(rng.choice(TOUCH_TYPES)),
                "event_time_uptime_millis": int(uptime),
                "event_time_wall_millis": int(wall),
                "collected_at_wall_millis": int(wall + 1),
            }
        )
    events.sort(key=lambda e: e["event_time_wall_millis"])
    return events


def _build_batch(
    *,
    rng: np.random.Generator,
    device_id: str,
    session_id: str,
    day_id: str,
    scenario: str,
    started_wall_millis: int,
    base_elapsed_nanos: int,
    duration_sec: int,
    package: str,
    activity: str,
    drop_channel: str | None,
) -> dict[str, Any]:
    """Assemble a single schema-valid BUILTIN_TASK batch dict.

    Args:
        rng: Seeded generator for this batch.
        device_id: 64-hex device id.
        session_id: Session id (== task_session_id for BUILTIN_TASK).
        day_id: Human day id (for provenance only; not a schema field).
        scenario: Ground-truth scenario id (I0..I6).
        started_wall_millis: Batch start wall time (ms).
        base_elapsed_nanos: Elapsed-clock origin for sensor timestamps.
        duration_sec: Batch duration (seconds).
        package: Foreground package name.
        activity: Foreground activity class name.
        drop_channel: Sensor channel to omit (missing-stream sim) or ``None``.

    Returns:
        A batch dict that satisfies ``app.schemas.Batch``.
    """
    profile = PROFILES[scenario]
    ended_wall_millis = started_wall_millis + duration_sec * 1000
    task_name, intuition = SCENARIO_TASK_META[scenario]
    orientation = "landscape" if profile.landscape else "portrait"

    task_fields = {
        "collection_source": "BUILTIN_TASK",
        "task_sequence": int(scenario[1:]),
        "task_id": scenario,
        "task_name": task_name,
        "task_intuitive_description": intuition,
        "task_category": scenario,
        "task_session_id": session_id,
    }

    sensor_samples = _build_sensor_samples(
        rng,
        profile,
        base_elapsed_nanos=base_elapsed_nanos,
        started_wall_millis=started_wall_millis,
        duration_sec=duration_sec,
        drop_channel=drop_channel,
    )

    # Emit a handful of context events across the batch window.
    n_events = max(1, int(round(profile.event_rate_hz * duration_sec)))
    n_events = min(n_events, 12)
    context_events: list[dict[str, Any]] = []
    for i in range(n_events):
        evt_wall = started_wall_millis + int((i + 0.5) * duration_sec * 1000 / n_events)
        context_events.append(
            _build_context_event(
                rng,
                profile,
                event_time_wall_millis=evt_wall,
                package=package,
                activity=activity,
                orientation=orientation,
            )
        )

    # One context feature per event (references it by event_id), echoing task.
    context_features = [
        _build_context_feature(
            rng,
            profile,
            event=event,
            computed_at_wall_millis=event["event_time_wall_millis"] + 5,
            task_fields=task_fields,
            scenario=scenario,
            orientation=orientation,
        )
        for event in context_events
    ]

    touch_events = _build_touch_events(
        rng,
        profile,
        started_wall_millis=started_wall_millis,
        duration_sec=duration_sec,
    )

    batch: dict[str, Any] = {
        "batch_id": _uuid_from(rng),
        "device_id": device_id,
        "session_id": session_id,
        "record_type": "collection",
        "collection_source": "BUILTIN_TASK",
        "app_package_name": package,
        "foreground_activity_class_name": activity,
        "foreground_component_name": f"{package}/{activity}",
        "sampling_rate_hz": SAMPLING_RATE_HZ,
        "batch_duration_seconds": duration_sec,
        "task_sequence": task_fields["task_sequence"],
        "task_id": task_fields["task_id"],
        "task_name": task_fields["task_name"],
        "task_intuitive_description": task_fields["task_intuitive_description"],
        "task_category": task_fields["task_category"],
        "task_session_id": task_fields["task_session_id"],
        "task_started_at_wall_millis": started_wall_millis,
        "task_elapsed_seconds_at_batch_end": duration_sec,
        "app_version": APP_VERSION,
        "rule_version": RULE_VERSION,
        "rule_hash": RULE_HASH_ZEROS,
        "consent_version": CONSENT_VERSION,
        "started_at_wall_millis": started_wall_millis,
        "ended_at_wall_millis": ended_wall_millis,
        "base_elapsed_nanos": base_elapsed_nanos,
        "sensor_samples": sensor_samples,
        "touch_events": touch_events,
        "context_events": context_events,
        "context_features": context_features,
        "skip_events": [],
        "diagnostics": {
            "sensor_sample_count": len(sensor_samples),
            "context_event_count": len(context_events),
            "touch_event_count": len(touch_events),
            "redaction_applied": True,
            "compression": "lz4_frame",
            "encryption": "none",
            "sampling_rate_hz": SAMPLING_RATE_HZ,
        },
    }
    return batch


def _build_envelope(batch: dict[str, Any]) -> dict[str, Any]:
    """Wrap a batch into the 8-field ``LZ4_FRAME+JSON`` envelope.

    Compresses the canonical batch JSON with lz4-frame, hashes the COMPRESSED
    bytes (SHA-256), and base64-encodes the compressed payload -- matching the
    app producer / server consumer contract exactly.

    Args:
        batch: A schema-valid batch dict.

    Returns:
        An :class:`app.schemas.Envelope`-shaped dict.
    """
    batch_json = json.dumps(batch, ensure_ascii=False, sort_keys=True).encode("utf-8")
    compressed = lz4.frame.compress(batch_json)
    return {
        "algorithm": ALGORITHM,
        "payload_base64": base64.b64encode(compressed).decode("ascii"),
        "payload_sha256_hex": hashlib.sha256(compressed).hexdigest(),
        "device_id": batch["device_id"],
        "batch_id": batch["batch_id"],
        "rule_version": batch["rule_version"],
        "rule_hash": batch["rule_hash"],
        "created_at_wall_millis": batch["started_at_wall_millis"],
    }


def _write_batch(out: Path, batch: dict[str, Any]) -> Path:
    """Write a batch to ``devices/{device_id}/{date}/{batch_id}.json``.

    Uses the same canonical serialization as the server (sorted keys) and the
    same UTC date-dir derivation from ``started_at_wall_millis``.

    Args:
        out: Dataset root.
        batch: The batch dict to write.

    Returns:
        The written batch file path.
    """
    date = _date_dir_for(batch["started_at_wall_millis"])
    batch_dir = out / "devices" / batch["device_id"] / date
    batch_dir.mkdir(parents=True, exist_ok=True)
    path = batch_dir / f"{batch['batch_id']}.json"
    path.write_text(json.dumps(batch, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def _write_envelope(out: Path, envelope: dict[str, Any]) -> Path:
    """Write an envelope to ``envelopes/{batch_id}.json``.

    Args:
        out: Dataset root.
        envelope: The envelope dict to write.

    Returns:
        The written envelope file path.
    """
    env_dir = out / "envelopes"
    env_dir.mkdir(parents=True, exist_ok=True)
    path = env_dir / f"{envelope['batch_id']}.json"
    path.write_text(json.dumps(envelope, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def generate(
    *,
    users: int,
    days: int,
    sessions_per_day: int,
    out: Path,
    seed: int,
    emit_envelopes: bool = False,
) -> GenStats:
    """Generate the full synthetic dataset and write it to disk.

    Simulates, per user/day/session, a run of scenario batches. Each session
    picks a package bucket and a dominant scenario, with per-batch scenario
    mixing (label noise) and occasional missing sensor channels + app
    transitions. Determinism: every batch derives its own generator from a
    stable seed of (seed, user, day, session, batch_index).

    Args:
        users: Number of distinct synthetic users.
        days: Number of days per user.
        sessions_per_day: Number of sessions per day.
        out: Dataset root directory.
        seed: Base run seed.
        emit_envelopes: Also write 8-field LZ4 envelopes.

    Returns:
        A :class:`GenStats` with the generation counters.
    """
    out = Path(out)
    # Clean prior batch/envelope outputs so a fresh (seed, users, days) run is
    # deterministic and self-contained -- otherwise successive runs accumulate
    # stale devices/batches (data dirs are runtime-generated + gitignored).
    for stale in (out / "devices", out / "envelopes"):
        if stale.exists():
            shutil.rmtree(stale)
    stats = GenStats(users=users, days=days)

    # Wall-clock origin: a fixed epoch so date-dirs are deterministic. Each day
    # advances by 24h; sessions are spaced within the day.
    epoch_ms = 1_710_000_000_000  # fixed reference (matches test helpers' era)
    day_ms = 24 * 60 * 60 * 1000

    for user_index in range(users):
        device_id = _device_id_for_user(user_index, seed)
        # A per-user motion bias so users are (weakly) separable for auth.
        user_rng = _rng_for(seed, "user", user_index)
        user_bias = float(user_rng.normal(0.0, 0.03))

        for day in range(days):
            day_id = f"d{day}"
            stats.days = max(stats.days, day + 1)
            for session in range(sessions_per_day):
                stats.sessions += 1
                session_rng = _rng_for(seed, "session", user_index, day, session)
                session_id = _uuid_from(session_rng)

                # Session-level package bucket + dominant scenario.
                pkg_idx = int(session_rng.integers(0, len(PACKAGE_POOL)))
                package, activity = PACKAGE_POOL[pkg_idx]
                dominant = SCENARIOS[int(session_rng.integers(0, len(SCENARIOS)))]

                session_start_ms = epoch_ms + day * day_ms + session * (3 * 60 * 60 * 1000)
                base_elapsed = int(session_rng.integers(10**6, 10**9))

                # A short run of batches per session (5s each).
                n_batches = int(session_rng.integers(3, 7))
                cursor_ms = session_start_ms
                for batch_index in range(n_batches):
                    batch_rng = _rng_for(seed, "batch", user_index, day, session, batch_index)

                    # Label noise: 20% of batches drift to a neighbouring scenario.
                    if batch_rng.random() < 0.2:
                        scenario = SCENARIOS[int(batch_rng.integers(0, len(SCENARIOS)))]
                    else:
                        scenario = dominant

                    # Occasional app transition mid-session (10%).
                    if batch_rng.random() < 0.1:
                        alt_idx = int(batch_rng.integers(0, len(PACKAGE_POOL)))
                        package, activity = PACKAGE_POOL[alt_idx]

                    # Occasional missing sensor channel (8%).
                    drop_channel: str | None = None
                    if batch_rng.random() < 0.08:
                        drop_channel = SENSOR_TYPES[int(batch_rng.integers(0, len(SENSOR_TYPES)))]

                    duration_sec = 5
                    # Advance the elapsed clock so successive batches in a
                    # session have monotonically increasing sensor timestamps.
                    base_elapsed += int(batch_rng.integers(10**5, 10**6))

                    batch = _build_batch(
                        rng=batch_rng,
                        device_id=device_id,
                        session_id=session_id,
                        day_id=day_id,
                        scenario=scenario,
                        started_wall_millis=cursor_ms,
                        base_elapsed_nanos=base_elapsed,
                        duration_sec=duration_sec,
                        package=package,
                        activity=activity,
                        drop_channel=drop_channel,
                    )
                    # Apply a tiny deterministic per-user shift to accel means so
                    # identity is learnable without touching the schema.
                    if abs(user_bias) > 0:
                        for sample in batch["sensor_samples"]:
                            if sample["sensor_type"] == "ACCELEROMETER":
                                sample["z"] = round(sample["z"] + user_bias, 6)

                    _write_batch(out, batch)
                    stats.batches += 1
                    stats.sensor_samples += len(batch["sensor_samples"])
                    stats.scenario_counts[scenario] += 1

                    if emit_envelopes:
                        _write_envelope(out, _build_envelope(batch))
                        stats.envelopes += 1

                    cursor_ms += duration_sec * 1000 + int(batch_rng.integers(0, 2000))

    return stats


def _print_summary(stats: GenStats, out: Path) -> None:
    """Print a human-readable generation summary.

    Reports users/days/sessions/batches, total sensor samples (a proxy for
    "windows-worth of samples": at 100 Hz, 5s windows), and the per-scenario
    batch distribution.

    Args:
        stats: The generation counters.
        out: Dataset root (for the path line).
    """
    window_samples = SAMPLING_RATE_HZ * 5  # samples in one 5s window at 100 Hz
    approx_windows = stats.sensor_samples / max(1, window_samples)
    print("=== synthetic generation summary ===")
    print(f"out_dir           : {out.resolve()}")
    print(f"users             : {stats.users}")
    print(f"days (per user)   : {stats.days}")
    print(f"sessions          : {stats.sessions}")
    print(f"batches           : {stats.batches}")
    print(f"sensor_samples    : {stats.sensor_samples}")
    print(f"~windows (5s@100Hz, 3ch): {approx_windows:.1f}")
    print(f"envelopes         : {stats.envelopes}")
    print(f"scenario_counts   : {stats.scenario_counts}")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        A configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="generate_synthetic_data",
        description="Generate deterministic synthetic ContextAuth batches (+ optional LZ4 envelopes).",
    )
    parser.add_argument("--users", type=int, required=True, help="number of synthetic users")
    parser.add_argument("--days", type=int, required=True, help="days per user")
    parser.add_argument("--sessions-per-day", type=int, required=True, help="sessions per day")
    parser.add_argument("--out", type=Path, required=True, help="output dataset root directory")
    parser.add_argument("--seed", type=int, default=42, help="deterministic base seed")
    parser.add_argument(
        "--emit-envelopes",
        action="store_true",
        help="also write 8-field LZ4_FRAME+JSON envelopes under {out}/envelopes/",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector (defaults to ``sys.argv``).

    Returns:
        Process exit code (0 on success).
    """
    args = build_arg_parser().parse_args(argv)
    if args.users <= 0 or args.days <= 0 or args.sessions_per_day <= 0:
        raise SystemExit("--users, --days and --sessions-per-day must be positive")

    stats = generate(
        users=args.users,
        days=args.days,
        sessions_per_day=args.sessions_per_day,
        out=args.out,
        seed=args.seed,
        emit_envelopes=args.emit_envelopes,
    )
    _print_summary(stats, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
