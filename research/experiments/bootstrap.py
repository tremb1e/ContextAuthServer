"""Bootstrap CIs (pooled + by-user), Holm correction, paired delta (§18.3).

* :func:`pooled_bootstrap_ci` — the exp_prompt §18.3 PRIMARY protocol: resample
  **users** with replacement, rebuild the genuine + matched-impostor pairs of the
  resampled users, and RECOMPUTE the pooled EER each replicate (not the mean of
  per-user EERs). Returns ``{mean, ci_lo, ci_hi, n_boot_effective}``.
* :func:`user_resample_indices` — the shared seed-deterministic user-resample
  index matrix, so an M7-vs-baseline paired delta is computed on the SAME
  replicate indices (§18.3 "paired delta on the same resample indices").
* :func:`bootstrap_ci` — the legacy by-user vector bootstrap (HMOG parity);
  RETAINED as a SECONDARY report only. It resamples a per-user EER vector and
  returns its mean CI — this is the "mean of per-user EER" estimator §18.3 marks
  as NOT the main protocol; prefer :func:`pooled_bootstrap_ci`.
* :func:`holm_correction` — mirrors HMOG ``pipeline.py`` §3a step-down
  Holm-Bonferroni: NaN-safe, preserves input order, enforces monotone
  non-decreasing adjusted p.
* :func:`paired_permutation_p` — mirrors HMOG §4 sign-flip permutation p.
* :func:`paired_delta` — the §4 "compare two configs" recipe: paired diff over
  matched users, ``delta_mean``, bootstrap CI on the diff, Wilcoxon signed-rank
  (with a binomial **sign-test fallback** when >50 % ties or Wilcoxon is
  undefined), Cohen's d and win-rate.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
from scipy.stats import binomtest, wilcoxon

from research.experiments.metrics import compute_eer_threshold


def user_resample_indices(n_users: int, n_boot: int, seed: int) -> np.ndarray:
    """Return a seed-deterministic ``[n_boot, n_users]`` user-resample index matrix.

    Row ``b`` holds ``n_users`` indices in ``[0, n_users)`` drawn with replacement
    — the users kept in bootstrap replicate ``b``. Sharing this one matrix across
    configurations realises the §18.3 rule that an M7-vs-baseline paired delta is
    evaluated on the SAME resampled users each replicate.

    Args:
        n_users: Number of distinct users to resample from.
        n_boot: Number of bootstrap replicates.
        seed: Deterministic RNG seed.

    Returns:
        An integer array of shape ``[n_boot, n_users]`` (empty columns if
        ``n_users == 0``).
    """
    rng = np.random.default_rng(seed)
    if n_users <= 0:
        return np.zeros((int(n_boot), 0), dtype=int)
    return rng.integers(0, int(n_users), size=(int(n_boot), int(n_users)))


def _rows_by_user(users: Sequence[object]) -> tuple[list[str], dict[str, np.ndarray]]:
    """Return ``(sorted_users, {user: row_indices})`` for a per-pair user vector."""
    users_arr = np.asarray([str(u) for u in users], dtype=object)
    uniq = sorted(set(users_arr.tolist()))
    return uniq, {u: np.where(users_arr == u)[0] for u in uniq}


def pooled_bootstrap_ci(
    labels: Sequence[float],
    scores: Sequence[float],
    users: Sequence[object],
    *,
    n_boot: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
    resample_matrix: np.ndarray | None = None,
) -> dict[str, float]:
    """§18.3 primary CI: resample users → rebuild pairs → recompute POOLED EER.

    Each pair (row) is attributed to its attacked genuine user; resampling whole
    user blocks with replacement carries that user's genuine AND matched-impostor
    rows together (the impostor pairs stay scene-matched by construction), and the
    pooled EER is recomputed on the resampled multiset. The returned ``mean`` is
    the pooled point EER on the full data; ``ci_lo``/``ci_hi`` are the
    ``[alpha/2, 1-alpha/2]`` quantiles of the replicate EERs.

    Args:
        labels: Binary labels (1 == genuine) aligned with ``scores``/``users``.
        scores: Match scores (larger == genuine).
        users: The attacked (genuine) user id per pair.
        n_boot: Number of bootstrap replicates (ignored if ``resample_matrix``).
        seed: Deterministic RNG seed (ignored if ``resample_matrix`` is given).
        alpha: Two-sided level (0.05 -> 95 % CI).
        resample_matrix: Optional pre-built ``user_resample_indices`` (columns ==
            number of distinct users here, sorted); lets callers pair configs on
            identical replicates.

    Returns:
        ``{mean, ci_lo, ci_hi, n_boot_effective}`` — all ``nan`` (and
        ``n_boot_effective == 0``) when there are fewer than two users or no
        finite replicate EER.
    """
    labels_arr = np.asarray(labels)
    scores_arr = np.asarray(scores, dtype=float)
    uniq, rows = _rows_by_user(users)
    nan_result = {"mean": float("nan"), "ci_lo": float("nan"), "ci_hi": float("nan"), "n_boot_effective": 0}
    if len(uniq) < 2:
        return nan_result
    matrix = resample_matrix if resample_matrix is not None else user_resample_indices(len(uniq), n_boot, seed)
    if matrix.shape[1] != len(uniq):
        matrix = user_resample_indices(len(uniq), matrix.shape[0], seed)
    replicate_eers: list[float] = []
    for b in range(matrix.shape[0]):
        picked_rows = np.concatenate([rows[uniq[j]] for j in matrix[b]]) if matrix.shape[1] else np.empty(0, dtype=int)
        eer, _ = compute_eer_threshold(labels_arr[picked_rows], scores_arr[picked_rows])
        if np.isfinite(eer):
            replicate_eers.append(float(eer))
    if not replicate_eers:
        return nan_result
    arr = np.asarray(replicate_eers, dtype=float)
    point, _ = compute_eer_threshold(labels_arr, scores_arr)
    return {
        "mean": float(point),
        "ci_lo": float(np.quantile(arr, alpha / 2.0)),
        "ci_hi": float(np.quantile(arr, 1.0 - alpha / 2.0)),
        "n_boot_effective": len(replicate_eers),
    }


def pooled_paired_delta(
    labels_a: Sequence[float],
    scores_a: Sequence[float],
    users_a: Sequence[object],
    labels_b: Sequence[float],
    scores_b: Sequence[float],
    users_b: Sequence[object],
    *,
    n_boot: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> dict[str, float]:
    """Same-replicate pooled EER delta ``B - A`` (positive == A better; §18.3).

    Resamples the users SHARED by both configs with a single
    :func:`user_resample_indices` matrix, recomputes each config's pooled EER on
    the same resampled users per replicate, and returns the ``B - A`` delta CI (so
    for A == M7 vs B == baseline, a positive delta means M7 has the lower EER).

    Returns:
        ``{delta_mean, ci_lo, ci_hi, n_boot_effective, n_shared_users}`` (nan when
        fewer than two shared users or no finite replicate).
    """
    ua, rows_a = _rows_by_user(users_a)
    ub, rows_b = _rows_by_user(users_b)
    shared = sorted(set(ua) & set(ub))
    nan_result = {
        "delta_mean": float("nan"),
        "ci_lo": float("nan"),
        "ci_hi": float("nan"),
        "n_boot_effective": 0,
        "n_shared_users": len(shared),
    }
    if len(shared) < 2:
        return nan_result
    la, sa = np.asarray(labels_a), np.asarray(scores_a, dtype=float)
    lb, sb = np.asarray(labels_b), np.asarray(scores_b, dtype=float)
    matrix = user_resample_indices(len(shared), n_boot, seed)
    deltas: list[float] = []
    for b in range(matrix.shape[0]):
        picked = matrix[b]
        rows_a_b = np.concatenate([rows_a[shared[j]] for j in picked])
        rows_b_b = np.concatenate([rows_b[shared[j]] for j in picked])
        eer_a, _ = compute_eer_threshold(la[rows_a_b], sa[rows_a_b])
        eer_b, _ = compute_eer_threshold(lb[rows_b_b], sb[rows_b_b])
        if np.isfinite(eer_a) and np.isfinite(eer_b):
            deltas.append(float(eer_b - eer_a))
    if not deltas:
        return nan_result
    arr = np.asarray(deltas, dtype=float)
    return {
        "delta_mean": float(arr.mean()),
        "ci_lo": float(np.quantile(arr, alpha / 2.0)),
        "ci_hi": float(np.quantile(arr, 1.0 - alpha / 2.0)),
        "n_boot_effective": len(deltas),
        "n_shared_users": len(shared),
    }


def bootstrap_ci(
    values: Sequence[float],
    n_boot: int = 1000,
    seed: int = 0,
    *,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """Return ``(mean, lo, hi)`` percentile CI over a by-user EER vector.

    SECONDARY (HMOG-parity) estimator only. Non-finite values are dropped, then
    ``n_boot`` resamples (with replacement) of the per-user EER vector give the
    MEAN distribution whose ``[alpha/2, 1-alpha/2]`` quantiles are the CI. This is
    the "mean of per-user EER" quantity exp_prompt §18.3 explicitly marks as NOT
    the main protocol; the pooled-metric CI (:func:`pooled_bootstrap_ci`) is the
    primary口径. Retained for backward compatibility and cross-checking.

    Args:
        values: Per-user metric values (e.g. per-user EER).
        n_boot: Number of bootstrap resamples.
        seed: RNG seed (deterministic).
        alpha: Two-sided significance level (0.05 -> 95 % CI).

    Returns:
        Tuple ``(mean, lo, hi)`` (all ``nan`` when no finite value is present).
    """
    arr = np.asarray([v for v in values if np.isfinite(v)], dtype=float)
    if arr.size == 0:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    means = np.empty(int(n_boot), dtype=float)
    for i in range(int(n_boot)):
        idx = rng.integers(0, arr.size, size=arr.size)
        means[i] = arr[idx].mean()
    mean = float(arr.mean())
    lo = float(np.quantile(means, alpha / 2.0))
    hi = float(np.quantile(means, 1.0 - alpha / 2.0))
    return mean, lo, hi


def holm_correction(pvals: Sequence[float]) -> np.ndarray:
    """Return Holm-Bonferroni adjusted p-values (input order preserved).

    Mirrors HMOG ``holm_correction`` (_recon_hmog §3a): step-down over the finite
    p-values sorted ascending, multiplier ``m, m-1, ..., 1``, monotone
    non-decreasing adjusted p, NaN-safe (non-finite entries stay ``nan``).

    Args:
        pvals: A family of raw p-values.

    Returns:
        A float array of adjusted p-values, same length/order as the input.
    """
    p = np.asarray(pvals, dtype=float)
    out = np.full_like(p, np.nan, dtype=float)
    finite = np.where(np.isfinite(p))[0]
    if len(finite) == 0:
        return out
    order = finite[np.argsort(p[finite])]
    m = len(order)
    adjusted = np.empty(m)
    prev = 0.0
    for rank, idx in enumerate(order):
        adj = min(1.0, (m - rank) * p[idx])
        prev = max(prev, adj)
        adjusted[rank] = prev
    for idx, adj in zip(order, adjusted):
        out[idx] = adj
    return out


def paired_permutation_p(diff: Sequence[float], n_perm: int = 10000, seed: int = 0) -> float:
    """Sign-flip permutation p-value for a paired difference (one-sided ``>``).

    Mirrors HMOG ``paired_permutation_p`` (_recon_hmog §4): under the null the
    sign of each paired diff is exchangeable; the p-value is
    ``(count + 1) / (n_perm + 1)`` where ``count`` counts sign-flipped means at
    least as large as the observed mean.

    Args:
        diff: Paired differences.
        n_perm: Number of sign-flip permutations.
        seed: RNG seed.

    Returns:
        The permutation p-value (``nan`` when there are no finite diffs).
    """
    diff_arr = np.asarray([d for d in diff if np.isfinite(d)], dtype=float)
    if diff_arr.size == 0:
        return float("nan")
    obs = float(np.mean(diff_arr))
    rng = np.random.default_rng(seed)
    count = 0
    for _ in range(int(n_perm)):
        signs = rng.choice([-1.0, 1.0], size=diff_arr.size)
        if float(np.mean(diff_arr * signs)) >= obs:
            count += 1
    return float((count + 1) / (int(n_perm) + 1))


def _cohens_d(diff: np.ndarray) -> float:
    """Paired Cohen's d = mean / std (ddof=1); 0 when std is ~0 or n<2."""
    if diff.size < 2:
        return 0.0
    std = float(diff.std(ddof=1))
    if std < 1e-12:
        return 0.0
    return float(diff.mean() / std)


def paired_delta(a: Sequence[float], b: Sequence[float], seed: int = 0) -> dict[str, float]:
    """Compare two per-user metric vectors ``a`` vs ``b`` (paired by position).

    Implements the HMOG §4 recipe: ``diff = a - b`` over matched (finite) pairs,
    ``delta_mean``, bootstrap CI on the diff, a **Wilcoxon signed-rank** p-value,
    and — when more than half the pairs are ties (or Wilcoxon is undefined) — a
    binomial **sign-test fallback** over the non-tie wins/losses. Also returns a
    sign-flip permutation p, Cohen's d and the win-rate.

    Args:
        a: Per-user metric for config A (aligned with ``b``; lower EER is better).
        b: Per-user metric for config B.
        seed: Deterministic seed for the bootstrap / permutation.

    Returns:
        Dict with ``delta_mean``, ``ci_lo``, ``ci_hi``, ``p_wilcoxon``,
        ``p_sign``, ``p_perm``, ``cohens_d``, ``win_rate``, ``n_pairs`` and a
        ``test`` label (``"wilcoxon"`` or ``"sign"``) recording which test drove
        the reported ``p_value``.
    """
    a_arr = np.asarray(a, dtype=float)
    b_arr = np.asarray(b, dtype=float)
    n = min(a_arr.size, b_arr.size)
    a_arr, b_arr = a_arr[:n], b_arr[:n]
    finite = np.isfinite(a_arr) & np.isfinite(b_arr)
    diff = a_arr[finite] - b_arr[finite]

    result: dict[str, float] = {
        "delta_mean": float("nan"),
        "ci_lo": float("nan"),
        "ci_hi": float("nan"),
        "p_wilcoxon": float("nan"),
        "p_sign": float("nan"),
        "p_perm": float("nan"),
        "cohens_d": float("nan"),
        "win_rate": float("nan"),
        "n_pairs": float(diff.size),
    }
    if diff.size == 0:
        result["test"] = "none"  # type: ignore[assignment]
        return result

    result["delta_mean"] = float(diff.mean())
    _, lo, hi = bootstrap_ci(diff, seed=seed)
    result["ci_lo"], result["ci_hi"] = lo, hi
    result["cohens_d"] = _cohens_d(diff)
    result["p_perm"] = paired_permutation_p(diff, seed=seed)

    wins = int(np.sum(diff > 0))
    losses = int(np.sum(diff < 0))
    ties = int(np.sum(diff == 0))
    decisive = wins + losses
    # win-rate here = fraction where A beats B (A lower EER -> diff<0 is a win
    # for A); we report the raw mean(diff>0) to mirror HMOG's win_rate exactly.
    result["win_rate"] = float(np.mean(diff > 0)) if diff.size else float("nan")

    # Wilcoxon signed-rank (needs at least one non-zero diff).
    p_wilcoxon = float("nan")
    if decisive > 0:
        try:
            p_wilcoxon = float(wilcoxon(diff, zero_method="wilcox", alternative="two-sided").pvalue)
        except ValueError:
            p_wilcoxon = float("nan")
    result["p_wilcoxon"] = p_wilcoxon

    # Sign-test fallback on the non-tie pairs.
    p_sign = float("nan")
    if decisive > 0:
        p_sign = float(binomtest(min(wins, losses), decisive, 0.5).pvalue)
    result["p_sign"] = p_sign

    too_many_ties = ties > 0.5 * diff.size
    if too_many_ties or not np.isfinite(p_wilcoxon):
        result["p_value"] = p_sign  # type: ignore[assignment]
        result["test"] = "sign"  # type: ignore[assignment]
    else:
        result["p_value"] = p_wilcoxon  # type: ignore[assignment]
        result["test"] = "wilcoxon"  # type: ignore[assignment]
    return result
