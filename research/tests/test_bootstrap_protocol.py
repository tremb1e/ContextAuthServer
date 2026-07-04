"""§18.3 pooled-bootstrap protocol + same-index paired delta + Holm (SRV-3).

Asserts the PRIMARY bootstrap resamples USERS and recomputes the POOLED EER
(not the mean of per-user EERs), is deterministic, brackets the pooled point
estimate, differs from the legacy by-user vector CI, pairs configs on identical
replicate indices, and degrades to nan on a single user.
"""

from __future__ import annotations

import numpy as np

from research.experiments.bootstrap import (
    bootstrap_ci,
    holm_correction,
    pooled_bootstrap_ci,
    pooled_paired_delta,
    user_resample_indices,
)
from research.experiments.metrics import compute_eer_threshold


def _heterogeneous_pool(seed: int = 0):
    """Build an 8-user genuine/impostor pool with heterogeneous separability."""
    rng = np.random.default_rng(seed)
    labels, scores, users = [], [], []
    for u in range(8):
        sep = 0.2 + 0.05 * u  # users differ in separability
        for _ in range(10):
            labels.append(1); scores.append(rng.normal(0.5 + sep, 0.2)); users.append(f"u{u}")
            labels.append(0); scores.append(rng.normal(0.5 - sep, 0.2)); users.append(f"u{u}")
    return labels, scores, users


def test_pooled_bootstrap_deterministic() -> None:
    """Same seed -> identical CI; different seed -> different CI."""
    labels, scores, users = _heterogeneous_pool()
    a = pooled_bootstrap_ci(labels, scores, users, n_boot=200, seed=7)
    b = pooled_bootstrap_ci(labels, scores, users, n_boot=200, seed=7)
    assert a == b
    c = pooled_bootstrap_ci(labels, scores, users, n_boot=200, seed=8)
    assert (a["ci_lo"], a["ci_hi"]) != (c["ci_lo"], c["ci_hi"])


def test_pooled_ci_brackets_pooled_eer_and_differs_from_vector() -> None:
    """CI brackets the POOLED point EER; and differs from the by-user vector CI."""
    labels, scores, users = _heterogeneous_pool()
    pooled = pooled_bootstrap_ci(labels, scores, users, n_boot=400, seed=1)
    point, _ = compute_eer_threshold(labels, scores)
    assert pooled["ci_lo"] <= point <= pooled["ci_hi"]
    assert pooled["ci_lo"] < pooled["ci_hi"]
    assert abs(pooled["mean"] - point) < 1e-9
    # By-user vector法 (mean of per-user EER) gives a DIFFERENT CI (guards against
    # the implementation silently collapsing back to the forbidden estimator).
    from research.experiments.metrics import per_user_eer

    per_user = per_user_eer(labels, scores, users)
    _, vlo, vhi = bootstrap_ci(list(per_user.values()), seed=1)
    assert (round(vlo, 6), round(vhi, 6)) != (round(pooled["ci_lo"], 6), round(pooled["ci_hi"], 6))


def test_pooled_bootstrap_single_user_nan() -> None:
    """A single user -> all-nan CI + zero effective replicates (degenerate guard)."""
    res = pooled_bootstrap_ci([1, 0, 1, 0], [0.9, 0.1, 0.8, 0.2], ["u0"] * 4, n_boot=50, seed=0)
    assert res["n_boot_effective"] == 0
    assert np.isnan(res["mean"]) and np.isnan(res["ci_lo"]) and np.isnan(res["ci_hi"])


def test_pooled_paired_delta_same_scores_zero() -> None:
    """A==B (identical configs) -> every replicate delta is 0 (same-index pairing)."""
    labels, scores, users = _heterogeneous_pool()
    delta = pooled_paired_delta(labels, scores, users, labels, scores, users, n_boot=200, seed=3)
    assert abs(delta["delta_mean"]) < 1e-12
    assert abs(delta["ci_lo"]) < 1e-12 and abs(delta["ci_hi"]) < 1e-12
    assert delta["n_shared_users"] == 8


def test_user_resample_indices_shape_and_determinism() -> None:
    """The resample matrix is [n_boot, n_users] and seed-deterministic."""
    m1 = user_resample_indices(5, 100, seed=2)
    m2 = user_resample_indices(5, 100, seed=2)
    assert m1.shape == (100, 5)
    assert np.array_equal(m1, m2)
    assert m1.min() >= 0 and m1.max() < 5


def test_holm_monotone_nondecreasing() -> None:
    """Holm-adjusted p-values are monotone non-decreasing over the sorted family."""
    pvals = [0.001, 0.04, 0.5, 0.02]
    adj = holm_correction(pvals)
    order = np.argsort(pvals)
    sorted_adj = adj[order]
    assert np.all(np.diff(sorted_adj) >= -1e-12)
    assert np.all(sorted_adj <= 1.0 + 1e-12)
