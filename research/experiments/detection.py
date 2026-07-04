"""Continuous-authentication detection policies + stream event metrics (§9.7).

The verifier emits one score per window; a *detection policy* turns that noisy
per-window reject signal (``score < threshold``) into a session-level decision:

* ``raw`` — reject the moment a single window falls below threshold.
* ``k_of_n`` — reject once ``k`` of the last ``n`` windows are below threshold
  (debounces isolated dips).
* ``ewma`` — reject once an exponentially-weighted reject rate exceeds a trigger
  (``alpha`` smoothing), so a lone outlier does not raise a false alarm.

The policy is selected on VALIDATION (min time-to-detect subject to a
false-alarm budget) and then FROZEN for the single test evaluation (§9.7's
"val-select, test-fixed" discipline). Event metrics are computed over per-(user,
session) genuine streams and per-(attacked-user, impostor-user) attack streams
built from the pooled :class:`~research.experiments.evaluator.EvalResult`.
"""

from __future__ import annotations

from typing import Any

import numpy as np

#: The frozen policy grid searched on validation (raw + two k-of-n + two EWMA).
DETECTION_GRID: list[dict[str, Any]] = [
    {"kind": "raw"},
    {"kind": "k_of_n", "k": 2, "n": 3},
    {"kind": "k_of_n", "k": 3, "n": 5},
    {"kind": "ewma", "alpha": 0.3},
    {"kind": "ewma", "alpha": 0.5},
]


def _rejects(scores: np.ndarray, threshold: float) -> np.ndarray:
    """Per-window reject indicator (reject iff ``score < threshold``)."""
    return np.asarray(scores, dtype=float) < float(threshold)


def policy_decisions(rejects: np.ndarray, policy: dict[str, Any]) -> np.ndarray:
    """Return the per-window REJECT-state boolean vector under a policy.

    Args:
        rejects: Per-window raw reject indicator (``score < threshold``).
        policy: A policy dict from :data:`DETECTION_GRID`.

    Returns:
        A boolean array (same length) that is ``True`` at each window where the
        policy is in a reject state.

    Raises:
        ValueError: If the policy kind is unknown.
    """
    rej = np.asarray(rejects, dtype=bool)
    n = rej.size
    kind = str(policy.get("kind", "raw"))
    if kind == "raw":
        return rej.copy()
    out = np.zeros(n, dtype=bool)
    if kind == "k_of_n":
        k = int(policy.get("k", 2))
        w = int(policy.get("n", 3))
        for i in range(n):
            lo = max(0, i - w + 1)
            out[i] = int(rej[lo : i + 1].sum()) >= k
        return out
    if kind == "ewma":
        alpha = float(policy.get("alpha", 0.5))
        trigger = float(policy.get("trigger", 0.5))
        e = 0.0
        for i in range(n):
            e = alpha * float(rej[i]) + (1.0 - alpha) * e
            out[i] = e > trigger
        return out
    raise ValueError(f"unknown detection policy kind: {kind!r}")


def stream_detect(scores_ordered: np.ndarray, threshold: float, policy: dict[str, Any]) -> int | None:
    """Return the index of the first policy-REJECT window, or ``None`` if never.

    Args:
        scores_ordered: Time-ordered window scores of one stream.
        threshold: Accept threshold (reject iff ``score < threshold``).
        policy: A policy dict from :data:`DETECTION_GRID`.

    Returns:
        The first rejecting window index, or ``None`` when the stream never
        triggers a reject.
    """
    decisions = policy_decisions(_rejects(scores_ordered, threshold), policy)
    idx = np.where(decisions)[0]
    return int(idx[0]) if idx.size else None


def _window_index(window_id: object) -> int:
    """Parse the trailing integer window index from a ``device:session:...:idx`` id."""
    try:
        return int(str(window_id).rsplit(":", 1)[1])
    except (ValueError, IndexError):
        return 0


def build_streams(result: Any) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Split a pooled EvalResult into time-ordered genuine + attack score streams.

    Genuine streams are grouped by (attacked user, session) — a legitimate
    continuous session. Attack streams are grouped by (attacked user, impostor
    user) and ordered by the impostor window's (session, window index).

    Args:
        result: An :class:`~research.experiments.evaluator.EvalResult`.

    Returns:
        ``(genuine_streams, attack_streams)`` — each a list of 1-D score arrays.
    """
    scores = np.asarray(result.scores, dtype=float)
    labels = np.asarray(result.labels)
    n = scores.size
    users = list(result.users) if result.users else [""] * n
    imp_users = list(result.impostor_user_ids) if getattr(result, "impostor_user_ids", None) else [""] * n
    sessions = list(result.session_ids) if getattr(result, "session_ids", None) else [""] * n
    wins = list(result.query_window_ids) if getattr(result, "query_window_ids", None) else [""] * n

    genuine: dict[tuple[str, str], list[tuple[int, float]]] = {}
    attack: dict[tuple[str, str], list[tuple[str, int, float]]] = {}
    for i in range(n):
        if labels[i] == 1:
            genuine.setdefault((users[i], sessions[i]), []).append((_window_index(wins[i]), scores[i]))
        else:
            attack.setdefault((users[i], imp_users[i]), []).append((sessions[i], _window_index(wins[i]), scores[i]))
    gen_streams = [np.asarray([s for _, s in sorted(v)], dtype=float) for v in genuine.values()]
    att_streams = [np.asarray([s for *_, s in sorted(v)], dtype=float) for v in attack.values()]
    return gen_streams, att_streams


def stream_event_metrics(
    result: Any,
    threshold: float,
    policy: dict[str, Any],
    stride_sec: float,
) -> dict[str, float]:
    """Compute streaming TTD / false-alarms-per-hour / attack-detection-rate.

    * ``time_to_detect_sec`` — restricted mean over attack streams of the first
      policy-reject time (a stream that never triggers contributes its whole
      observed span; right-censored restricted mean).
    * ``attack_detection_rate`` — fraction of attack streams that ever trigger.
    * ``false_alarms_per_hour`` — mean over genuine streams of (policy-reject
      windows / observed hours).

    Args:
        result: The pooled evaluation result.
        threshold: The FROZEN (validation-selected) decision threshold.
        policy: The frozen detection policy.
        stride_sec: Seconds between consecutive windows.

    Returns:
        Dict with the three event metrics (each ``nan`` when its stream set is
        empty or the threshold is non-finite).
    """
    nan = float("nan")
    if not np.isfinite(threshold):
        return {"time_to_detect_sec": nan, "false_alarms_per_hour": nan, "attack_detection_rate": nan}
    gen_streams, att_streams = build_streams(result)

    ttd_values: list[float] = []
    triggered = 0
    for stream in att_streams:
        if stream.size == 0:
            continue
        idx = stream_detect(stream, threshold, policy)
        if idx is None:
            ttd_values.append(float(stream.size) * stride_sec)  # censored: whole span
        else:
            ttd_values.append(float(idx) * stride_sec)
            triggered += 1
    time_to_detect = float(np.mean(ttd_values)) if ttd_values else nan
    attack_detection_rate = float(triggered / len(att_streams)) if att_streams else nan

    fa_rates: list[float] = []
    for stream in gen_streams:
        if stream.size == 0:
            continue
        decisions = policy_decisions(_rejects(stream, threshold), policy)
        hours = (stream.size * stride_sec) / 3600.0
        if hours > 0:
            fa_rates.append(float(int(decisions.sum())) / hours)
    false_alarms_per_hour = float(np.mean(fa_rates)) if fa_rates else nan

    return {
        "time_to_detect_sec": time_to_detect,
        "false_alarms_per_hour": false_alarms_per_hour,
        "attack_detection_rate": attack_detection_rate,
    }


def select_detection_policy(
    val_result: Any,
    val_threshold: float,
    stride_sec: float,
    *,
    fa_budget_per_hour: float = float("inf"),
) -> dict[str, Any]:
    """Grid-select the detection policy on VALIDATION (min TTD s.t. FA budget).

    Prefers policies whose validation false-alarm rate is within
    ``fa_budget_per_hour``; among those it minimises validation time-to-detect,
    breaking ties by grid order (raw first). The chosen policy + the (val) EER
    threshold are FROZEN for the single test evaluation.

    Args:
        val_result: The validation-split evaluation result.
        val_threshold: The validation EER threshold (the frozen decision line).
        stride_sec: Seconds between windows.
        fa_budget_per_hour: Max tolerated validation false-alarm rate.

    Returns:
        ``{kind, params, threshold, selected_on}`` (``selected_on == "val"``).
    """
    if not np.isfinite(val_threshold):
        return {"kind": "raw", "params": {}, "threshold": float("nan"), "selected_on": "val"}
    best_key: tuple[int, float, int] | None = None
    best_policy = DETECTION_GRID[0]
    for grid_idx, policy in enumerate(DETECTION_GRID):
        ev = stream_event_metrics(val_result, val_threshold, policy, stride_sec)
        fa = ev["false_alarms_per_hour"]
        ttd = ev["time_to_detect_sec"]
        feasible = 0 if (not np.isfinite(fa)) or fa <= fa_budget_per_hour else 1
        ttd_key = ttd if np.isfinite(ttd) else float("inf")
        key = (feasible, ttd_key, grid_idx)
        if best_key is None or key < best_key:
            best_key = key
            best_policy = policy
    return {
        "kind": str(best_policy["kind"]),
        "params": {k: v for k, v in best_policy.items() if k != "kind"},
        "threshold": float(val_threshold),
        "selected_on": "val",
    }
