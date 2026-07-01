"""Weak-labeling functions: per-class top1 + mixed top-k + no leakage — §15.1.4.

Asserts:

* for each of the 7 scenarios a hand-built synthetic feature dict (with that
  scenario's cues turned on) yields ``top1 == that scenario``;
* a mixed window's ``topk`` contains the expected competing scenarios;
* the labeler reads ONLY its allow-list (``LABEL_FEATURE_KEYS``), which is
  disjoint from ``LEAKAGE_COLUMNS`` — injecting large values into the 4 leakage
  columns does NOT change the output (they are never read);
* the output contract (probs sum to 1, confidence in [0,1], entropy >= 0).
"""

from __future__ import annotations

import numpy as np

from research import LEAKAGE_COLUMNS, N_SCENARIOS, SCENARIOS
from research.labeling.interaction_states import LABEL_FEATURE_KEYS, topk, weak_label

# Per-scenario cue dicts (only allow-list keys are read by the labeler).
_PER_CLASS_CUES: dict[str, dict[str, float]] = {
    "C0": {"evt_rate": 0.1, "ui_stable_ms": 3000, "motion_energy_low": 0.95, "motion_energy_high": 0.0, "ui_surface_like": 0.0, "ui_node_count_mean": 12},
    "C1": {"evt_textchanged_count": 5, "ui_editable_count": 3, "ui_focusable_count": 2, "evt_focus_count": 2, "evt_rate": 3.0},
    "C2": {"evt_scroll_count": 6, "ui_scrollable_count": 3, "ui_list": 1.0, "ui_scroll_indicator": 1.0, "evt_rate": 4.0},
    "C3": {"evt_click_count": 3, "evt_windowstate_count": 2, "ui_treediff_nodedelta": 10, "ui_treediff_categoryl1": 5, "evt_rate": 1.5},
    "C4": {"ui_form_like_control_count": 3, "ui_checked_count": 2, "ui_selected_count": 1, "evt_click_count": 2, "ui_editable_count": 2, "ui_treediff_nodedelta": 2.0, "evt_rate": 1.2},
    "C5": {"ui_surface_like": 1.0, "ui_node_count_mean": 5, "motion_energy_high": 0.0, "ui_stable_ms": 9000, "evt_rate": 0.05, "orient_landscape": 1.0},
    "C6": {"ui_surface_like": 1.0, "ui_node_count_mean": 4, "motion_energy_high": 0.9, "gyro_burst_count": 10, "gyro_mag_mean": 1.0, "touch_rate": 4.0, "evt_rate": 0.3, "motion_energy_low": 0.0},
}


def test_allow_list_is_leakage_free() -> None:
    """The labeling allow-list never intersects the leakage-column set."""
    assert set(LABEL_FEATURE_KEYS).isdisjoint(LEAKAGE_COLUMNS)


def test_per_class_top1_is_correct() -> None:
    """Each scenario's cue dict makes that scenario the top-1 weak label."""
    for scene, cues in _PER_CLASS_CUES.items():
        out = weak_label(cues)
        assert out["top1"] == scene, f"{scene}: got top1={out['top1']} (topk={out['topk']})"
        # Output contract.
        probs = np.asarray(out["probs"], dtype=float)
        assert probs.shape == (N_SCENARIOS,)
        assert abs(float(probs.sum()) - 1.0) < 1e-6
        assert 0.0 <= out["confidence"] <= 1.0
        assert out["entropy"] >= 0.0
        assert out["topk"][0] == scene


def test_mixed_window_topk_contains_expected() -> None:
    """A structured-control window that also clicks/navigates keeps C3 in top-k."""
    mixed = {
        "ui_form_like_control_count": 3, "ui_checked_count": 2, "evt_click_count": 2,
        "ui_editable_count": 2, "evt_windowstate_count": 2, "ui_treediff_nodedelta": 3.0,
        "evt_rate": 1.2,
    }
    out = weak_label(mixed, topk_k=3)
    assert out["top1"] == "C4"
    assert "C3" in out["topk"], f"expected navigation cue to keep C3 in top-k, got {out['topk']}"


def test_leakage_columns_are_never_read() -> None:
    """Injecting huge values into the 4 leakage columns does not change the output."""
    base = {"evt_scroll_count": 6, "ui_scrollable_count": 3, "ui_list": 1.0, "evt_rate": 4.0}
    poisoned = dict(base)
    for col in LEAKAGE_COLUMNS:
        poisoned[col] = 1e6  # a value that WOULD dominate if it were ever read
    a = weak_label(base)
    b = weak_label(poisoned)
    assert np.allclose(np.asarray(a["probs"]), np.asarray(b["probs"]))
    assert a["top1"] == b["top1"] == "C2"


def test_topk_helper_orders_and_clamps() -> None:
    """``topk`` returns descending scenario ids and clamps k to [1, N]."""
    scores = np.array([0.0, 6.0, 1.0, 5.0, 2.0, 4.0, 3.0])  # argmax at C1
    assert topk(scores, 1) == ["C1"]
    assert topk(scores, 3) == ["C1", "C3", "C5"]
    assert len(topk(scores, 99)) == N_SCENARIOS  # clamped
    assert len(topk(scores, 0)) == 1  # clamped up to 1
    assert set(topk(scores, N_SCENARIOS)) == set(SCENARIOS)
