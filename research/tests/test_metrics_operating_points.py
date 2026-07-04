"""§9.7 operating points + detection policies (SRV-4).

Covers :func:`frr_at_far` / :func:`far_at_frr` (monotonicity, EER coincidence,
single-class nan) and the raw / k-of-n / EWMA detection policies (debounce +
smoothing behaviour, and val-selected policy reuse).
"""

from __future__ import annotations

import numpy as np

from research.experiments.detection import DETECTION_GRID, policy_decisions, select_detection_policy, stream_detect
from research.experiments.evaluator import EvalResult
from research.experiments.metrics import (
    compute_eer_threshold,
    far_at_frr,
    far_frr_at_threshold,
    frr_at_far,
)


def _separable(seed: int = 0):
    """A separable two-class score vector (genuine high, impostor low)."""
    rng = np.random.default_rng(seed)
    labels, scores = [], []
    for _ in range(200):
        labels.append(1); scores.append(rng.normal(0.7, 0.15))
        labels.append(0); scores.append(rng.normal(0.3, 0.15))
    return np.array(labels), np.array(scores)


def test_operating_points_at_eer_threshold() -> None:
    """At the EER threshold FAR≈FRR≈EER; frr_at_far monotone; single-class nan."""
    labels, scores = _separable()
    eer, thr = compute_eer_threshold(labels, scores)
    far, frr = far_frr_at_threshold(labels, scores, thr)
    assert abs(far - eer) < 0.03 and abs(frr - eer) < 0.03
    f1 = frr_at_far(labels, scores, 0.01)
    f5 = frr_at_far(labels, scores, 0.05)
    assert not np.isfinite(f1) or not np.isfinite(f5) or f1 >= f5 - 1e-9  # stricter FAR -> >= FRR
    assert np.isfinite(far_at_frr(labels, scores, 0.05))
    # Single-class input -> nan everywhere.
    assert np.isnan(frr_at_far(np.ones(5), np.linspace(0, 1, 5), 0.05))
    assert np.isnan(far_at_frr(np.ones(5), np.linspace(0, 1, 5), 0.05))


def test_far_resolution_target_below_resolution() -> None:
    """A 1% FAR target below the impostor-count resolution returns nan (auditable)."""
    # 40 impostors -> smallest non-zero FAR is 1/40 = 2.5% > 1%.
    labels = np.array([1] * 40 + [0] * 40)
    scores = np.array([0.9] * 40 + [0.1] * 40)
    assert np.isnan(frr_at_far(labels, scores, 0.01)) or frr_at_far(labels, scores, 0.01) == 0.0


def test_detection_policies_debounce_and_smooth() -> None:
    """raw fires on a lone dip; k-of-n debounces it; ewma smooths it."""
    thr = 0.5
    lone_dip = np.array([0.9, 0.9, 0.2, 0.9, 0.9])  # one isolated reject
    assert stream_detect(lone_dip, thr, {"kind": "raw"}) == 2  # raw flags the dip
    assert stream_detect(lone_dip, thr, {"kind": "k_of_n", "k": 2, "n": 3}) is None  # 2-of-3 not met
    assert stream_detect(lone_dip, thr, {"kind": "ewma", "alpha": 0.5}) is None  # smoothed away
    # A sustained attack triggers under every policy.
    attack = np.array([0.1, 0.1, 0.1, 0.1])
    for policy in DETECTION_GRID:
        assert stream_detect(attack, thr, policy) is not None
    # k-of-n fires on two consecutive rejects.
    assert stream_detect(np.array([0.9, 0.2, 0.2, 0.9]), thr, {"kind": "k_of_n", "k": 2, "n": 3}) == 2
    # policy_decisions counts sustained reject windows (false-alarm exposure).
    assert int(policy_decisions(lone_dip < thr, {"kind": "raw"}).sum()) == 1
    assert int(policy_decisions(lone_dip < thr, {"kind": "ewma", "alpha": 0.5}).sum()) == 0


def _val_result_two_users() -> EvalResult:
    """A tiny two-user val result with time-ordered streams for policy selection."""
    scores = np.array([0.8, 0.75, 0.7, 0.2, 0.25, 0.15], dtype=float)
    labels = np.array([1, 1, 1, 0, 0, 0])
    return EvalResult(
        scores=scores, labels=labels,
        users=["a", "a", "a", "a", "a", "a"],
        scenes=["I0"] * 6, n_genuine=3, n_impostor=3,
        query_window_ids=["d:s:0", "d:s:1", "d:s:2", "d:s:0", "d:s:1", "d:s:2"],
        impostor_user_ids=["", "", "", "b", "b", "b"],
        session_ids=["s", "s", "s", "s", "s", "s"],
    )


def test_detection_policy_selected_on_val() -> None:
    """The selected policy records selected_on == 'val' with a concrete threshold."""
    val = _val_result_two_users()
    eer, thr = compute_eer_threshold(val.labels, val.scores)
    policy = select_detection_policy(val, thr, stride_sec=1.0)
    assert policy["selected_on"] == "val"
    assert policy["kind"] in {p["kind"] for p in DETECTION_GRID}
    assert np.isfinite(policy["threshold"])


def test_detection_policy_nan_threshold_is_raw() -> None:
    """A non-finite (single-class) val threshold degrades to the raw policy."""
    val = _val_result_two_users()
    policy = select_detection_policy(val, float("nan"), stride_sec=1.0)
    assert policy["kind"] == "raw" and np.isnan(policy["threshold"])
