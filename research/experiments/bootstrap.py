"""By-user bootstrap CI, Holm correction, paired delta (_recon_hmog §2/§3a/§4).

* :func:`bootstrap_ci` — mirrors HMOG ``bootstrap.py`` §2: resamples a **per-user
  EER vector** with replacement and returns ``(mean, lo, hi)`` percentile CI. The
  unit of resampling is the user (each value is one user's EER), so this IS a
  by-user bootstrap.
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


def bootstrap_ci(
    values: Sequence[float],
    n_boot: int = 1000,
    seed: int = 0,
    *,
    alpha: float = 0.05,
) -> tuple[float, float, float]:
    """Return ``(mean, lo, hi)`` percentile CI over a by-user EER vector.

    Mirrors HMOG ``bootstrap_ci`` (_recon_hmog §2): non-finite values are
    dropped, then ``n_boot`` bootstrap resamples (with replacement, one draw of
    ``len(values)`` per iteration) give the mean distribution whose
    ``[alpha/2, 1-alpha/2]`` quantiles are the CI. Because each input value is
    one user's EER, resampling the values IS a by-user bootstrap.

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
