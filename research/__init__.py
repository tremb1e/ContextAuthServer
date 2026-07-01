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

#: EN task name / intuitive description per scenario, mirroring the app's
#: TaskCategory metadata (see _recon_contract.md §b). Used by the synthetic
#: generator so BUILTIN_TASK batches carry contract-valid ``task_*`` fields.
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
    "SCENARIO_TASK_META",
]
