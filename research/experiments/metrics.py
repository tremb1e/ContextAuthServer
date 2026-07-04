"""Verification metrics — mirrors HMOG ``sample_metrics.py`` (_recon_hmog §1b/§5).

Score convention (VERBATIM from HMOG): **larger score == genuine**, ``label 1 ==
genuine``, ``label 0 == impostor``. :func:`compute_eer_threshold` copies the
sklearn ``roc_curve`` + scipy ``brentq`` root-find where ``fnr(x) == x`` with the
``argmin|fpr-fnr|`` fallback and the ``len(unique(labels)) < 2 -> nan`` guard.

Also provided (contract §5): :func:`far_frr_at_threshold`,
:func:`compute_eer_auc`, :func:`per_user_eer`, :func:`per_scene_eer`,
:func:`time_to_detect` and :func:`false_alarms_per_hour` (event-level, minimal
but documented).
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
from scipy.interpolate import interp1d
from scipy.optimize import brentq
from sklearn.metrics import average_precision_score, roc_auc_score, roc_curve


def compute_eer_threshold(labels: Sequence[float], scores: Sequence[float]) -> tuple[float, float]:
    """Return ``(eer, threshold)`` where larger score == genuine (label 1).

    Mirrors HMOG ``sample_metrics.compute_eer_threshold`` VERBATIM: ROC-curve
    interpolation with a ``brentq`` root find on ``fnr(x) - x``, falling back to
    the closest ``argmin|fpr - fnr|`` index on any numerical failure. Returns
    ``(nan, nan)`` when there are fewer than two classes or fewer than two
    scores (the nan-guard).

    Args:
        labels: Binary labels (1 == genuine, 0 == impostor).
        scores: Match scores (larger == more genuine).

    Returns:
        Tuple ``(eer, eer_threshold)`` (both ``nan`` when undefined).
    """
    labels_arr = np.asarray(labels)
    scores_arr = np.asarray(scores, dtype=float)
    if len(np.unique(labels_arr)) < 2 or len(scores_arr) < 2:
        return float("nan"), float("nan")
    fpr, tpr, thr = roc_curve(labels_arr, scores_arr)
    fnr = 1 - tpr
    try:
        f = interp1d(fpr, fnr, kind="linear", bounds_error=False, fill_value=(fnr[0], fnr[-1]))
        eer = brentq(lambda x: f(x) - x, 0.0, 1.0)
        f_thr = interp1d(fpr, thr, kind="linear", bounds_error=False, fill_value=(thr[0], thr[-1]))
        eer_thr = float(f_thr(eer))
    except (ValueError, RuntimeError):
        i = int(np.argmin(np.abs(fpr - fnr)))
        eer = float((fpr[i] + fnr[i]) / 2)
        eer_thr = float(thr[i])
    return float(eer), float(eer_thr)


def far_frr_at_threshold(
    labels: Sequence[float], scores: Sequence[float], threshold: float
) -> tuple[float, float]:
    """Return ``(FAR, FRR)`` at a decision ``threshold`` (accept iff score >= thr).

    Mirrors HMOG ``far_frr_at_threshold``: ``FAR`` = fraction of impostor scores
    accepted, ``FRR`` = fraction of genuine scores rejected.

    Args:
        labels: Binary labels (1 == genuine).
        scores: Match scores.
        threshold: Accept threshold.

    Returns:
        Tuple ``(far, frr)``.
    """
    labels_arr = np.asarray(labels)
    scores_arr = np.asarray(scores, dtype=float)
    pred = (scores_arr >= threshold).astype(int)
    pos = labels_arr == 1
    neg = labels_arr == 0
    n_pos = int(pos.sum())
    n_neg = int(neg.sum())
    far = float((pred[neg] == 1).sum()) / max(1, n_neg)
    frr = float((pred[pos] == 0).sum()) / max(1, n_pos)
    return far, frr


def frr_at_far(labels: Sequence[float], scores: Sequence[float], target_far: float) -> float:
    """Return the FRR at the strictest operating point whose FAR <= ``target_far``.

    §9.7 main metric. Uses the ROC curve (``fpr == FAR``, ``1 - tpr == FRR``) and
    takes the CONSERVATIVE step operating point: among the ROC vertices with
    ``FAR <= target_far`` it returns the smallest FRR (the largest admissible
    FAR). Returns ``nan`` when the target FAR is unreachable (e.g. below the
    impostor-count resolution) or the input is single-class.

    Args:
        labels: Binary labels (1 == genuine).
        scores: Match scores (larger == genuine).
        target_far: The FAR budget (e.g. 0.01 for FRR@FAR=1%).

    Returns:
        The FRR at that operating point (``nan`` if undefined/unreachable).
    """
    labels_arr = np.asarray(labels)
    scores_arr = np.asarray(scores, dtype=float)
    if len(np.unique(labels_arr)) < 2 or len(scores_arr) < 2:
        return float("nan")
    fpr, tpr, _ = roc_curve(labels_arr, scores_arr)
    fnr = 1.0 - tpr
    admissible = fpr <= float(target_far) + 1e-12
    if not admissible.any():
        return float("nan")
    return float(fnr[admissible].min())


def far_at_frr(labels: Sequence[float], scores: Sequence[float], target_frr: float) -> float:
    """Return the FAR at the strictest operating point whose FRR <= ``target_frr``.

    §9.7 main metric (the FRR-constrained dual of :func:`frr_at_far`): among the
    ROC vertices with ``FRR <= target_frr`` it returns the smallest FAR.

    Args:
        labels: Binary labels (1 == genuine).
        scores: Match scores (larger == genuine).
        target_frr: The FRR budget (e.g. 0.05 for FAR@FRR=5%).

    Returns:
        The FAR at that operating point (``nan`` if undefined/unreachable).
    """
    labels_arr = np.asarray(labels)
    scores_arr = np.asarray(scores, dtype=float)
    if len(np.unique(labels_arr)) < 2 or len(scores_arr) < 2:
        return float("nan")
    fpr, tpr, _ = roc_curve(labels_arr, scores_arr)
    fnr = 1.0 - tpr
    admissible = fnr <= float(target_frr) + 1e-12
    if not admissible.any():
        return float("nan")
    return float(fpr[admissible].min())


def compute_eer_auc(labels: Sequence[float], scores: Sequence[float]) -> dict[str, float]:
    """Return ``{eer, roc_auc, pr_auc, threshold}`` for a score/label vector.

    Args:
        labels: Binary labels (1 == genuine).
        scores: Match scores (larger == genuine).

    Returns:
        Dict with the EER, ROC-AUC, PR-AUC and the EER threshold. Any metric that
        is undefined (single-class input) is ``nan``.
    """
    labels_arr = np.asarray(labels)
    scores_arr = np.asarray(scores, dtype=float)
    eer, thr = compute_eer_threshold(labels_arr, scores_arr)
    if len(np.unique(labels_arr)) < 2:
        return {"eer": eer, "roc_auc": float("nan"), "pr_auc": float("nan"), "threshold": thr}
    try:
        roc_auc = float(roc_auc_score(labels_arr, scores_arr))
    except ValueError:  # pragma: no cover - guarded by the unique check above
        roc_auc = float("nan")
    try:
        pr_auc = float(average_precision_score(labels_arr, scores_arr))
    except ValueError:  # pragma: no cover
        pr_auc = float("nan")
    return {"eer": eer, "roc_auc": roc_auc, "pr_auc": pr_auc, "threshold": thr}


def per_user_eer(
    labels: Sequence[float],
    scores: Sequence[float],
    user_ids: Sequence[object],
) -> dict[str, float]:
    """Return a per-genuine-user EER map (one EER value per attacked user).

    A user contributes an EER only when its sliced labels contain both a genuine
    and an impostor score (mirrors HMOG's ``per_user`` loop guard). The returned
    dict feeds :func:`research.experiments.bootstrap.bootstrap_ci` as a by-user
    vector.

    Args:
        labels: Binary labels (1 == genuine) aligned with ``scores``.
        scores: Match scores.
        user_ids: The attacked (genuine) user id for each pair, aligned.

    Returns:
        Mapping ``user_id -> eer`` (finite entries only).
    """
    labels_arr = np.asarray(labels)
    scores_arr = np.asarray(scores, dtype=float)
    users_arr = np.asarray([str(u) for u in user_ids], dtype=object)
    out: dict[str, float] = {}
    for user in np.unique(users_arr):
        mask = users_arr == user
        y, s = labels_arr[mask], scores_arr[mask]
        if len(np.unique(y)) < 2:
            continue
        eer, _ = compute_eer_threshold(y, s)
        if np.isfinite(eer):
            out[str(user)] = float(eer)
    return out


def per_scene_eer(
    labels: Sequence[float],
    scores: Sequence[float],
    scenes: Sequence[object],
) -> dict[str, float]:
    """Return a per-scene (weak-label scenario) EER map.

    Args:
        labels: Binary labels (1 == genuine) aligned with ``scores``.
        scores: Match scores.
        scenes: The matched scene id (I0..I6) for each pair, aligned.

    Returns:
        Mapping ``scene -> eer`` (finite entries only).
    """
    labels_arr = np.asarray(labels)
    scores_arr = np.asarray(scores, dtype=float)
    scenes_arr = np.asarray([str(c) for c in scenes], dtype=object)
    out: dict[str, float] = {}
    for scene in np.unique(scenes_arr):
        mask = scenes_arr == scene
        y, s = labels_arr[mask], scores_arr[mask]
        if len(np.unique(y)) < 2:
            continue
        eer, _ = compute_eer_threshold(y, s)
        if np.isfinite(eer):
            out[str(scene)] = float(eer)
    return out


def time_to_detect(
    labels: Sequence[float],
    scores: Sequence[float],
    threshold: float,
    window_stride_sec: float = 1.0,
) -> float:
    """Restricted-mean time (seconds) to reject a stream of impostor windows.

    Minimal event-level metric (documented in ``research/README.md``): treats the
    impostor scores as an ordered stream and returns the index of the first
    rejected window (``score < threshold``) times the window stride. If no
    impostor window is ever rejected the whole observed span is returned (a
    right-censored, restricted mean over one stream).

    Args:
        labels: Binary labels (1 == genuine).
        scores: Match scores aligned with ``labels``.
        threshold: Accept threshold (reject iff ``score < threshold``).
        window_stride_sec: Seconds between consecutive windows.

    Returns:
        Time-to-detect in seconds (``nan`` when there are no impostor windows).
    """
    labels_arr = np.asarray(labels)
    scores_arr = np.asarray(scores, dtype=float)
    imp = scores_arr[labels_arr == 0]
    if imp.size == 0:
        return float("nan")
    rejected = np.where(imp < threshold)[0]
    if rejected.size == 0:
        return float(imp.size * window_stride_sec)
    return float(int(rejected[0]) * window_stride_sec)


def false_alarms_per_hour(
    labels: Sequence[float],
    scores: Sequence[float],
    threshold: float,
    window_stride_sec: float = 1.0,
) -> float:
    """Genuine-window false-reject rate expressed as false alarms per hour.

    Minimal event-level metric (documented): a *false alarm* is a genuine window
    that is rejected (``score < threshold``). The count is normalised by the
    genuine observation span (``n_genuine * window_stride_sec``) and scaled to an
    hourly rate.

    Args:
        labels: Binary labels (1 == genuine).
        scores: Match scores aligned with ``labels``.
        threshold: Accept threshold.
        window_stride_sec: Seconds between consecutive windows.

    Returns:
        False alarms per hour (``nan`` when there are no genuine windows).
    """
    labels_arr = np.asarray(labels)
    scores_arr = np.asarray(scores, dtype=float)
    gen = scores_arr[labels_arr == 1]
    if gen.size == 0:
        return float("nan")
    false_alarms = int(np.sum(gen < threshold))
    observed_hours = (gen.size * window_stride_sec) / 3600.0
    if observed_hours <= 0:
        return float("nan")
    return float(false_alarms / observed_hours)
