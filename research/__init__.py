"""ContextAuth research package.

Shared, frozen constants live here and are imported everywhere so the whole
package agrees on the C0..C6 scenario taxonomy, the leakage-column exclusion
set, and the 3-channel sensor parity. These values are defined ONCE (per the
build contract §2) and MUST NOT be redefined in other modules.
"""

from __future__ import annotations

# --- Frozen shared constants (build contract §2, VERBATIM) -----------------

#: The 7 interaction scenarios == 7 MoE experts. Ordinal index == list position.
SCENARIOS = ["C0", "C1", "C2", "C3", "C4", "C5", "C6"]

#: Human-readable canonical name for each scenario id.
SCENARIO_NAMES = {
    "C0": "QUIESCENT_VIEWING",
    "C1": "KEYBOARD_TEXT_ENTRY",
    "C2": "CONTINUOUS_SCROLLING",
    "C3": "DISCRETE_NAVIGATION",
    "C4": "STRUCTURED_CONTROL",
    "C5": "MEDIA_PLAYBACK",
    "C6": "CANVAS_HIGH_MOTION",
}

#: Number of scenarios / experts.
N_SCENARIOS = 7

#: Columns that leak the task label or app fingerprint. NEVER computed into
#: features and NEVER used by weak labels. The synthetic generator still emits
#: them in raw batches (they exist in real data); the pipeline just excludes
#: them. The IMU-derived landscape boolean is named ``orient_landscape`` and is
#: ALLOWED (it is our own signal, not a task-bound uploaded label).
LEAKAGE_COLUMNS = {
    "estimated_context_category",
    "game_like_score",
    "viewIdResourceName",
    "coarse_orientation",
}

#: The 3 IMU sensor channels, fully equal (accel/gyro/mag parity).
SENSOR_TYPES = ["ACCELEROMETER", "GYROSCOPE", "MAGNETIC_FIELD"]

# --- Convenience derived maps (not part of the frozen block) ----------------

#: scenario id -> ordinal index (C0 -> 0 ... C6 -> 6).
SCENARIO_INDEX = {scenario: index for index, scenario in enumerate(SCENARIOS)}

#: Raw task categories accepted in stored batches. ``C0..C6`` is the canonical
#: research taxonomy; ``I0..I7`` is the current Android app taxonomy observed in
#: ``ContextAuthlab``. The research pipeline maps raw task ids into canonical
#: ``C`` scenes and keeps the original value as ``raw_task_category``.
RAW_TASK_CATEGORIES = [*SCENARIOS, "I0", "I1", "I2", "I3", "I4", "I5", "I6", "I7"]

#: Post-hoc task->scene mappings. ``recommended`` is the default bridge from the
#: current app's eight task cards to the paper's seven experts. ``alt_c5_nav``
#: keeps the same seven-expert output space but treats the app's blended
#: discrete-control/object task differently for the mapping ablation.
_C_IDENTITY = {scene: scene for scene in SCENARIOS}
TASK_SCENE_MAPPINGS = {
    "recommended": {
        **_C_IDENTITY,
        "I0": "C0",  # static viewing / reading / passive video
        "I1": "C1",  # text entry
        "I2": "C3",  # discrete taps, menus and controls
        "I3": "C2",  # list browsing
        "I4": "C2",  # long-form review / scrolling
        "I5": "C6",  # object/canvas manipulation
        "I6": "C6",  # spatial capture / phone motion
        "I7": "C6",  # wrist rotation / high-motion canvas
    },
    "alt_c5_nav": {
        **_C_IDENTITY,
        "I0": "C0",
        "I1": "C1",
        "I2": "C4",  # emphasize structured controls in the blended task
        "I3": "C2",
        "I4": "C2",
        "I5": "C3",  # alternate: target/object manipulation as navigation
        "I6": "C6",
        "I7": "C6",
    },
}


def canonical_scene_for_task(task_category: str | None, mapping: str = "recommended") -> str | None:
    """Map a raw app task category into the canonical ``C0..C6`` scene id.

    Args:
        task_category: Raw batch ``task_category`` (``C*``, ``I*`` or ``None``).
        mapping: Mapping variant name, currently ``recommended`` or
            ``alt_c5_nav``.

    Returns:
        The canonical scene id, or ``None`` if the task is absent / unknown.

    Raises:
        ValueError: If the mapping variant is unknown.
    """
    if task_category is None:
        return None
    if mapping not in TASK_SCENE_MAPPINGS:
        raise ValueError(f"unknown task-scene mapping: {mapping!r}")
    return TASK_SCENE_MAPPINGS[mapping].get(str(task_category))


#: EN task name / intuitive description per scenario, mirroring the paper's
#: canonical C0..C6 taxonomy. Used by the synthetic generator so BUILTIN_TASK
#: batches carry contract-valid ``task_*`` fields.
SCENARIO_TASK_META = {
    "C0": ("Hold and read", "Quiescent viewing"),
    "C1": ("Paragraph copy", "Keyboard text entry"),
    "C2": ("Feed browsing", "Continuous scrolling"),
    "C3": ("Menu navigation", "Discrete navigation"),
    "C4": ("Simulated phone settings", "Multi-control operation"),
    "C5": ("Local video playback", "Media playback"),
    "C6": ("Wrist rotation", "Canvas high motion"),
}

__all__ = [
    "SCENARIOS",
    "SCENARIO_NAMES",
    "N_SCENARIOS",
    "LEAKAGE_COLUMNS",
    "SENSOR_TYPES",
    "SCENARIO_INDEX",
    "RAW_TASK_CATEGORIES",
    "TASK_SCENE_MAPPINGS",
    "canonical_scene_for_task",
    "SCENARIO_TASK_META",
]
