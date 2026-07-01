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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import torch
import yaml

from research import SCENARIOS
from research.config import config_hash, load_config
from research.experiments._data import DatasetBundle
from research.experiments.bootstrap import bootstrap_ci, paired_delta
from research.experiments.evaluator import EvalResult, evaluate
from research.experiments.metrics import (
    compute_eer_auc,
    false_alarms_per_hour,
    per_scene_eer,
    per_user_eer,
    time_to_detect,
)
from research.experiments.trainer import build_model, train_model
from research.models.moe import MoEAuthenticator
from research.utils.logging import JsonlLogger, get_logger, run_context
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


def _metrics_payload(result: EvalResult, seed: int, stride_sec: float) -> dict[str, Any]:
    """Compute the full metrics.json payload from an evaluation result.

    Args:
        result: The evaluation result (pooled scores/labels/users/scenes).
        seed: The run seed (drives the deterministic bootstrap).
        stride_sec: Window stride (seconds) for the event-level metrics.

    Returns:
        The metrics dict (EER/AUC family, by-user bootstrap CI, per-user/per-scene
        maps, event-level metrics, routing diagnostics, pair counts).
    """
    eer_auc = compute_eer_auc(result.labels, result.scores)
    thr = eer_auc["threshold"]
    per_user = per_user_eer(result.labels, result.scores, result.users)
    per_scene = per_scene_eer(result.labels, result.scores, result.scenes)
    boot_mean, boot_lo, boot_hi = bootstrap_ci(list(per_user.values()), seed=seed)

    ttd = time_to_detect(result.labels, result.scores, thr, stride_sec) if np.isfinite(thr) else float("nan")
    fa_per_hour = false_alarms_per_hour(result.labels, result.scores, thr, stride_sec) if np.isfinite(thr) else float("nan")

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
        "eer_by_user_bootstrap": {"mean": boot_mean, "ci_lo": boot_lo, "ci_hi": boot_hi},
        "matched_impostor_eer": eer_auc["eer"],
        "per_user_eer": per_user,
        "per_scene_eer": per_scene,
        "time_to_detect_sec": ttd,
        "false_alarms_per_hour": fa_per_hour,
        "n_genuine_pairs": result.n_genuine,
        "n_impostor_pairs": result.n_impostor,
        "active_experts": result.active_experts,
        "router_entropy": router_entropy,
        "expert_utilization_entropy": util_entropy,
        "router_probs_mean": result.router_probs_mean,
        "expert_utilization": result.expert_utilization,
    }


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
    stride_sec = float(cfg.get("preprocess", {}).get("stride_sec", 1))
    metrics = _metrics_payload(result, seed, stride_sec)
    metrics["param_count"] = history.get("param_count")
    metrics["active_param_count"] = history.get("active_param_count", history.get("param_count"))
    metrics["best_epoch"] = history.get("best_epoch")
    metrics["tag"] = tag
    metrics["model_kind"] = str(cfg.get("model", {}).get("kind"))
    metrics["router"] = str(cfg.get("model", {}).get("router")) if cfg.get("model", {}).get("kind") == "moe" else "dense"
    metrics["feature_mode"] = str(cfg.get("features", {}).get("mode"))
    metrics["top_k"] = cfg.get("model", {}).get("top_k")

    # metrics.json
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True, default=str), encoding="utf-8")

    # metrics.csv (one flat row of the headline scalars).
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
        "eer_ci_lo": metrics["eer_by_user_bootstrap"]["ci_lo"],
        "eer_ci_hi": metrics["eer_by_user_bootstrap"]["ci_hi"],
        "matched_impostor_eer": metrics["matched_impostor_eer"],
        "time_to_detect_sec": metrics["time_to_detect_sec"],
        "false_alarms_per_hour": metrics["false_alarms_per_hour"],
        "n_genuine_pairs": metrics["n_genuine_pairs"],
        "n_impostor_pairs": metrics["n_impostor_pairs"],
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
    # expert_scene_matrix.csv (rows = scene, cols = experts C0..C6)
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

    result = _evaluate_on_split(model, bundle, data_dir, split_for_eer)
    eer_auc = compute_eer_auc(result.labels, result.scores)
    per_user = per_user_eer(result.labels, result.scores, result.users)
    per_scene = per_scene_eer(result.labels, result.scores, result.scenes)
    latency = _measure_latency_ms(model, bundle)
    return {
        "k": int(k),
        "eer": eer_auc["eer"],
        "roc_auc": eer_auc["roc_auc"],
        "per_scene_eer": ";".join(f"{s}:{per_scene.get(s, float('nan')):.4f}" for s in SCENARIOS),
        "matched_impostor_eer": eer_auc["eer"],
        "avg_active_experts": float(k),
        "latency_ms": latency,
        "param_count": int(history.get("param_count", 0)),
        "active_param_count": int(history.get("active_param_count", history.get("param_count", 0))),
        "_per_user_eer": per_user,
    }


def _evaluate_on_split(model: torch.nn.Module, bundle: DatasetBundle, data_dir: str | Path, split: str) -> EvalResult:
    """Evaluate scoring the given split's windows as the genuine queries.

    For ``split == "test"`` this is the standard :func:`evaluate`. For
    ``split == "val"`` the val windows are scored against each user's TRAIN
    prototype (train/val sessions are disjoint under leave-session-out), giving a
    frozen k*-selection EER that never touches the test split.

    Args:
        model: The trained model.
        bundle: The dataset bundle.
        data_dir: The dataset directory.
        split: ``"val"`` or ``"test"``.

    Returns:
        The evaluation result for that split.
    """
    if split == "test":
        return evaluate(model, bundle, data_dir)
    return _evaluate_val(model, bundle)


def _evaluate_val(model: torch.nn.Module, bundle: DatasetBundle) -> EvalResult:
    """Prototype/cosine EER on the VAL split (queries=val, enroll=train).

    Genuine = val window vs own train prototype (sessions disjoint). Impostor =
    every OTHER user's val window vs the attacked user's train prototype (matched
    loosely by being cross-user; scene taken from the impostor window's weak
    label). Used only for the frozen k* selection.

    Args:
        model: The trained model.
        bundle: The dataset bundle.

    Returns:
        The val-split evaluation result.
    """
    from research.experiments.evaluator import _embed_all, _prototypes, _cosine  # local import to reuse helpers

    train_emb, train_meta = _embed_all(model, bundle, "train")
    val_emb, val_meta = _embed_all(model, bundle, "val")
    if val_emb.shape[0] == 0 or train_emb.shape[0] == 0:
        return EvalResult(np.empty(0), np.empty(0), [], [], 0, 0, active_experts=float(getattr(model, "top_k", 0.0)))
    protos = _prototypes(train_emb, train_meta)

    scores: list[float] = []
    labels: list[int] = []
    users: list[str] = []
    scenes: list[str] = []
    val_users = val_meta["user_id"].astype(str).tolist()
    val_scenes = val_meta["weak_label_top1"].astype(str).tolist() if "weak_label_top1" in val_meta else [SCENARIOS[0]] * len(val_meta)
    for row in range(val_emb.shape[0]):
        user = val_users[row]
        if user in protos:  # genuine
            scores.append(_cosine(val_emb[row], protos[user]))
            labels.append(1)
            users.append(user)
            scenes.append(val_scenes[row])
        # impostor: score this val window against every OTHER user's prototype
        for other, proto in protos.items():
            if other == user:
                continue
            scores.append(_cosine(val_emb[row], proto))
            labels.append(0)
            users.append(other)
            scenes.append(val_scenes[row])
    return EvalResult(
        scores=np.asarray(scores, dtype=float),
        labels=np.asarray(labels, dtype=int),
        users=users,
        scenes=scenes,
        n_genuine=int(sum(1 for lbl in labels if lbl == 1)),
        n_impostor=int(sum(1 for lbl in labels if lbl == 0)),
        active_experts=float(getattr(model, "top_k", 0.0)),
    )


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
    for k in ks:
        row = _eval_for_topk(cfg, data_dir, int(k), select_on, scratch_dir)
        per_user_by_k[int(k)] = row.pop("_per_user_eer")
        rows.append(row)

    _write_csv(out / "topk_sweep.csv", rows)
    # Throwaway per-k training logs are not part of the run artifacts.
    import shutil

    shutil.rmtree(scratch_dir, ignore_errors=True)

    kstar, provenance = select_kstar_pareto(rows, per_user_by_k, seed=int(cfg.get("seed", 42)))
    (out / "topk_kstar.json").write_text(json.dumps({"kstar": kstar, **provenance}, indent=2, sort_keys=True, default=str), encoding="utf-8")
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
        cfg = load_config(yaml_path)
    else:
        cfg = _deep_override(base_cfg, M_OVERRIDES[name])
    # Resolve the k* sentinel wherever it appears in the model block.
    model_cfg = cfg.get("model", {})
    if model_cfg.get("top_k") == "__kstar__":
        model_cfg["top_k"] = int(kstar)
    return cfg


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

    (out / "runs_index.json").write_text(json.dumps(index, indent=2, sort_keys=True, default=str), encoding="utf-8")
    LOGGER.info("run_all_experiments done: %d baselines, k*=%d", len(index["runs"]), kstar)
    return out / "runs_index.json"
