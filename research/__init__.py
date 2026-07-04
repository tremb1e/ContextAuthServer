"""ContextAuth research package.

Shared, frozen constants live here and are imported everywhere so the whole
package agrees on the I0..I6 scenario taxonomy, the leakage-column exclusion
set, and the 3-channel sensor parity. These values are defined ONCE (per the
build contract §2) and MUST NOT be redefined in other modules.

2026-07-03 taxonomy change: the research gold/scene/expert space is now the
Android app's own 7 task classes ``I0..I6`` (1:1, no 8->7 bridge). The former
``C0..C6`` paper taxonomy and the old ``recommended``/``alt_c5_nav`` dual
mappings are RETIRED. Legacy on-disk data (task ids ``I7`` and ``C0..C6``, plus
the deleted spatial-capture ``I6`` "scan" task) is digested by
:func:`canonical_scene_for_task` — see its docstring and §3 of
``00-common.md``.
"""

from __future__ import annotations

# --- Frozen shared constants (build contract §2, VERBATIM) -----------------

#: The 7 interaction scenarios == 7 MoE experts == the app's 7 task classes.
#: Ordinal index == list position (``I0`` -> 0 ... ``I6`` -> 6).
SCENARIOS = ["I0", "I1", "I2", "I3", "I4", "I5", "I6"]

#: Human-readable canonical name for each scenario id (matches the app task
#: taxonomy; ``I6`` is wrist rotation after the ``I7`` -> ``I6`` renumbering).
SCENARIO_NAMES = {
    "I0": "STATIC_VIEWING",
    "I1": "TEXT_ENTRY",
    "I2": "DISCRETE_TOUCH",
    "I3": "LIST_BROWSING",
    "I4": "LONG_FORM_REVIEW",
    "I5": "OBJECT_MANIPULATION",
    "I6": "WRIST_ROTATION",
}

#: Number of scenarios / experts.
N_SCENARIOS = 7

#: Columns that leak the task label or app fingerprint. NEVER computed into
#: features and NEVER used by weak labels. The synthetic generator still emits
#: them in raw batches (they exist in real data); the pipeline just excludes
#: them. The IMU-derived landscape boolean is named ``orient_landscape`` and is
#: ALLOWED (it is our own signal, not a task-bound uploaded label).
#: ``media_like_score`` / ``list_like_score`` / ``form_like_score`` are the
#: task-correlated app-uploaded siblings of ``game_like_score`` (they hint at the
#: interaction class) and are excluded on the same grounds (2026-07-04 P2-a).
LEAKAGE_COLUMNS = {
    "estimated_context_category",
    "game_like_score",
    "media_like_score",
    "list_like_score",
    "form_like_score",
    "viewIdResourceName",
    "coarse_orientation",
}

#: The 3 IMU sensor channels, fully equal (accel/gyro/mag parity).
SENSOR_TYPES = ["ACCELEROMETER", "GYROSCOPE", "MAGNETIC_FIELD"]

# --- Convenience derived maps (not part of the frozen block) ----------------

#: scenario id -> ordinal index (I0 -> 0 ... I6 -> 6).
SCENARIO_INDEX = {scenario: index for index, scenario in enumerate(SCENARIOS)}

#: Raw task categories that can appear in stored batches. ``I0..I6`` is the
#: current (canonical) taxonomy; ``I7`` and ``C0..C6`` are LEGACY ids kept only
#: for backward compatibility with old APK / old on-disk data. The research
#: pipeline maps raw task ids into canonical scenes via
#: :func:`canonical_scene_for_task` and keeps the original value as
#: ``raw_task_category``.
LEGACY_TASK_CATEGORIES = ["I7", "C0", "C1", "C2", "C3", "C4", "C5", "C6"]
RAW_TASK_CATEGORIES = [*SCENARIOS, *LEGACY_TASK_CATEGORIES]

#: Legacy ``task_name`` values of the DELETED spatial-capture task (old ``I6``).
#: A batch with ``task_category == "I6"`` AND one of these names is old
#: scan-and-capture data: it is dropped from gold (scene=None). A batch with
#: ``task_category == "I6"`` and any other name is the NEW wrist-rotation task.
LEGACY_SCAN_TASK_NAMES = {"Scan, frame, and capture", "扫描取景与拍摄"}

#: Legacy ``task_name`` values of the wrist-rotation task (old ``I7`` == new
#: ``I6``). Documented for clarity; the ``I7`` -> ``I6`` remap is unconditional
#: (it does not depend on the name).
LEGACY_WRIST_TASK_NAMES = {"Wrist rotation", "手腕转动"}


def canonical_scene_for_task(task_category: str | None, task_name: str | None = None) -> str | None:
    """Map a raw app ``task_category`` (+ optional ``task_name``) to a gold scene.

    The gold/scene space is the canonical ``I0..I6`` taxonomy. Legacy on-disk
    data is digested per ``00-common.md`` §3:

    * ``I0..I5`` -> the same id (identity).
    * ``I6`` -> ``None`` when ``task_name`` is a deleted spatial-capture "scan"
      name (:data:`LEGACY_SCAN_TASK_NAMES`); otherwise ``I6`` (the new
      wrist-rotation task, incl. future APK data whose name is "Wrist rotation").
    * ``I7`` -> ``I6`` (old wrist rotation, unconditionally renumbered).
    * ``C0..C6`` -> ``None`` (retired paper taxonomy; the raw C payloads have
      been removed from disk — this is purely defensive).
    * ``None`` / unknown id (e.g. ``I8``) -> ``None`` (no gold label).

    Args:
        task_category: Raw batch ``task_category`` (``I*``, ``C*`` or ``None``).
        task_name: Raw batch ``task_name``; only consulted to disambiguate the
            new wrist ``I6`` from the deleted scan ``I6``.

    Returns:
        The canonical scene id, or ``None`` when the task has no gold scene.
    """
    if task_category is None:
        return None
    cat = str(task_category)
    if cat in ("I0", "I1", "I2", "I3", "I4", "I5"):
        return cat
    if cat == "I6":
        # New I6 == wrist rotation. Legacy I6 was the deleted spatial-capture
        # "scan" task; drop those windows from gold (scene=None).
        if task_name is not None and str(task_name) in LEGACY_SCAN_TASK_NAMES:
            return None
        return "I6"
    if cat == "I7":
        # Old wrist rotation, renumbered to the new I6 (unconditional).
        return "I6"
    # C0..C6 (retired taxonomy) and any unknown id have no gold scene.
    return None


#: EN task name / intuitive description per scenario, VERBATIM from the Android
#: app task enum (``ContextAuthlab``). Used by the synthetic generator so
#: BUILTIN_TASK batches carry contract-valid, app-identical ``task_*`` fields.
#: I0 uses the canonical "Quiet watching and video" (the historical "Quiet
#: viewing and video" is a legacy variant only present in 2026-07-03 on-disk
#: data; never emitted here).
SCENARIO_TASK_META = {
    "I0": ("Quiet watching and video", "Static viewing"),
    "I1": ("Text entry and editing", "Text entry"),
    "I2": ("Discrete taps and controls", "Discrete touch"),
    "I3": ("List scrolling and selection", "List browsing"),
    "I4": ("Long-document review", "Long-form review"),
    "I5": ("Annotate, draw, and drag", "Object manipulation"),
    "I6": ("Wrist rotation", "Wrist rotation"),
}

__all__ = [
    "SCENARIOS",
    "SCENARIO_NAMES",
    "N_SCENARIOS",
    "LEAKAGE_COLUMNS",
    "SENSOR_TYPES",
    "SCENARIO_INDEX",
    "RAW_TASK_CATEGORIES",
    "LEGACY_TASK_CATEGORIES",
    "LEGACY_SCAN_TASK_NAMES",
    "LEGACY_WRIST_TASK_NAMES",
    "canonical_scene_for_task",
    "SCENARIO_TASK_META",
]
