"""Experiment runner: single run, top-k sweep + Pareto k*, and M0..M10 suite (§6).

* :func:`run_experiment` — train (deterministic, early stop, best ckpt) + evaluate
  (prototype/cosine, enroll/query disjoint) and write the full §6 file set to
  ``data/results/{run_id}/``: ``config.yaml, metrics.json, metrics.csv,
  per_user_metrics.csv, per_scene_metrics.csv, expert_utilization.csv,
  expert_scene_matrix.csv, model.pt, logs/train.jsonl, run_context.json``.
* :func:`run_topk_sweep` — evaluate k ∈ 1..7 (k=7 == dense-all) writing
  ``topk_sweep.csv`` (k, eer, roc_auc, per_scene_eer, matched_impostor_eer,
  avg_active_experts, latency_ms, param_count).
* :func:`select_kstar_pareto` — pick the smallest-cost k on VALIDATION whose EER
  is not significantly worse than the best (frozen k*; the test EER is then read
  once at that k).
* :func:`run_all_experiments` — build each M0..M10 config (from
  ``configs/experiments/mN.yaml`` if present, else from built-in overrides),
  run it, run the top-k sweep, and write ``runs_index.json``.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
import yaml

from research import SCENARIOS
from research.config import _read_yaml, config_hash, deep_merge, load_config
from research.experiments._data import DatasetBundle
from research.experiments.bootstrap import (
    bootstrap_ci,
    holm_correction,
    paired_delta,
    pooled_bootstrap_ci,
    pooled_paired_delta,
)
from research.experiments.detection import DETECTION_GRID, select_detection_policy, stream_event_metrics
from research.experiments.evaluator import EvalResult, evaluate
from research.experiments.metrics import (
    compute_eer_auc,
    far_at_frr,
    far_frr_at_threshold,
    frr_at_far,
    per_scene_eer,
    per_user_eer,
)
from research.experiments.trainer import build_model, train_model
from research.models.moe import MoEAuthenticator
from research.preprocessing.feature_extractors import build_feature_columns
from research.utils.logging import get_logger, run_context
from research.utils.seed import set_seed

LOGGER = get_logger("research.runner")

# --- M0..M10 built-in config overrides (spec §7 / §9.4) ---------------------

#: Built-in thin overrides for each baseline (used when the matching
#: ``configs/experiments/mN.yaml`` is absent). ``__kstar__`` in ``top_k`` is a
#: sentinel resolved to the frozen k* at suite build time.
M_OVERRIDES: dict[str, dict[str, Any]] = {
    "m0": {"model": {"kind": "dense", "hidden_dims": [128, 64]}, "features": {"mode": "sensor_only"}},
    "m1": {"model": {"kind": "dense", "hidden_dims": [128, 64]}, "features": {"mode": "ui_sensor"}},
    "m2": {"model": {"kind": "dense", "hidden_dims": [256, 128, 128]}, "features": {"mode": "ui_sensor"}},
    "m3": {"model": {"kind": "moe", "router": "package_only", "top_k": 2}, "features": {"mode": "ui_sensor"}},
    "m4": {"model": {"kind": "moe", "router": "fixed_rule", "top_k": 1}, "features": {"mode": "ui_sensor"}},
    "m5": {"model": {"kind": "moe", "router": "fixed_rule", "top_k": "__kstar__"}, "features": {"mode": "ui_sensor"}},
    "m6": {
        "model": {"kind": "moe", "router": "learned", "top_k": "__kstar__"},
        "features": {"mode": "ui_sensor"},
        "loss": {"lambda_scene": 0.0},
    },
    "m7": {"model": {"kind": "moe", "router": "learned", "top_k": "__kstar__"}, "features": {"mode": "ui_sensor"}},
    "m8": {"model": {"kind": "moe", "router": "learned", "top_k": "__kstar__"}, "features": {"mode": "ui_sensor_no_package"}},
    "m9": {"model": {"kind": "moe", "router": "random", "top_k": "__kstar__"}, "features": {"mode": "ui_sensor"}},
    "m10": {"model": {"kind": "moe", "router": "hash", "top_k": "__kstar__"}, "features": {"mode": "ui_sensor"}},
}

#: Human-readable label per baseline (for the runs index / reports).
M_LABELS = {
    "m0": "sensor_only_dense",
    "m1": "ui_sensor_dense",
    "m2": "capacity_matched_dense",
    "m3": "package_only_router",
    "m4": "fixed_rule_top1",
    "m5": "fixed_rule_topk_star",
    "m6": "auth_only_moe",
    "m7": "weak_moe",
    "m8": "weak_moe_no_package",
    "m9": "random_moe",
    "m10": "hash_moe",
}


# --- small IO helpers -------------------------------------------------------


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write a list of dict rows to CSV (stable header from the first row)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _run_id(cfg: dict[str, Any], tag: str | None) -> str:
    """Compose a stable, human-readable run id from the model + a config hash."""
    model_cfg = cfg.get("model", {})
    kind = str(model_cfg.get("kind", "moe"))
    router = str(model_cfg.get("router", "-")) if kind == "moe" else "dense"
    k = model_cfg.get("top_k", "-") if kind == "moe" else "-"
    mode = str(cfg.get("features", {}).get("mode", "ui_sensor"))
    short = config_hash(cfg)[:8]
    parts = [p for p in [tag, kind, router, f"k{k}", mode, short] if p not in (None, "")]
    return "__".join(str(p) for p in parts)


def _matched_impostor_eer(result: EvalResult) -> float:
    """EER restricted to matched-impostor pairs + all genuine pairs.

    Every impostor pair is scene/user matched by construction, so the pooled EER
    already IS the matched-impostor EER. Returned separately for the sweep CSV.

    Args:
        result: The evaluation result.

    Returns:
        The pooled EER over the matched pairs (``nan`` if undefined).
    """
    return compute_eer_auc(result.labels, result.scores)["eer"]


def _measure_latency_ms(model: torch.nn.Module, bundle: DatasetBundle, repeats: int = 3) -> float:
    """Mean per-window forward latency in milliseconds (CPU, small batch).

    Args:
        model: The model to time.
        bundle: The dataset bundle (a test batch is used).
        repeats: Number of timed passes to average.

    Returns:
        Mean latency per window in ms (``nan`` if no data).
    """
    tensors = bundle.tensors("test")
    if tensors.features.shape[0] == 0:
        tensors = bundle.tensors("train")
    n = tensors.features.shape[0]
    if n == 0:
        return float("nan")
    model.eval()
    batch = tensors.features[: min(64, n)]
    weak = tensors.weak_probs[: min(64, n)]
    ids = tensors.hash_ids[: min(64, n)]
    with torch.no_grad():
        for _ in range(2):  # warmup
            _forward_any(model, batch, weak, ids)
        start = time.perf_counter()
        for _ in range(repeats):
            _forward_any(model, batch, weak, ids)
        elapsed = time.perf_counter() - start
    per_window = elapsed / (repeats * batch.shape[0])
    return float(per_window * 1000.0)


def _forward_any(model: torch.nn.Module, features: torch.Tensor, weak: torch.Tensor, ids: torch.Tensor) -> Any:
    """Forward through a Dense or MoE model (routing args only for MoE)."""
    if isinstance(model, MoEAuthenticator):
        return model(features, weak, ids)
    return model(features)


# --- single run -------------------------------------------------------------


def _metrics_payload(
    result: EvalResult,
    val_result: EvalResult | None,
    seed: int,
    stride_sec: float,
    *,
    n_boot: int = 1000,
) -> dict[str, Any]:
    """Compute the full metrics.json payload from an evaluation result.

    Args:
        result: The test evaluation result (pooled scores/labels/users/scenes).
        val_result: The validation evaluation result — supplies the FROZEN
            decision threshold + detection-policy selection (§9.7). ``None`` falls
            back to the test EER threshold with the raw policy.
        seed: The run seed (drives the deterministic bootstrap / resampling).
        stride_sec: Window stride (seconds) for the event-level metrics.
        n_boot: Bootstrap replicates for the §18.3 pooled CI.

    Returns:
        The metrics dict (EER/AUC family, PRIMARY pooled-bootstrap CI + secondary
        by-user CI, §9.7 operating points, val-selected detection policy +
        streaming event metrics, per-user/per-scene maps, routing diagnostics,
        coverage counters, pair counts).
    """
    eer_auc = compute_eer_auc(result.labels, result.scores)
    thr = eer_auc["threshold"]
    per_user = per_user_eer(result.labels, result.scores, result.users)
    per_scene = per_scene_eer(result.labels, result.scores, result.scenes)

    # SRV-3 PRIMARY: pooled-metric bootstrap CI (resample users -> rebuild pairs ->
    # recompute pooled EER). Secondary: the legacy by-user vector CI (retained).
    pooled = pooled_bootstrap_ci(result.labels, result.scores, result.users, n_boot=n_boot, seed=seed)
    boot_mean, boot_lo, boot_hi = bootstrap_ci(list(per_user.values()), seed=seed)

    # SRV-4 §9.7 operating points (conservative ROC step口径).
    frr_far_1 = frr_at_far(result.labels, result.scores, 0.01)
    frr_far_5 = frr_at_far(result.labels, result.scores, 0.05)
    far_frr_5 = far_at_frr(result.labels, result.scores, 0.05)
    far_thr, frr_thr = far_frr_at_threshold(result.labels, result.scores, thr) if np.isfinite(thr) else (float("nan"), float("nan"))
    far_resolution = (1.0 / result.n_impostor) if result.n_impostor > 0 else float("nan")

    # SRV-4 detection: select the policy on VALIDATION (val EER threshold), freeze
    # it, and evaluate the streaming event metrics ONCE on test with that threshold.
    if val_result is not None and val_result.scores.size:
        val_thr = compute_eer_auc(val_result.labels, val_result.scores)["threshold"]
        detection_policy = select_detection_policy(val_result, val_thr, stride_sec)
    else:
        detection_policy = {"kind": "raw", "params": {}, "threshold": thr, "selected_on": "test_fallback"}
    decision_thr = detection_policy["threshold"]
    event = stream_event_metrics(result, decision_thr, {"kind": detection_policy["kind"], **detection_policy["params"]}, stride_sec)

    router_entropy = float("nan")
    util_entropy = float("nan")
    if result.router_probs_mean:
        rp = np.asarray(result.router_probs_mean, dtype=float)
        rp = rp[rp > 0]
        router_entropy = float(-np.sum(rp * np.log(rp))) if rp.size else float("nan")
    if result.expert_utilization:
        util = np.asarray(result.expert_utilization, dtype=float)
        norm = util / util.sum() if util.sum() > 0 else util
        nz = norm[norm > 0]
        util_entropy = float(-np.sum(nz * np.log(nz))) if nz.size else float("nan")

    return {
        "eer": eer_auc["eer"],
        "roc_auc": eer_auc["roc_auc"],
        "pr_auc": eer_auc["pr_auc"],
        "eer_threshold": thr,
        "eer_pooled_bootstrap": pooled,
        "eer_by_user_bootstrap": {"mean": boot_mean, "ci_lo": boot_lo, "ci_hi": boot_hi},
        "matched_impostor_eer": eer_auc["eer"],
        "per_user_eer": per_user,
        "per_scene_eer": per_scene,
        "frr_at_far_1pct": frr_far_1,
        "frr_at_far_5pct": frr_far_5,
        "far_at_frr_5pct": far_frr_5,
        "far_at_threshold": far_thr,
        "frr_at_threshold": frr_thr,
        "far_resolution": far_resolution,
        "detection_policy": detection_policy,
        "attack_detection_rate": event["attack_detection_rate"],
        "time_to_detect_sec": event["time_to_detect_sec"],
        "false_alarms_per_hour": event["false_alarms_per_hour"],
        "n_genuine_pairs": result.n_genuine,
        "n_impostor_pairs": result.n_impostor,
        "n_test_users": result.n_test_users,
        "n_evaluated_users": result.n_evaluated_users,
        "dropped_users_no_enroll": result.dropped_users_no_enroll,
        "n_skipped_impostor_pairs_no_enroll": result.n_skipped_impostor_pairs_no_enroll,
        "active_experts": result.active_experts,
        "router_entropy": router_entropy,
        "expert_utilization_entropy": util_entropy,
        "router_probs_mean": result.router_probs_mean,
        "expert_utilization": result.expert_utilization,
    }


def _scores_frame(result: EvalResult) -> pd.DataFrame:
    """Per-pair scores frame for post-hoc ROC / CI recompute (SRV-4). Pseudonymous."""
    n = int(np.asarray(result.scores).size)
    return pd.DataFrame(
        {
            "score": np.asarray(result.scores, dtype=float),
            "label": np.asarray(result.labels, dtype=int),
            "attacked_user": list(result.users) if result.users else [""] * n,
            "impostor_user": list(result.impostor_user_ids) if result.impostor_user_ids else [""] * n,
            "scene": list(result.scenes) if result.scenes else [""] * n,
            "query_window_id": list(result.query_window_ids) if result.query_window_ids else [""] * n,
            "session_id": list(result.session_ids) if result.session_ids else [""] * n,
        }
    )


def _roc_points(result: EvalResult, max_points: int = 512) -> list[dict[str, float]]:
    """Down-sampled ROC vertices (fpr,tpr) for a faithful ROC redraw (SRV-4)."""
    labels = np.asarray(result.labels)
    scores = np.asarray(result.scores, dtype=float)
    if len(np.unique(labels)) < 2 or scores.size < 2:
        return []
    from sklearn.metrics import roc_curve

    fpr, tpr, _ = roc_curve(labels, scores)
    if fpr.size > max_points:
        idx = np.linspace(0, fpr.size - 1, max_points).round().astype(int)
        fpr, tpr = fpr[idx], tpr[idx]
    return [{"fpr": float(a), "tpr": float(b)} for a, b in zip(fpr, tpr)]


def run_experiment(cfg: dict[str, Any], data_dir: str | Path, out_dir: str | Path, *, tag: str | None = None) -> Path:
    """Train + evaluate one config and write the §6 run directory.

    Args:
        cfg: The merged experiment config.
        data_dir: The dataset directory (split parquets + manifests).
        out_dir: Results root; the run is written under ``out_dir/{run_id}``.
        tag: Optional run tag prefix (e.g. ``"m7"``) folded into the run id.

    Returns:
        The run directory path.
    """
    seed = int(cfg.get("seed", 42))
    set_seed(seed)
    bundle = DatasetBundle(data_dir)

    run_id = _run_id(cfg, tag)
    run_dir = Path(out_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # config.yaml (the exact merged config used).
    (run_dir / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=True, allow_unicode=True), encoding="utf-8")

    model, history = train_model(cfg, bundle, run_dir)
    torch.save(model.state_dict(), run_dir / "model.pt")

    result = evaluate(model, bundle, data_dir)
    # VAL result (frozen decision threshold + detection-policy selection, §9.7).
    val_result, _val_matching = _evaluate_on_split(model, bundle, data_dir, "val", seed=seed)
    stride_sec = float(cfg.get("preprocess", {}).get("stride_sec", 1))
    n_boot = int(cfg.get("stats", {}).get("n_boot", 1000))
    metrics = _metrics_payload(result, val_result, seed, stride_sec, n_boot=n_boot)
    metrics["param_count"] = history.get("param_count")
    metrics["active_param_count"] = history.get("active_param_count", history.get("param_count"))
    metrics["best_epoch"] = history.get("best_epoch")
    metrics["epochs_configured"] = history.get("epochs_configured")
    metrics["epochs_run"] = history.get("epochs_run")
    metrics["early_stopped"] = history.get("early_stopped")
    metrics["tag"] = tag
    metrics["model_kind"] = str(cfg.get("model", {}).get("kind"))
    metrics["router"] = str(cfg.get("model", {}).get("router")) if cfg.get("model", {}).get("kind") == "moe" else "dense"
    metrics["feature_mode"] = str(cfg.get("features", {}).get("mode"))
    metrics["top_k"] = cfg.get("model", {}).get("top_k")

    # metrics.json
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True, default=str), encoding="utf-8")

    # Per-pair scores (SRV-3/SRV-4): faithful ROC redraw, pooled-bootstrap CI, and
    # cross-run same-index paired deltas all need the raw scores persisted. These
    # are pseudonymous (user_id / window_id / scene only — no text), same
    # sensitivity as per_user_metrics.csv.
    _scores_frame(result).to_parquet(run_dir / "scores.parquet", index=False)
    if val_result is not None:
        _scores_frame(val_result).to_parquet(run_dir / "scores_val.parquet", index=False)
    # SRV-3 pair_scores.parquet (the §18.3 delta reads these columns).
    _scores_frame(result)[["attacked_user", "scene", "label", "score"]].rename(
        columns={"attacked_user": "user_id"}
    ).to_parquet(run_dir / "pair_scores.parquet", index=False)
    _write_csv(run_dir / "roc_points.csv", _roc_points(result))

    # metrics.csv (one flat row of the headline scalars). The eer_ci_* columns now
    # carry the PRIMARY pooled-bootstrap CI (aligned with the pooled EER on the
    # same row); the legacy by-user CI is kept in the eer_by_user_ci_* columns.
    pooled = metrics["eer_pooled_bootstrap"]
    by_user = metrics["eer_by_user_bootstrap"]
    flat = {
        "run_id": run_id,
        "tag": tag or "",
        "model_kind": metrics["model_kind"],
        "router": metrics["router"],
        "feature_mode": metrics["feature_mode"],
        "top_k": metrics["top_k"],
        "eer": metrics["eer"],
        "roc_auc": metrics["roc_auc"],
        "pr_auc": metrics["pr_auc"],
        "eer_ci_lo": pooled["ci_lo"],
        "eer_ci_hi": pooled["ci_hi"],
        "eer_by_user_ci_lo": by_user["ci_lo"],
        "eer_by_user_ci_hi": by_user["ci_hi"],
        "matched_impostor_eer": metrics["matched_impostor_eer"],
        "frr_at_far_1pct": metrics["frr_at_far_1pct"],
        "frr_at_far_5pct": metrics["frr_at_far_5pct"],
        "far_at_frr_5pct": metrics["far_at_frr_5pct"],
        "attack_detection_rate": metrics["attack_detection_rate"],
        "time_to_detect_sec": metrics["time_to_detect_sec"],
        "false_alarms_per_hour": metrics["false_alarms_per_hour"],
        "n_genuine_pairs": metrics["n_genuine_pairs"],
        "n_impostor_pairs": metrics["n_impostor_pairs"],
        "n_evaluated_users": metrics["n_evaluated_users"],
        "epochs_run": metrics.get("epochs_run"),
        "early_stopped": metrics.get("early_stopped"),
        "param_count": metrics["param_count"],
        "active_experts": metrics["active_experts"],
    }
    _write_csv(run_dir / "metrics.csv", [flat])

    # per_user_metrics.csv
    _write_csv(
        run_dir / "per_user_metrics.csv",
        [{"user_id": u, "eer": e} for u, e in sorted(metrics["per_user_eer"].items())] or [{"user_id": "", "eer": ""}],
    )
    # per_scene_metrics.csv (all 7 scenes; blank EER when undefined for the scene).
    _write_csv(
        run_dir / "per_scene_metrics.csv",
        [{"scene": s, "eer": metrics["per_scene_eer"].get(s, "")} for s in SCENARIOS],
    )
    # expert_utilization.csv
    if result.expert_utilization:
        _write_csv(
            run_dir / "expert_utilization.csv",
            [
                {"expert": SCENARIOS[i], "utilization": u, "router_prob_mean": result.router_probs_mean[i]}
                for i, u in enumerate(result.expert_utilization)
            ],
        )
    else:
        _write_csv(run_dir / "expert_utilization.csv", [{"expert": s, "utilization": "", "router_prob_mean": ""} for s in SCENARIOS])
    # expert_scene_matrix.csv (rows = scene, cols = experts I0..I6)
    if result.expert_scene_matrix:
        matrix_rows = []
        for s_idx, scene in enumerate(SCENARIOS):
            row = {"scene": scene}
            for e_idx, expert in enumerate(SCENARIOS):
                row[f"expert_{expert}"] = result.expert_scene_matrix[s_idx][e_idx]
            matrix_rows.append(row)
        _write_csv(run_dir / "expert_scene_matrix.csv", matrix_rows)
    else:
        _write_csv(run_dir / "expert_scene_matrix.csv", [{"scene": s, **{f"expert_{e}": "" for e in SCENARIOS}} for s in SCENARIOS])

    # run_context.json
    context = run_context(
        seed=seed,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        config_hash=config_hash(cfg),
        extra={"run_id": run_id, "tag": tag, "data_dir": str(data_dir)},
        cwd=Path(__file__).resolve().parent,
    )
    (run_dir / "run_context.json").write_text(json.dumps(context, indent=2, sort_keys=True, default=str), encoding="utf-8")

    LOGGER.info("run %s: EER=%.4f (genuine=%d impostor=%d)", run_id, metrics["eer"], result.n_genuine, result.n_impostor)
    return run_dir


# --- top-k sweep + Pareto k* ------------------------------------------------


def _eval_for_topk(cfg: dict[str, Any], data_dir: str | Path, k: int, split_for_eer: str, scratch_dir: Path) -> dict[str, Any]:
    """Train an MoE at a given top-k and return sweep-row metrics.

    Args:
        cfg: Base config (its model block is overridden to MoE/top_k=k).
        data_dir: The dataset directory.
        k: The number of active experts (1..7).
        split_for_eer: ``"val"`` or ``"test"`` — which split's genuine windows to
            score for the EER (k* is selected on ``"val"``, then frozen).
        scratch_dir: A throwaway directory for the sweep's per-k training log
            (never the read-only datasets dir).

    Returns:
        A dict row for ``topk_sweep.csv`` plus the per-user EER vector (for the
        significance test in :func:`select_kstar_pareto`).
    """
    k_cfg = _deep_override(cfg, {"model": {"kind": "moe", "top_k": int(k)}})
    seed = int(k_cfg.get("seed", 42))
    set_seed(seed)
    bundle = DatasetBundle(data_dir)
    model, history = train_model(k_cfg, bundle, scratch_dir / f"k{k}")

    result, val_matching = _evaluate_on_split(model, bundle, data_dir, split_for_eer, seed=seed)
    eer_auc = compute_eer_auc(result.labels, result.scores)
    per_user = per_user_eer(result.labels, result.scores, result.users)
    per_scene = per_scene_eer(result.labels, result.scores, result.scenes)
    latency = _measure_latency_ms(model, bundle)
    # SRV-6: when the val impostors are a loose (non-scene-matched) fallback, do
    # NOT report the EER in the matched-impostor column — it is not matched.
    matched_impostor = eer_auc["eer"] if val_matching != "loose_fallback" else float("nan")
    return {
        "k": int(k),
        "eer": eer_auc["eer"],
        "roc_auc": eer_auc["roc_auc"],
        "per_scene_eer": ";".join(f"{s}:{per_scene.get(s, float('nan')):.4f}" for s in SCENARIOS),
        "matched_impostor_eer": matched_impostor,
        "avg_active_experts": float(k),
        "latency_ms": latency,
        "param_count": int(history.get("param_count", 0)),
        "active_param_count": int(history.get("active_param_count", history.get("param_count", 0))),
        "_per_user_eer": per_user,
        "_val_impostor_matching": val_matching,
    }


def _evaluate_on_split(
    model: torch.nn.Module, bundle: DatasetBundle, data_dir: str | Path, split: str, *, seed: int = 42
) -> tuple[EvalResult, str]:
    """Evaluate scoring the given split's windows as the genuine queries.

    For ``split == "test"`` this is the standard :func:`evaluate` (matching
    ``"test"``). For ``split == "val"`` the val windows are scored against each
    user's TRAIN prototype (train/val sessions are disjoint), giving a frozen
    k*-selection / detection-threshold EER that never touches the test split.

    Args:
        model: The trained model.
        bundle: The dataset bundle.
        data_dir: The dataset directory.
        split: ``"val"`` or ``"test"``.
        seed: Deterministic seed for the val matched-impostor sampling.

    Returns:
        ``(EvalResult, matching)`` where ``matching`` is ``"test"``, ``"matched"``
        or ``"loose_fallback"`` (the val impostor provenance).
    """
    if split == "test":
        return evaluate(model, bundle, data_dir), "test"
    return _evaluate_val(model, bundle, seed=seed)


def _evaluate_val(model: torch.nn.Module, bundle: DatasetBundle, *, seed: int = 42) -> tuple[EvalResult, str]:
    """Prototype/cosine EER on the VAL split (queries=val, enroll=train), SRV-6.

    Genuine = each val window vs its own train prototype (sessions disjoint).
    Impostor = SCENE-MATCHED cross-user val windows (same construction as the test
    impostor pairs) vs the attacked user's train prototype, drawn from the VAL pool
    only. When no scene-matched cross-user pair exists (e.g. a single-user or
    single-scene val split) it falls back to the loose "every other user's window"
    method and reports ``"loose_fallback"`` so the provenance is explicit.

    Args:
        model: The trained model.
        bundle: The dataset bundle.
        seed: Deterministic seed for the matched-impostor sampling.

    Returns:
        ``(EvalResult, matching)`` — ``matching`` is ``"matched"`` or
        ``"loose_fallback"`` (``"empty"`` when a split is empty).
    """
    from research.experiments.evaluator import _embed_all, _prototypes, _cosine  # local import to reuse helpers
    from research.datasets.impostors import sample_matched_impostors
    from research.datasets.splits import SESSION_COL, USER_COL, WINDOW_COL

    active = float(getattr(model, "top_k", 0.0))
    train_emb, train_meta = _embed_all(model, bundle, "train")
    val_emb, val_meta = _embed_all(model, bundle, "val")
    if val_emb.shape[0] == 0 or train_emb.shape[0] == 0:
        return EvalResult(np.empty(0), np.empty(0), [], [], 0, 0, active_experts=active), "empty"
    protos = _prototypes(train_emb, train_meta)

    val_users = val_meta[USER_COL].astype(str).tolist()
    val_wins = val_meta[WINDOW_COL].astype(str).tolist() if WINDOW_COL in val_meta else [""] * len(val_meta)
    val_sessions = val_meta[SESSION_COL].astype(str).tolist() if SESSION_COL in val_meta else [""] * len(val_meta)
    val_scenes = val_meta["weak_label_top1"].astype(str).tolist() if "weak_label_top1" in val_meta else [SCENARIOS[0]] * len(val_meta)
    win_to_row = {w: i for i, w in enumerate(val_wins)}

    scores: list[float] = []
    labels: list[int] = []
    users: list[str] = []
    scenes: list[str] = []
    qwins: list[str] = []
    imp_users: list[str] = []
    sess: list[str] = []

    # Genuine: each val window vs its own train prototype.
    for row in range(val_emb.shape[0]):
        user = val_users[row]
        proto = protos.get(user)
        if proto is None:
            continue
        scores.append(_cosine(val_emb[row], proto))
        labels.append(1)
        users.append(user)
        scenes.append(val_scenes[row])
        qwins.append(val_wins[row])
        imp_users.append("")
        sess.append(val_sessions[row])

    # Impostor: scene-matched cross-user val windows (SRV-6), else loose fallback.
    val_frame = bundle.raw_frame("val").reset_index(drop=True)
    pairs = (
        sample_matched_impostors(val_frame, genuine_idx=list(val_frame.index), pool_idx=list(val_frame.index), seed=seed, n_per_genuine=1)
        if not val_frame.empty
        else None
    )
    matching = "matched"
    if pairs is not None and len(pairs) > 0:
        for attacked, iwin, iuser, scn in zip(pairs.genuine_user_ids, pairs.impostor_window_ids, pairs.impostor_user_ids, pairs.scene):
            proto = protos.get(str(attacked))
            irow = win_to_row.get(str(iwin))
            if proto is None or irow is None:
                continue
            scores.append(_cosine(val_emb[irow], proto))
            labels.append(0)
            users.append(str(attacked))
            scenes.append(str(scn))
            qwins.append(str(iwin))
            imp_users.append(str(iuser))
            sess.append(val_sessions[irow])
    else:
        matching = "loose_fallback"
        for row in range(val_emb.shape[0]):
            user = val_users[row]
            for other, proto in protos.items():
                if other == user:
                    continue
                scores.append(_cosine(val_emb[row], proto))
                labels.append(0)
                users.append(other)
                scenes.append(val_scenes[row])
                qwins.append(val_wins[row])
                imp_users.append(user)
                sess.append(val_sessions[row])

    result = EvalResult(
        scores=np.asarray(scores, dtype=float),
        labels=np.asarray(labels, dtype=int),
        users=users,
        scenes=scenes,
        n_genuine=int(sum(1 for lbl in labels if lbl == 1)),
        n_impostor=int(sum(1 for lbl in labels if lbl == 0)),
        query_window_ids=qwins,
        impostor_user_ids=imp_users,
        session_ids=sess,
        active_experts=active,
    )
    return result, matching


def _deep_override(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a deep copy of ``base``."""
    import copy

    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_override(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hard-link a dataset artifact if possible, falling back to copy.

    Args:
        src: Source file.
        dst: Destination file.
    """
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _write_feature_manifest(
    dataset_dir: Path,
    feature_columns: list[str],
    *,
    feature_mode: str,
    source_manifest: dict[str, Any] | None = None,
    privacy_transform: dict[str, Any] | None = None,
) -> None:
    """Write a feature manifest for a dataset view.

    Args:
        dataset_dir: View directory.
        feature_columns: Active ordered feature columns.
        feature_mode: Label for the feature mode / ablation view.
        source_manifest: Optional source manifest to preserve ancillary fields.
        privacy_transform: Optional value-transform record (SRV-10) — the
            quantization / dropped columns applied to coarsen the view.
    """
    manifest = dict(source_manifest or {})
    manifest.update(
        {
            "feature_mode": feature_mode,
            "feature_columns": feature_columns,
            "package_columns": [c for c in feature_columns if c.startswith("pkg_")],
            "input_dim": len(feature_columns),
            "leakage_free": True,
        }
    )
    if privacy_transform:
        manifest["privacy_transform"] = privacy_transform
    (dataset_dir / "feature_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )


def _dataset_view(
    source_dir: str | Path,
    view_root: str | Path,
    name: str,
    *,
    feature_mode: str | None = None,
    drop_columns: list[str] | None = None,
    drop_prefixes: list[str] | None = None,
    quantize: dict[str, float] | None = None,
    seed: int = 42,
) -> Path:
    """Create a dataset view for feature / sensor-channel / privacy ablations.

    Column-subset views (feature-family, sensor-channel, ``drop_columns``) only
    need a reduced ``feature_manifest.json``; the split parquets are hard-linked.
    A ``quantize`` mapping (column -> step) makes the view a REAL value transform
    (SRV-10 privacy coarsening): the affected split parquets are read, the listed
    columns are rounded to the given step, and rewritten (never hard-linked), so
    the privacy levels differ in data, not just in a label. (The old 8->7 mapping
    ablation was removed with the C0..C6 taxonomy — the scene space is the
    identity ``I0..I6``, so no alternate mapping exists.)

    Args:
        source_dir: Existing dataset directory.
        view_root: Root directory for generated views.
        name: View directory name.
        feature_mode: Optional feature mode whose columns become active.
        drop_columns: Exact feature columns to drop.
        drop_prefixes: Feature-column prefixes to drop.
        quantize: Optional ``{column: step}`` value-quantization (privacy views).
        seed: Deterministic impostor resampling seed (unused here; kept for
            call-site symmetry with the ablation suites).

    Returns:
        The view dataset directory.
    """
    source = Path(source_dir)
    view = Path(view_root) / name
    if view.exists():
        shutil.rmtree(view)
    view.mkdir(parents=True, exist_ok=True)

    src_manifest = json.loads((source / "feature_manifest.json").read_text(encoding="utf-8"))
    if feature_mode is None:
        columns = list(src_manifest["feature_columns"])
        mode_label = str(src_manifest.get("feature_mode", "ui_sensor"))
    else:
        columns = build_feature_columns(feature_mode)
        mode_label = feature_mode
    drop_set = set(drop_columns or [])
    prefixes = tuple(drop_prefixes or [])
    if drop_set or prefixes:
        columns = [c for c in columns if c not in drop_set and not c.startswith(prefixes)]
        if drop_set:
            mode_label = f"{mode_label}__drop_" + "_".join(sorted(drop_set))
        if prefixes:
            mode_label = f"{mode_label}__drop_" + "_".join(p.rstrip("_") for p in prefixes)
    if not columns:
        raise ValueError(f"dataset view {name!r} would have zero active feature columns")

    quant = {c: float(s) for c, s in (quantize or {}).items() if s and float(s) > 0}
    privacy_transform: dict[str, Any] | None = None
    if quant or drop_set:
        privacy_transform = {"quantize": quant, "drop_columns": sorted(drop_set)}
        mode_label = f"{mode_label}__quant_" + "_".join(sorted(quant)) if quant else mode_label

    _write_feature_manifest(view, columns, feature_mode=mode_label, source_manifest=src_manifest, privacy_transform=privacy_transform)

    src_split_manifest = source / "split_manifest.json"
    split_manifest = json.loads(src_split_manifest.read_text(encoding="utf-8")) if src_split_manifest.exists() else {}
    split_manifest.update(
        {
            "dataset_view_of": str(source),
            "dataset_name": name,
            "feature_mode": mode_label,
            "input_dim": len(columns),
        }
    )
    if privacy_transform:
        split_manifest["privacy_transform"] = privacy_transform

    for filename in ("train.parquet", "val.parquet", "test.parquet", "impostor_pairs.parquet"):
        src = source / filename
        if not src.exists():
            continue
        if quant and filename != "impostor_pairs.parquet":
            frame = pd.read_parquet(src)
            for col, step in quant.items():
                if col in frame.columns:
                    frame[col] = np.round(frame[col].astype(float) / step) * step
            frame.to_parquet(view / filename, index=False)
        else:
            _link_or_copy(src, view / filename)

    (view / "split_manifest.json").write_text(
        json.dumps(split_manifest, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    return view


def run_topk_sweep(cfg: dict[str, Any], data_dir: str | Path, out_dir: str | Path) -> Path:
    """Sweep k ∈ config ``topk.sweep`` (default 1..7) and write ``topk_sweep.csv``.

    The EER used for k* selection is computed on the VALIDATION split (frozen
    discipline); the CSV also carries the per-scene EER, cost proxies and param
    counts. The selected k* (via :func:`select_kstar_pareto`) is written into
    ``topk_kstar.json`` next to the sweep CSV.

    Args:
        cfg: The base config.
        data_dir: The dataset directory.
        out_dir: The directory to write ``topk_sweep.csv`` / ``topk_kstar.json``.

    Returns:
        The path to ``topk_sweep.csv``.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ks = list(cfg.get("topk", {}).get("sweep", [1, 2, 3, 4, 5, 6, 7]))
    select_on = str(cfg.get("topk", {}).get("select_on", "val"))

    scratch_dir = out / "_topk_scratch"
    rows: list[dict[str, Any]] = []
    per_user_by_k: dict[int, dict[str, float]] = {}
    val_impostor_matching = "matched"
    for k in ks:
        row = _eval_for_topk(cfg, data_dir, int(k), select_on, scratch_dir)
        per_user_by_k[int(k)] = row.pop("_per_user_eer")
        # Provenance is data-determined (identical across k); keep a loose flag if any k fell back.
        matching = row.pop("_val_impostor_matching", "matched")
        if matching == "loose_fallback":
            val_impostor_matching = "loose_fallback"
        rows.append(row)

    _write_csv(out / "topk_sweep.csv", rows)
    # Throwaway per-k training logs are not part of the run artifacts.
    import shutil

    shutil.rmtree(scratch_dir, ignore_errors=True)

    kstar, provenance = select_kstar_pareto(rows, per_user_by_k, seed=int(cfg.get("seed", 42)))
    (out / "topk_kstar.json").write_text(
        json.dumps({"kstar": kstar, "val_impostor_matching": val_impostor_matching, **provenance}, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    LOGGER.info("top-k sweep done: k*=%s (selected on %s)", kstar, select_on)
    return out / "topk_sweep.csv"


def select_kstar_pareto(
    sweep_rows: list[dict[str, Any]],
    per_user_by_k: dict[int, dict[str, float]],
    *,
    seed: int = 42,
) -> tuple[int, dict[str, Any]]:
    """Pick the smallest-cost k whose EER is not significantly worse than best.

    The best k is the one with the lowest (finite) EER. A candidate k is
    "not significantly worse" if the paired by-user delta between k and the best
    k is not significant (Wilcoxon/sign p >= 0.05) OR its EER is within a small
    absolute tolerance of the best. Among the qualifying ks the smallest is
    chosen (fewest active experts == lowest cost). Ties/degenerate stats fall
    back to the lowest-EER k.

    Args:
        sweep_rows: The rows from :func:`run_topk_sweep` (each has ``k`` / ``eer``).
        per_user_by_k: Per-user EER vector for each k (for the significance test).
        seed: Deterministic seed for the paired delta.

    Returns:
        Tuple ``(kstar, provenance)`` where ``provenance`` records the best k, its
        EER, the qualifying ks and the selection rule.
    """
    finite = [(int(r["k"]), float(r["eer"])) for r in sweep_rows if np.isfinite(r.get("eer", float("nan")))]
    if not finite:
        # No finite EER anywhere -> default to the smallest swept k.
        ks = sorted(int(r["k"]) for r in sweep_rows)
        return (ks[0] if ks else 2), {"rule": "no_finite_eer_default_smallest", "best_k": None, "best_eer": None}

    best_k, best_eer = min(finite, key=lambda kv: kv[1])
    tol = 0.02  # absolute EER tolerance for "practically equivalent"
    qualifying: list[int] = []
    details: dict[str, Any] = {}
    for k, eer in finite:
        a = list(per_user_by_k.get(k, {}).values())
        b = list(per_user_by_k.get(best_k, {}).values())
        # Align by shared users for a paired test.
        shared = sorted(set(per_user_by_k.get(k, {})) & set(per_user_by_k.get(best_k, {})))
        if shared:
            a_vec = [per_user_by_k[k][u] for u in shared]
            b_vec = [per_user_by_k[best_k][u] for u in shared]
            delta = paired_delta(a_vec, b_vec, seed=seed)
            p_value = float(delta.get("p_value", float("nan")))
        else:
            p_value = float("nan")
        not_worse = (eer <= best_eer + tol) or (np.isfinite(p_value) and p_value >= 0.05) or (not np.isfinite(p_value))
        details[str(k)] = {"eer": eer, "p_vs_best": p_value, "qualifies": bool(not_worse)}
        if not_worse:
            qualifying.append(k)

    kstar = min(qualifying) if qualifying else best_k
    return kstar, {
        "rule": "smallest_k_not_sig_worse_than_best",
        "best_k": best_k,
        "best_eer": best_eer,
        "tolerance": tol,
        "qualifying_ks": sorted(qualifying),
        "per_k": details,
    }


# --- full M0..M10 suite -----------------------------------------------------


def _resolve_experiment_cfg(base_cfg: dict[str, Any], name: str, kstar: int, configs_dir: Path | None) -> dict[str, Any]:
    """Resolve the merged config for baseline ``name`` (yaml override or built-in).

    Args:
        base_cfg: The base (default-merged) config.
        name: The baseline name (``m0``..``m10``).
        kstar: The frozen k* to substitute for the ``__kstar__`` sentinel.
        configs_dir: Optional ``configs/experiments`` dir to look for ``{name}.yaml``.

    Returns:
        The fully merged config for the baseline.
    """
    yaml_path = (configs_dir / f"{name}.yaml") if configs_dir else None
    if yaml_path and yaml_path.exists():
        # SRV-9: deep-merge the baseline's thin yaml override ONTO the caller's
        # base_cfg (not a fresh default-only load). When base_cfg is the pure
        # default this is byte-for-byte identical to load_config(yaml_path); when
        # the caller passed --config / --smoke overrides, those now reach M0..M10.
        cfg = deep_merge(base_cfg, _read_yaml(yaml_path))
    else:
        cfg = _deep_override(base_cfg, M_OVERRIDES[name])
    # Resolve the k* sentinel wherever it appears in the model block.
    model_cfg = cfg.get("model", {})
    if model_cfg.get("top_k") == "__kstar__":
        model_cfg["top_k"] = int(kstar)
    return cfg


def _formal_m7_cfg(base_cfg: dict[str, Any], kstar: int, override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the formal M7 config with ``k*`` resolved plus optional overrides."""
    cfg = _deep_override(base_cfg, M_OVERRIDES["m7"])
    cfg.setdefault("model", {})["top_k"] = int(kstar)
    if override:
        cfg = _deep_override(cfg, override)
        if cfg.get("model", {}).get("top_k") == "__kstar__":
            cfg["model"]["top_k"] = int(kstar)
    return cfg


def _ablation_metric_row(kind: str, name: str, run_dir: Path, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build one CSV/index row from an ablation run directory."""
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    row: dict[str, Any] = {
        "ablation_kind": kind,
        "name": name,
        "eer": metrics.get("eer"),
        "roc_auc": metrics.get("roc_auc"),
        "matched_impostor_eer": metrics.get("matched_impostor_eer"),
        "top_k": metrics.get("top_k"),
        "feature_mode": metrics.get("feature_mode"),
        "router": metrics.get("router"),
        "run_dir": str(run_dir),
    }
    if extra:
        row.update(extra)
    return row


def _run_ablation_suites(cfg: dict[str, Any], data_dir: str | Path, out_dir: str | Path, kstar: int) -> dict[str, Any]:
    """Run prompt-required ablation suites and write their CSV summaries.

    Args:
        cfg: Base config.
        data_dir: Source dataset directory (preferably full ``ui_sensor``).
        out_dir: Results root.
        kstar: Frozen validation-selected top-k.

    Returns:
        Manifest fragment indexing the written ablation CSVs and runs.
    """
    out = Path(out_dir)
    data = Path(data_dir)
    view_root = out / "_dataset_views"
    seed = int(cfg.get("seed", 42))
    manifest: dict[str, Any] = {}

    feature_specs = [
        ("no_ui", {"features": {"mode": "sensor_only"}}, {"feature_mode": "sensor_only"}),
        ("no_sensor", {"features": {"mode": "ui_only"}}, {"feature_mode": "ui_only"}),
        ("no_package", {"features": {"mode": "ui_sensor_no_package"}}, {"feature_mode": "ui_sensor_no_package"}),
        ("no_tree_diff", {"features": {"mode": "ui_sensor"}}, {"drop_columns": ["ui_treediff_nodedelta", "ui_treediff_categoryl1", "ui_treediff_boundsl1", "ui_treediff_hashchanged"]}),
        ("no_temporal_smoothness", {"loss": {"lambda_smooth": 0.0}}, {}),
        ("no_load_balance", {"loss": {"lambda_balance": 0.0}}, {}),
    ]
    feature_rows: list[dict[str, Any]] = []
    for name, override, view_kwargs in feature_specs:
        try:
            if view_kwargs:
                view = _dataset_view(data, view_root, f"feature__{name}", seed=seed, **view_kwargs)
            else:
                view = data
            run_dir = run_experiment(_formal_m7_cfg(cfg, kstar, override), view, out, tag=f"ablation_feature_{name}")
            feature_rows.append(_ablation_metric_row("feature", name, run_dir))
        except Exception as exc:  # pragma: no cover - keeps long suites auditable
            feature_rows.append({"ablation_kind": "feature", "name": name, "error": str(exc)})
            LOGGER.warning("feature ablation %s failed: %s", name, exc)
    _write_csv(out / "feature_ablation.csv", feature_rows)
    manifest["feature"] = {"csv": str(out / "feature_ablation.csv"), "runs": feature_rows}

    # SRV-10: three GENUINELY-DIFFERENT privacy levels (was a no-op — all three
    # mapped to the same column set). All share the base ui_sensor_no_package mode
    # (resource id is a leakage column, always excluded); they diverge by real
    # value coarsening / column subsetting so the RQ7 privacy-utility trade-off is
    # measurable rather than training noise.
    ui_cols = [c for c in build_feature_columns("ui_sensor_no_package") if c.startswith("ui_")]
    ui_category_keep = {"ui_webview", "ui_list", "ui_form_like_control_count", "ui_treediff_categoryl1"}
    ui_category_drop = [c for c in ui_cols if c not in ui_category_keep]
    privacy_specs = [
        # coarse bounds: quantize bounds occupancy to 4 levels + drop the bounds tree-diff.
        ("privacy_coarse_bounds", "ui_sensor_no_package", {"quantize": {"ui_bounds_occupancy": 0.25}, "drop_columns": ["ui_treediff_boundsl1"]}),
        # explicit baseline: full ui_sensor_no_package (no coarsening) — the reference level.
        ("no_resource_id", "ui_sensor_no_package", {}),
        # strictest: keep ONLY the pure widget-category-derived UI columns (+ IMU/event).
        ("coarse_widget_category_only", "ui_sensor_no_package", {"drop_columns": ui_category_drop}),
    ]
    privacy_rows: list[dict[str, Any]] = []
    for name, mode, view_kwargs in privacy_specs:
        try:
            view = _dataset_view(data, view_root, f"privacy__{name}", feature_mode=mode, seed=seed, **view_kwargs)
            run_dir = run_experiment(
                _formal_m7_cfg(cfg, kstar, {"features": {"mode": mode}}),
                view,
                out,
                tag=f"ablation_privacy_{name}",
            )
            privacy_rows.append(_ablation_metric_row("privacy", name, run_dir, {"privacy_level": name}))
        except Exception as exc:  # pragma: no cover
            privacy_rows.append({"ablation_kind": "privacy", "name": name, "privacy_level": name, "error": str(exc)})
            LOGGER.warning("privacy ablation %s failed: %s", name, exc)
    _write_csv(out / "privacy_ablation.csv", privacy_rows)
    manifest["privacy"] = {"csv": str(out / "privacy_ablation.csv"), "runs": privacy_rows}

    # SRV-10 §9.5: weak-label confidence-threshold ablation (was never implemented).
    # KL router supervision is hard-gated below each threshold (identity/L_auth is
    # untouched); runs the formal M7 on the source data (no dataset view needed).
    confidence_rows: list[dict[str, Any]] = []
    for thr in (0.0, 0.2, 0.4, 0.6):
        try:
            run_dir = run_experiment(
                _formal_m7_cfg(cfg, kstar, {"loss": {"weak_conf_threshold": float(thr)}}),
                data,
                out,
                tag=f"ablation_confidence_{thr:.1f}",
            )
            confidence_rows.append(_ablation_metric_row("confidence", f"thr_{thr:.1f}", run_dir, {"confidence_threshold": float(thr)}))
        except Exception as exc:  # pragma: no cover
            confidence_rows.append({"ablation_kind": "confidence", "name": f"thr_{thr:.1f}", "confidence_threshold": float(thr), "error": str(exc)})
            LOGGER.warning("confidence ablation thr=%.1f failed: %s", thr, exc)
    _write_csv(out / "confidence_ablation.csv", confidence_rows)
    manifest["confidence"] = {"csv": str(out / "confidence_ablation.csv"), "runs": confidence_rows}

    # NOTE: the 8->7 task-mapping ablation was removed with the C0..C6 taxonomy.
    # The scene space is now the identity I0..I6, so there is no alternate mapping
    # to compare against.

    sensor_specs = [
        ("no_accel", "acc_"),
        ("no_gyro", "gyro_"),
        ("no_magnetometer", "mag_"),
    ]
    sensor_rows: list[dict[str, Any]] = []
    for name, prefix in sensor_specs:
        try:
            view = _dataset_view(data, view_root, f"sensor__{name}", drop_prefixes=[prefix], seed=seed)
            run_dir = run_experiment(_formal_m7_cfg(cfg, kstar), view, out, tag=f"ablation_sensor_{name}")
            sensor_rows.append(_ablation_metric_row("sensor_channel", name, run_dir, {"channel": name}))
        except Exception as exc:  # pragma: no cover
            sensor_rows.append({"ablation_kind": "sensor_channel", "name": name, "channel": name, "error": str(exc)})
            LOGGER.warning("sensor ablation %s failed: %s", name, exc)
    _write_csv(out / "sensor_channel_ablation.csv", sensor_rows)
    manifest["sensor_channel"] = {"csv": str(out / "sensor_channel_ablation.csv"), "runs": sensor_rows}
    return manifest


def _paired_delta_statistics(out: Path, index: dict[str, Any], *, seed: int = 42, n_boot: int = 1000) -> Path | None:
    """Write ``paired_deltas.csv``: M7-vs-baseline deltas + Holm family correction.

    For each baseline the same-replicate POOLED EER delta (§18.3, positive ==
    M7 lower EER) is computed on the SHARED users with one
    :func:`~research.experiments.bootstrap.user_resample_indices` matrix, and the
    per-user Wilcoxon/sign paired test supplies the p-value family that
    :func:`~research.experiments.bootstrap.holm_correction` adjusts.

    Args:
        out: Results root.
        index: The populated runs index (``runs`` -> per-baseline ``run_dir``).
        seed: Deterministic resampling seed.
        n_boot: Bootstrap replicates for the pooled delta CI.

    Returns:
        The ``paired_deltas.csv`` path, or ``None`` when M7's pair scores are absent.
    """
    runs = index.get("runs", {})
    m7 = runs.get("m7", {})
    m7_dir = Path(m7["run_dir"]) if m7.get("run_dir") else None
    if m7_dir is None or not (m7_dir / "pair_scores.parquet").exists():
        return None
    m7_ps = pd.read_parquet(m7_dir / "pair_scores.parquet")
    m7_pu = json.loads((m7_dir / "metrics.json").read_text(encoding="utf-8")).get("per_user_eer", {})

    rows: list[dict[str, Any]] = []
    pvals: list[float] = []
    for name in [f"m{i}" for i in range(11)]:
        if name == "m7" or name not in runs or not runs[name].get("run_dir"):
            continue
        bdir = Path(runs[name]["run_dir"])
        ps_path = bdir / "pair_scores.parquet"
        if not ps_path.exists():
            continue
        b_ps = pd.read_parquet(ps_path)
        pooled = pooled_paired_delta(
            m7_ps["label"], m7_ps["score"], m7_ps["user_id"],
            b_ps["label"], b_ps["score"], b_ps["user_id"],
            n_boot=n_boot, seed=seed,
        )
        b_pu = json.loads((bdir / "metrics.json").read_text(encoding="utf-8")).get("per_user_eer", {})
        shared = sorted(set(m7_pu) & set(b_pu))
        # A=baseline, B=m7 so delta_mean = baseline - m7 (positive == M7 better),
        # matching the pooled convention; the two-sided p-value is sign-agnostic.
        pud = paired_delta([b_pu[u] for u in shared], [m7_pu[u] for u in shared], seed=seed)
        p = float(pud.get("p_value", float("nan")))
        pvals.append(p)
        rows.append({
            "baseline": name,
            "label": M_LABELS.get(name, name),
            "pooled_delta_mean": pooled["delta_mean"],
            "pooled_ci_lo": pooled["ci_lo"],
            "pooled_ci_hi": pooled["ci_hi"],
            "n_shared_users": pooled["n_shared_users"],
            "per_user_delta_mean": pud.get("delta_mean"),
            "per_user_p_value": p,
            "test": pud.get("test"),
            "win_rate_m7": pud.get("win_rate"),
            "cohens_d": pud.get("cohens_d"),
        })
    adjusted = holm_correction(pvals) if pvals else []
    for row, adj in zip(rows, adjusted):
        row["p_value_holm"] = float(adj)
    _write_csv(out / "paired_deltas.csv", rows)
    return out / "paired_deltas.csv"


def run_all_experiments(cfg: dict[str, Any], data_dir: str | Path, out_dir: str | Path) -> Path:
    """Run the M0..M10 suite + top-k sweep and write ``runs_index.json``.

    The top-k sweep runs first to freeze k* (on validation), which is then
    injected into the k*-dependent baselines (M5/M6/M7/M8/M9/M10). Each baseline
    writes its own §6 run dir; a manifest indexes them all.

    Args:
        cfg: The base config.
        data_dir: The dataset directory.
        out_dir: Results root.

    Returns:
        The path to ``runs_index.json``.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    configs_dir = Path(__file__).resolve().parent.parent / "configs" / "experiments"
    configs_dir = configs_dir if configs_dir.exists() else None

    # 1) top-k sweep -> frozen k*.
    run_topk_sweep(cfg, data_dir, out)
    kstar_path = out / "topk_kstar.json"
    kstar = int(json.loads(kstar_path.read_text(encoding="utf-8"))["kstar"]) if kstar_path.exists() else int(cfg.get("model", {}).get("top_k", 2))

    # 2) each baseline.
    index: dict[str, Any] = {"kstar": kstar, "runs": {}}
    for name in [f"m{i}" for i in range(11)]:
        exp_cfg = _resolve_experiment_cfg(cfg, name, kstar, configs_dir)
        try:
            run_dir = run_experiment(exp_cfg, data_dir, out, tag=name)
            metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
            index["runs"][name] = {
                "label": M_LABELS[name],
                "run_dir": str(run_dir),
                "run_id": run_dir.name,
                "eer": metrics.get("eer"),
                "roc_auc": metrics.get("roc_auc"),
                "top_k": metrics.get("top_k"),
                "router": metrics.get("router"),
                "feature_mode": metrics.get("feature_mode"),
            }
            LOGGER.info("baseline %s (%s) done: EER=%s", name, M_LABELS[name], metrics.get("eer"))
        except Exception as exc:  # pragma: no cover - keep the suite going
            LOGGER.warning("baseline %s failed: %s", name, exc)
            index["runs"][name] = {"label": M_LABELS[name], "error": str(exc)}

    # 3) statistics: M7-vs-baseline same-index pooled deltas + Holm correction (§18.3).
    stats_seed = int(cfg.get("seed", 42))
    stats_nboot = int(cfg.get("stats", {}).get("n_boot", 1000))
    deltas_path = _paired_delta_statistics(out, index, seed=stats_seed, n_boot=stats_nboot)
    if deltas_path is not None:
        index["paired_deltas"] = str(deltas_path)

    if bool(cfg.get("ablation", {}).get("enabled", True)):
        index["ablations"] = _run_ablation_suites(cfg, data_dir, out, kstar)

    (out / "runs_index.json").write_text(json.dumps(index, indent=2, sort_keys=True, default=str), encoding="utf-8")
    LOGGER.info("run_all_experiments done: %d baselines, k*=%d", len(index["runs"]), kstar)
    return out / "runs_index.json"
