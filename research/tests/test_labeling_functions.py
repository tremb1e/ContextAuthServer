"""Weak-labeling functions: per-class top1 + mixed top-k + no leakage — §15.1.4.

Asserts (for the I0..I6 taxonomy):

* for each of the 7 scenarios a hand-built synthetic feature dict (with that
  scenario's cues turned on) yields ``top1 == that scenario``;
* a scroll-ambiguous window keeps both list (I3) and long-form (I4) in top-k;
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
    # STATIC_VIEWING: a landscape video surface at low motion (also covers plain
    # reading via the low event-rate / stable-UI / low-motion cues).
    "I0": {"evt_rate": 0.1, "ui_stable_ms": 3000, "motion_energy_low": 0.95, "motion_energy_high": 0.0, "ui_surface_like": 1.0, "orient_landscape": 1.0, "ui_node_count_mean": 6, "touch_rate": 0.1},
    # TEXT_ENTRY: typing into a focused editable with the IME up.
    "I1": {"evt_textchanged_count": 5, "ui_editable_count": 3, "ui_focusable_count": 2, "evt_focus_count": 2, "evt_rate": 3.0},
    # DISCRETE_TOUCH: taps + window-state changes + structured controls.
    "I2": {"evt_click_count": 3, "evt_windowstate_count": 2, "ui_form_like_control_count": 3, "ui_checked_count": 2, "ui_treediff_nodedelta": 6, "evt_rate": 1.5, "ui_node_count_mean": 18},
    # LIST_BROWSING: list scroll with item selection.
    "I3": {"evt_scroll_count": 6, "ui_scrollable_count": 3, "ui_list": 1.0, "evt_click_count": 1, "ui_selected_count": 1, "evt_rate": 4.0, "touch_rate": 1.5, "ui_node_count_mean": 28},
    # LONG_FORM_REVIEW: continuous doc/webview scroll, ~no clicks, long dwell.
    "I4": {"evt_scroll_count": 6, "ui_webview": 1.0, "ui_scrollable_count": 2, "evt_click_count": 0, "ui_stable_ms": 5000, "ui_list": 0.0, "evt_rate": 2.0, "ui_node_count_mean": 22},
    # OBJECT_MANIPULATION: high-touch drag on a landscape canvas at mid motion.
    "I5": {"touch_rate": 4.0, "ui_surface_like": 1.0, "motion_energy_mid": 0.6, "motion_energy_high": 0.1, "orient_landscape": 1.0, "evt_rate": 0.3, "ui_node_count_mean": 6},
    # WRIST_ROTATION: extreme rotation energy, ~no touch, low UI event rate.
    "I6": {"motion_energy_high": 0.9, "gyro_burst_count": 10, "gyro_mag_mean": 1.0, "touch_rate": 0.0, "evt_rate": 0.3, "ui_node_count_mean": 4, "motion_energy_low": 0.0},
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


def test_mixed_scroll_window_topk_contains_list_and_longform() -> None:
    """A scroll-ambiguous window (list + doc cues) keeps both I3 and I4 in top-k."""
    mixed = {
        "evt_scroll_count": 6, "ui_scrollable_count": 3, "ui_list": 1.0, "ui_webview": 1.0,
        "evt_click_count": 0, "ui_stable_ms": 5000, "touch_rate": 1.0, "ui_node_count_mean": 25,
        "evt_rate": 3.0,
    }
    out = weak_label(mixed, topk_k=3)
    assert out["top1"] == "I3"
    assert "I4" in out["topk"], f"expected the long-form sibling to stay in top-k, got {out['topk']}"


def test_i6_absence_cues_are_gated_behind_motion() -> None:
    """A quiet, low-touch window with NO motion must NOT be labeled I6 (P1-a).

    I6's near-zero-touch / low-event cues are 'absence' signals a static-viewing
    window also satisfies. Without motion evidence they must not fire, so this
    motionless window (which WOULD score I6 top1 under the old ungated rules)
    no longer does.
    """
    quiet_no_motion = {
        "touch_rate": 0.0,
        "evt_rate": 0.5,
        "ui_node_count_mean": 3,
        "motion_energy_low": 0.3,
        "motion_energy_mid": 0.0,
        "motion_energy_high": 0.0,
        "gyro_burst_count": 0,
        "gyro_mag_mean": 0.0,
        "ui_stable_ms": 1000,
    }
    out = weak_label(quiet_no_motion)
    assert out["top1"] != "I6", f"quiet motionless window scored I6: {out['topk']}"
    assert "I6:near_zero_touch" not in out["fired_rules"]
    assert "I6:low_ui_event_rate" not in out["fired_rules"]


def test_i6_still_wins_with_real_motion() -> None:
    """With genuine rotation energy the gated cues fire and I6 is still top1."""
    wrist = {
        "touch_rate": 0.0, "evt_rate": 0.3, "ui_node_count_mean": 4,
        "motion_energy_high": 0.9, "gyro_burst_count": 10, "gyro_mag_mean": 1.0,
        "motion_energy_low": 0.0,
    }
    out = weak_label(wrist)
    assert out["top1"] == "I6"
    assert "I6:near_zero_touch" in out["fired_rules"]


def test_i5_canvas_cue_is_gated_behind_touch() -> None:
    """A large surface with ~no touch is video (I0), not a canvas drag (P1-c).

    I5's large-canvas cue is an object-manipulation signal that a quiet video
    window also satisfies (it shows a media surface). Without touch evidence the
    cue must not fire, so this no-touch / light-motion surface window is labeled
    static-viewing, not I5 manipulation.
    """
    video_no_touch = {
        "touch_rate": 0.2,          # below the 0.5 touch-evidence gate
        "ui_surface_like": 1.0,     # a large media surface (would fire ungated)
        "motion_energy_low": 0.9,   # light motion — device essentially still
        "motion_energy_mid": 0.1,
        "motion_energy_high": 0.0,
        "orient_landscape": 1.0,
        "evt_rate": 0.3,
        "ui_stable_ms": 3000,
        "ui_node_count_mean": 6,
    }
    out = weak_label(video_no_touch)
    assert out["top1"] == "I0", f"no-touch surface window scored {out['topk']}"
    assert "I5:large_canvas" not in out["fired_rules"]


def test_i5_still_wins_with_touch_drag() -> None:
    """With genuine touch the canvas cue fires and I5 is still top1."""
    drag = {
        "touch_rate": 4.0, "ui_surface_like": 1.0, "motion_energy_mid": 0.6,
        "motion_energy_high": 0.1, "orient_landscape": 1.0, "evt_rate": 0.3,
        "ui_node_count_mean": 6,
    }
    out = weak_label(drag)
    assert out["top1"] == "I5"
    assert "I5:large_canvas" in out["fired_rules"]


def test_leakage_columns_are_never_read() -> None:
    """Injecting huge values into the 4 leakage columns does not change the output."""
    base = {"evt_scroll_count": 6, "ui_scrollable_count": 3, "ui_list": 1.0, "evt_rate": 4.0, "touch_rate": 1.0, "ui_node_count_mean": 25}
    poisoned = dict(base)
    for col in LEAKAGE_COLUMNS:
        poisoned[col] = 1e6  # a value that WOULD dominate if it were ever read
    a = weak_label(base)
    b = weak_label(poisoned)
    assert np.allclose(np.asarray(a["probs"]), np.asarray(b["probs"]))
    assert a["top1"] == b["top1"] == "I3"


def test_topk_helper_orders_and_clamps() -> None:
    """``topk`` returns descending scenario ids and clamps k to [1, N]."""
    scores = np.array([0.0, 6.0, 1.0, 5.0, 2.0, 4.0, 3.0])  # argmax at I1
    assert topk(scores, 1) == ["I1"]
    assert topk(scores, 3) == ["I1", "I3", "I5"]
    assert len(topk(scores, 99)) == N_SCENARIOS  # clamped
    assert len(topk(scores, 0)) == 1  # clamped up to 1
    assert set(topk(scores, N_SCENARIOS)) == set(SCENARIOS)
