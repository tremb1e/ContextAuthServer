"""Score-based weak labeling for the 7 interaction scenarios (I0..I6).

Each scenario gets MULTIPLE additive / subtractive labeling functions (LFs) —
not a single if/else (``_recon_spec.md`` §4). The per-class raw scores are
temperature-scaled and softmaxed into a probability vector; ``confidence`` is
``clip(top1_prob - top4_prob, 0, 1)``; ``entropy`` is the Shannon entropy of the
probabilities. A window is *low confidence* when ``max_prob < low_conf_prob`` or
``confidence < low_conf_margin``.

2026-07-03: the taxonomy moved from the retired ``C0..C6`` paper classes to the
app's own 7 task classes ``I0..I6``. The rules were re-keyed and split from the
old C-rules (initial heuristic port — quality calibration is deferred P1 work,
per ``20-server.md`` §B.2):

* **I0 STATIC_VIEWING** (quiet watching + video) <- old C0 QUIESCENT evidence,
  now ALSO absorbing the old C5 media cues: + low event rate, + stable UI,
  + low motion, + large stable surface at low motion (video), + landscape at low
  motion; − text/scroll/click, − high motion.
* **I1 TEXT_ENTRY** <- old C1 (unchanged): + text_changed, + focused editable,
  + editable nodes, + focus events; − no editable node.
* **I2 DISCRETE_TOUCH** <- old C3 (discrete navigation) + C4 (structured
  control) MERGED: + click/long_click, + window_state_changed, + form-like
  controls, + checked/selected change, + large UI-tree diff; − text_changed,
  − sustained scroll, − pure media/canvas.
* **I3 LIST_BROWSING** <- old C2 split #1 (list index evidence): + scroll,
  + scrollable nodes, + LIST container (``ui_list``), + item click/selection,
  + flick rhythm; − text_changed, − doc/webview container without a list.
* **I4 LONG_FORM_REVIEW** <- old C2 split #2 (continuous displacement evidence):
  + scroll, + DOC/webview container (``ui_webview``), + near-zero clicks,
  + long dwell, + continuous (non-list) scroll; − list container, − clicks,
  − text_changed. I3 vs I4 is decided by list-index cues (``ui_list`` + item
  taps) vs continuous-scroll doc cues (``ui_webview`` + long dwell, ~no clicks).
* **I5 OBJECT_MANIPULATION** <- old C6 split #1 (drag/annotate): + high touch
  density, + large canvas/surface, + mid/high motion, + landscape, + low
  semantic event rate; − pure wrist motion (high motion & ~no touch), − static.
* **I6 WRIST_ROTATION** <- old C6 split #2 (rotation): + very high accel/gyro
  energy, + gyro burst, + near-zero touch, + low UI event rate, + low node
  count; − high touch density, − low motion & stable UI. I5 vs I6 is decided by
  touch density (I5 high, I6 ~0) and motion extremity (I6 highest).

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


def _score_i0(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for I0 STATIC_VIEWING (quiet watching + video)."""
    score = 0.0
    fired: list[str] = []
    if f["evt_rate"] < 0.6:
        score += 1.0
        fired.append("I0:low_event_rate")
    if f["ui_stable_ms"] > 1500.0:
        score += 0.8
        fired.append("I0:stable_ui")
    if f["motion_energy_low"] > 0.7:
        score += 0.9
        fired.append("I0:low_motion")
    # Video signature: a large stable surface with low motion (absorbs the old
    # C5 media class — reading with NO surface also lands here via the cues above).
    if f["ui_surface_like"] > 0.5 and f["motion_energy_high"] < 0.1:
        score += 1.0
        fired.append("I0:media_surface_lowmotion")
    if f["orient_landscape"] > 0.5 and f["motion_energy_high"] < 0.1:
        score += 0.6
        fired.append("I0:landscape_video")
    if f["evt_textchanged_count"] > 0:
        score -= 1.5
        fired.append("I0:-text_changed")
    if f["evt_scroll_count"] > 0:
        score -= 1.2
        fired.append("I0:-scroll")
    if f["evt_click_count"] > 0:
        score -= 0.6
        fired.append("I0:-click")
    if f["motion_energy_high"] > 0.2:
        score -= 1.4
        fired.append("I0:-high_motion")
    return score, fired


def _score_i1(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for I1 TEXT_ENTRY (unchanged from old C1)."""
    score = 0.0
    fired: list[str] = []
    if f["evt_textchanged_count"] > 0:
        score += 1.6
        fired.append("I1:text_changed")
    if f["ui_focusable_count"] > 0 and f["ui_editable_count"] > 0:
        score += 1.0
        fired.append("I1:focused_editable")
    if f["ui_editable_count"] > 0:
        score += 0.9
        fired.append("I1:editable_nodes")
    if f["evt_focus_count"] > 0:
        score += 0.5
        fired.append("I1:focus_events")
    if f["ui_editable_count"] <= 0:
        score -= 1.2
        fired.append("I1:-no_editable")
    return score, fired


def _score_i2(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for I2 DISCRETE_TOUCH (old C3 + C4 merged)."""
    score = 0.0
    fired: list[str] = []
    if f["evt_click_count"] > 0 or f["evt_longclick_count"] > 0:
        score += 1.2
        fired.append("I2:click")
    if f["evt_windowstate_count"] > 0:
        score += 0.9
        fired.append("I2:window_state_changed")
    if f["ui_form_like_control_count"] > 0:
        score += 0.9
        fired.append("I2:form_like_controls")
    if f["ui_checked_count"] > 0 or f["ui_selected_count"] > 0:
        score += 0.8
        fired.append("I2:checked_or_selected")
    if f["ui_treediff_nodedelta"] > 4.0 or f["ui_treediff_categoryl1"] > 3.0:
        score += 0.5
        fired.append("I2:large_tree_diff")
    if f["evt_textchanged_count"] > 0:
        score -= 1.0
        fired.append("I2:-text_changed")
    if f["evt_scroll_count"] >= 3:
        score -= 1.2
        fired.append("I2:-sustained_scroll")
    if f["ui_surface_like"] > 0.5 and f["ui_node_count_mean"] < 6.0:
        score -= 1.0
        fired.append("I2:-pure_media_or_canvas")
    return score, fired


def _score_i3(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for I3 LIST_BROWSING (old C2 split: list scroll)."""
    score = 0.0
    fired: list[str] = []
    if f["evt_scroll_count"] > 0:
        score += 1.3
        fired.append("I3:scroll")
    if f["ui_scrollable_count"] > 0:
        score += 0.6
        fired.append("I3:scrollable_nodes")
    # LIST container is the key cue that separates I3 from the I4 doc/webview.
    if f["ui_list"] > 0.5:
        score += 1.2
        fired.append("I3:list_container")
    if f["evt_click_count"] > 0 or f["ui_selected_count"] > 0:
        score += 0.6
        fired.append("I3:item_select")
    if f["evt_scroll_count"] >= 2:
        score += 0.3
        fired.append("I3:flick_rhythm")
    if f["evt_textchanged_count"] > 0:
        score -= 1.0
        fired.append("I3:-text_changed")
    if f["ui_webview"] > 0.5 and f["ui_list"] < 0.5:
        score -= 0.8
        fired.append("I3:-doc_container")
    return score, fired


def _score_i4(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for I4 LONG_FORM_REVIEW (old C2 split: doc scroll)."""
    score = 0.0
    fired: list[str] = []
    if f["evt_scroll_count"] > 0:
        score += 1.1
        fired.append("I4:scroll")
    # DOC/webview container is the key cue that separates I4 from the I3 list.
    if f["ui_webview"] > 0.5:
        score += 1.2
        fired.append("I4:doc_container")
    if f["evt_click_count"] <= 0:
        score += 0.7
        fired.append("I4:near_zero_click")
    if f["ui_stable_ms"] > 2000.0:
        score += 0.5
        fired.append("I4:long_dwell")
    if f["ui_list"] < 0.5 and f["ui_scrollable_count"] > 0:
        score += 0.4
        fired.append("I4:continuous_scroll_not_list")
    if f["ui_list"] > 0.5:
        score -= 1.0
        fired.append("I4:-list_container")
    if f["evt_click_count"] > 0:
        score -= 0.5
        fired.append("I4:-clicks")
    if f["evt_textchanged_count"] > 0:
        score -= 1.0
        fired.append("I4:-text_changed")
    return score, fired


def _score_i5(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for I5 OBJECT_MANIPULATION (old C6 split: drag)."""
    score = 0.0
    fired: list[str] = []
    if f["touch_rate"] > 1.0:
        score += 1.2
        fired.append("I5:high_touch_density")
    if f["ui_surface_like"] > 0.5:
        score += 0.9
        fired.append("I5:large_canvas")
    if f["motion_energy_mid"] > 0.3 or f["motion_energy_high"] > 0.2:
        score += 0.7
        fired.append("I5:mid_high_motion")
    if f["orient_landscape"] > 0.5:
        score += 0.4
        fired.append("I5:landscape")
    if f["evt_rate"] < 0.8:
        score += 0.3
        fired.append("I5:low_semantic_event_rate")
    # Pure high-energy rotation with ~no touch is wrist motion (I6), not dragging.
    if f["motion_energy_high"] > 0.6 and f["touch_rate"] < 0.5:
        score -= 1.4
        fired.append("I5:-pure_wrist_motion")
    if f["motion_energy_low"] > 0.7 and f["ui_stable_ms"] > 2000.0:
        score -= 1.2
        fired.append("I5:-static")
    return score, fired


def _score_i6(f: dict[str, float]) -> tuple[float, list[str]]:
    """Additive/subtractive LFs for I6 WRIST_ROTATION (old C6 split: rotation)."""
    score = 0.0
    fired: list[str] = []
    if f["motion_energy_high"] > 0.3:
        score += 1.6
        fired.append("I6:high_motion_energy")
    if f["gyro_burst_count"] > 5 or f["gyro_mag_mean"] > 0.5:
        score += 1.1
        fired.append("I6:gyro_burst")
    # Near-zero touch is the key cue that separates I6 from the I5 canvas drag.
    if f["touch_rate"] < 0.5:
        score += 0.8
        fired.append("I6:near_zero_touch")
    if f["evt_rate"] < 0.6:
        score += 0.4
        fired.append("I6:low_ui_event_rate")
    if f["ui_node_count_mean"] < 8.0:
        score += 0.3
        fired.append("I6:low_node_count")
    if f["touch_rate"] > 1.0:
        score -= 1.2
        fired.append("I6:-high_touch")
    if f["motion_energy_low"] > 0.7 and f["ui_stable_ms"] > 2000.0:
        score -= 1.4
        fired.append("I6:-low_motion_stable_ui")
    return score, fired


#: Ordered scoring functions, one per scenario (index aligns with SCENARIOS).
_SCORERS = (_score_i0, _score_i1, _score_i2, _score_i3, _score_i4, _score_i5, _score_i6)


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
