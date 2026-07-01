"""Score-based weak labeling for the 7 interaction scenarios (C0..C6).

Each scenario gets MULTIPLE additive / subtractive labeling functions (LFs) —
not a single if/else (``_recon_spec.md`` §4). The per-class raw scores are
temperature-scaled and softmaxed into a probability vector; ``confidence`` is
``clip(top1_prob - top4_prob, 0, 1)``; ``entropy`` is the Shannon entropy of the
probabilities. A window is *low confidence* when ``max_prob < low_conf_prob`` or
``confidence < low_conf_margin``.

Scoring cues per class (mirrors ``_recon_spec.md`` §4) use ONLY non-leakage
window features produced by :mod:`research.preprocessing.feature_extractors`:

* **C0 QUIESCENT**: + low event rate, + stable UI, + low/mid motion, + no large
  surface; − text_changed, − scroll, − click, − high motion.
* **C1 KEYBOARD**: + text_changed, + focused editable, + editable nodes, + IME
  visible; − no editable node.
* **C2 SCROLLING**: + scroll count, + scrollable nodes, + list/webview/scroll
  container; − text_changed, − large nav diff.
* **C3 NAVIGATION**: + click/long_click, + window_state_changed, + large UI-tree
  diff; − text_changed, − sustained high scroll.
* **C4 STRUCTURED_CONTROL**: + checkable/switch controls, + form-like controls,
  + checked/selected changes, + click with small/medium diff; − pure scroll,
  − pure media/canvas.
* **C5 MEDIA_PLAYBACK**: + large surface, + UI stable > 8s (proxy), + low event
  rate, + low/mid motion, + landscape (IMU-derived); − high motion, − text,
  − high scroll.
* **C6 CANVAS_HIGH_MOTION**: + large surface, + low UI node count, + high
  accel/gyro/mag energy, + motion burst, + high touch density, + low semantic
  event rate; − low motion & stable UI.

``LABEL_FEATURE_KEYS`` is the audited allow-list of feature columns the LFs may
read — it is asserted disjoint from ``research.LEAKAGE_COLUMNS`` at import time.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from research import LEAKAGE_COLUMNS, N_SCENARIOS, SCENARIOS

#: The only feature columns the labeling functions are permitted to read. This
#: is an explicit allow-list so the "no leakage feature used" test can verify it
#: by construction. ``orient_landscape`` is our own IMU-derived bool (allowed).
LABEL_FEATURE_KEYS: tuple[str, ...] = (
    # event family
    "evt_click_count",
    "evt_longclick_count",
    "evt_scroll_count",
    "evt_textchanged_count",
    "evt_focus_count",
    "evt_windowstate_count",
    "evt_windowcontent_count",
    "evt_rate",
    # UI family
    "ui_node_count_mean",
    "ui_editable_count",
    "ui_scrollable_count",
    "ui_focusable_count",
    "ui_checked_count",
    "ui_selected_count",
    "ui_surface_like",
    "ui_webview",
    "ui_list",
    "ui_scroll_indicator",
    "ui_form_like_control_count",
    "ui_stable_ms",
    "ui_treediff_nodedelta",
    "ui_treediff_categoryl1",
    # motion / orientation (IMU-derived; orient_landscape is allowed)
    "motion_energy_low",
    "motion_energy_mid",
    "motion_energy_high",
    "gyro_burst_count",
    "acc_mag_std",
    "gyro_mag_mean",
    "mag_mag_std",
    "orient_landscape",
    # touch density (added by the labeler from the window context)
    "touch_rate",
)

# Fail fast if the allow-list ever intersects the leakage set.
assert not (set(LABEL_FEATURE_KEYS) & LEAKAGE_COLUMNS), "labeling allow-list touches a leakage column"

#: UI-stable milliseconds above which media playback is more indicated.
_MEDIA_STABLE_MS = 2000.0


def softmax(scores: np.ndarray, temperature: float = 1.0) -> np.ndarray:
    """Numerically-stable temperature-scaled softmax.

    Args:
        scores: 1-D array of raw class scores.
        temperature: Softmax temperature (>0); higher ⇒ flatter distribution.

    Returns:
        A probability vector summing to 1.
    """
    temp = max(1e-6, float(temperature))
    logits = np.asarray(scores, dtype=np.float64) / temp
    logits = logits - float(np.max(logits))
    exp = np.exp(logits)
    total = float(exp.sum())
    if total <= 0:  # pragma: no cover - exp is strictly positive
        return np.full(scores.shape, 1.0 / scores.size)
    return exp / total


def _clip01(value: float) -> float:
    """Clip a float to ``[0, 1]``.

    Args:
        value: Input value.

    Returns:
        The value clamped to the unit interval.
    """
    return float(min(1.0, max(0.0, value)))


def _score_c0(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for C0 QUIESCENT_VIEWING."""
    score = 0.0
    fired: list[str] = []
    if f["evt_rate"] < 0.6:
        score += 1.0
        fired.append("C0:low_event_rate")
    if f["ui_stable_ms"] > 1500.0:
        score += 0.8
        fired.append("C0:stable_ui")
    if f["motion_energy_low"] > 0.7:
        score += 0.8
        fired.append("C0:low_motion")
    if f["ui_surface_like"] < 0.5:
        score += 0.4
        fired.append("C0:no_large_surface")
    if f["evt_textchanged_count"] > 0:
        score -= 1.5
        fired.append("C0:-text_changed")
    if f["evt_scroll_count"] > 0:
        score -= 1.0
        fired.append("C0:-scroll")
    if f["evt_click_count"] > 0:
        score -= 0.6
        fired.append("C0:-click")
    if f["motion_energy_high"] > 0.2:
        score -= 1.2
        fired.append("C0:-high_motion")
    if f["ui_surface_like"] > 0.5:
        # A large media/canvas surface is not quiescent reading UI.
        score -= 1.4
        fired.append("C0:-large_surface")
    return score, fired


def _score_c1(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for C1 KEYBOARD_TEXT_ENTRY."""
    score = 0.0
    fired: list[str] = []
    if f["evt_textchanged_count"] > 0:
        score += 1.6
        fired.append("C1:text_changed")
    if f["ui_focusable_count"] > 0 and f["ui_editable_count"] > 0:
        score += 1.0
        fired.append("C1:focused_editable")
    if f["ui_editable_count"] > 0:
        score += 0.9
        fired.append("C1:editable_nodes")
    if f["evt_focus_count"] > 0:
        score += 0.5
        fired.append("C1:focus_events")
    if f["ui_editable_count"] <= 0:
        score -= 1.2
        fired.append("C1:-no_editable")
    return score, fired


def _score_c2(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for C2 CONTINUOUS_SCROLLING."""
    score = 0.0
    fired: list[str] = []
    if f["evt_scroll_count"] > 0:
        score += 1.5
        fired.append("C2:scroll_count")
    if f["ui_scrollable_count"] > 0:
        score += 0.9
        fired.append("C2:scrollable_nodes")
    if f["ui_list"] > 0.5 or f["ui_webview"] > 0.5 or f["ui_scroll_indicator"] > 0.5:
        score += 0.8
        fired.append("C2:list_or_scroll_container")
    if f["evt_scroll_count"] >= 3:
        score += 0.5
        fired.append("C2:sustained_scroll")
    if f["evt_textchanged_count"] > 0:
        score -= 1.2
        fired.append("C2:-text_changed")
    if f["ui_treediff_nodedelta"] > 8.0:
        score -= 0.6
        fired.append("C2:-large_nav_diff")
    return score, fired


def _score_c3(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for C3 DISCRETE_NAVIGATION."""
    score = 0.0
    fired: list[str] = []
    if f["evt_click_count"] > 0 or f["evt_longclick_count"] > 0:
        score += 1.3
        fired.append("C3:click")
    if f["evt_windowstate_count"] > 0:
        score += 1.1
        fired.append("C3:window_state_changed")
    if f["ui_treediff_nodedelta"] > 4.0 or f["ui_treediff_categoryl1"] > 3.0:
        score += 0.8
        fired.append("C3:large_tree_diff")
    if f["evt_textchanged_count"] > 0:
        score -= 1.0
        fired.append("C3:-text_changed")
    if f["evt_scroll_count"] >= 3:
        score -= 1.0
        fired.append("C3:-sustained_scroll")
    return score, fired


def _score_c4(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for C4 STRUCTURED_CONTROL."""
    score = 0.0
    fired: list[str] = []
    if f["ui_form_like_control_count"] > 0:
        score += 1.2
        fired.append("C4:form_like_controls")
    if f["ui_checked_count"] > 0 or f["ui_selected_count"] > 0:
        score += 1.0
        fired.append("C4:checked_or_selected")
    if f["evt_click_count"] > 0 and f["ui_editable_count"] >= 1:
        score += 0.8
        fired.append("C4:click_with_controls")
    if f["evt_click_count"] > 0 and 0.0 < f["ui_treediff_nodedelta"] <= 4.0:
        score += 0.5
        fired.append("C4:small_diff_after_click")
    if f["evt_scroll_count"] >= 3:
        score -= 1.0
        fired.append("C4:-pure_scroll")
    if f["ui_surface_like"] > 0.5 and f["ui_node_count_mean"] < 6.0:
        score -= 1.0
        fired.append("C4:-pure_media_or_canvas")
    return score, fired


def _score_c5(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for C5 MEDIA_PLAYBACK."""
    score = 0.0
    fired: list[str] = []
    if f["ui_surface_like"] > 0.5:
        score += 1.4
        fired.append("C5:large_surface")
    # Media signature: a large surface over a small node tree with low motion
    # (distinguishes video playback from quiescent reading, which has a bigger
    # node tree and no surface).
    if f["ui_surface_like"] > 0.5 and f["ui_node_count_mean"] < 10.0 and f["motion_energy_high"] < 0.1:
        score += 1.3
        fired.append("C5:media_surface_lowmotion")
    if f["ui_stable_ms"] > _MEDIA_STABLE_MS:
        score += 0.7
        fired.append("C5:ui_stable")
    if f["evt_rate"] < 0.5:
        score += 0.6
        fired.append("C5:low_event_rate")
    if f["motion_energy_high"] < 0.1:
        score += 0.6
        fired.append("C5:low_motion")
    if f["orient_landscape"] > 0.5:
        score += 0.9
        fired.append("C5:landscape")
    if f["motion_energy_high"] > 0.3 or f["gyro_burst_count"] > 5:
        score -= 1.6
        fired.append("C5:-high_motion")
    if f["evt_textchanged_count"] > 0:
        score -= 1.0
        fired.append("C5:-text_changed")
    if f["evt_scroll_count"] >= 3:
        score -= 1.0
        fired.append("C5:-high_scroll")
    return score, fired


def _score_c6(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for C6 CANVAS_HIGH_MOTION."""
    score = 0.0
    fired: list[str] = []
    if f["ui_surface_like"] > 0.5:
        score += 0.8
        fired.append("C6:large_surface")
    if f["ui_node_count_mean"] < 8.0:
        score += 0.5
        fired.append("C6:low_node_count")
    if f["motion_energy_high"] > 0.3:
        score += 1.6
        fired.append("C6:high_motion_energy")
    if f["gyro_burst_count"] > 5 or f["gyro_mag_mean"] > 0.5:
        score += 1.0
        fired.append("C6:motion_burst")
    if f["touch_rate"] > 1.0:
        score += 0.6
        fired.append("C6:high_touch_density")
    if f["evt_rate"] < 0.6:
        score += 0.4
        fired.append("C6:low_semantic_event_rate")
    if f["motion_energy_low"] > 0.7 and f["ui_stable_ms"] > 2000.0:
        score -= 1.4
        fired.append("C6:-low_motion_stable_ui")
    return score, fired


#: Ordered scoring functions, one per scenario (index aligns with SCENARIOS).
_SCORERS = (_score_c0, _score_c1, _score_c2, _score_c3, _score_c4, _score_c5, _score_c6)


def _prepare_features(features: dict[str, float]) -> dict[str, float]:
    """Project the input feature dict onto the labeling allow-list.

    Only :data:`LABEL_FEATURE_KEYS` are read; missing keys default to 0.0. This
    guarantees the LFs never see a leakage column even if the caller passes a
    superset dict.

    Args:
        features: A window feature dict (may contain extra columns).

    Returns:
        A dict restricted to the allow-list, all values coerced to float.
    """
    return {key: float(features.get(key, 0.0)) for key in LABEL_FEATURE_KEYS}


def topk(probs_or_scores: np.ndarray, k: int) -> list[str]:
    """Return the top-``k`` scenario ids for a score/probability vector.

    Args:
        probs_or_scores: A length-7 array of per-scenario scores or probs.
        k: Number of ids to return (clamped to ``[1, N_SCENARIOS]``).

    Returns:
        The ``k`` scenario ids ordered by descending value.
    """
    k = int(min(max(1, k), N_SCENARIOS))
    order = np.argsort(np.asarray(probs_or_scores, dtype=np.float64))[::-1]
    return [SCENARIOS[i] for i in order[:k]]


def weak_label(
    features: dict[str, float],
    temperature: float = 1.0,
    *,
    low_conf_prob: float = 0.35,
    low_conf_margin: float = 0.10,
    topk_k: int = 3,
) -> dict[str, Any]:
    """Weakly label a window feature dict with the 7-class score-based LFs.

    Args:
        features: A window feature dict (only allow-listed keys are read). May
            include a ``touch_rate`` value; if absent it defaults to 0.
        temperature: Softmax temperature for turning scores into probs.
        low_conf_prob: If the max probability is below this ⇒ low confidence.
        low_conf_margin: If ``top1 - top4`` is below this ⇒ low confidence.
        topk_k: The ``k`` used to populate the returned ``topk`` list.

    Returns:
        A dict with keys ``probs`` (``np.ndarray[7]``), ``scores``
        (``np.ndarray[7]``), ``confidence`` (float), ``entropy`` (float),
        ``fired_rules`` (list[str]), ``top1`` (str), ``topk`` (list[str]),
        ``low_confidence`` (bool).
    """
    f = _prepare_features(features)

    scores = np.zeros(N_SCENARIOS, dtype=np.float64)
    fired_rules: list[str] = []
    for idx, scorer in enumerate(_SCORERS):
        score, fired = scorer(f)
        scores[idx] = score
        fired_rules.extend(fired)

    probs = softmax(scores, temperature=temperature)
    order = np.argsort(probs)[::-1]
    top1_prob = float(probs[order[0]])
    top4_prob = float(probs[order[3]]) if probs.size >= 4 else 0.0
    confidence = _clip01(top1_prob - top4_prob)

    nz = probs[probs > 0]
    entropy = float(-np.sum(nz * np.log(nz))) if nz.size else 0.0

    low_confidence = bool(top1_prob < low_conf_prob or confidence < low_conf_margin)

    return {
        "probs": probs,
        "scores": scores,
        "confidence": confidence,
        "entropy": entropy,
        "fired_rules": fired_rules,
        "top1": SCENARIOS[int(order[0])],
        "topk": topk(probs, topk_k),
        "low_confidence": low_confidence,
    }
