"""Training smoke: train M0 & M7 for 1 epoch -> metrics.json exists — §15.1.7.

Runs the real :func:`research.experiments.runner.run_experiment` on the tiny
fixture dataset for the dense M0 baseline and the formal MoE M7 (with k*
resolved to a concrete small k), asserting each writes the full §6 run
directory: ``metrics.json`` (with the EER family + by-user bootstrap CI +
routing diagnostics), ``metrics.csv``, ``model.pt`` and ``logs/train.jsonl``.
Everything is smoke-scale (1 epoch, tiny nets) so it finishes fast.
"""

from __future__ import annotations

import json
from pathlib import Path

from research import N_SCENARIOS
from research.config import load_config
from research.experiments.runner import _resolve_experiment_cfg
from research.experiments.runner import run_experiment


def _smoke_cfg(name: str, kstar: int = 2) -> dict:
    """Resolve a baseline config, force smoke + 1 epoch (from its yaml override)."""
    base = load_config()
    configs_dir = Path("research/configs/experiments")
    cfg = _resolve_experiment_cfg(base, name, kstar=kstar, configs_dir=configs_dir)
    cfg.setdefault("runtime", {})["smoke"] = True
    cfg.setdefault("train", {})["epochs"] = 1
    return cfg


def _assert_run_dir(run_dir: Path) -> dict:
    """Assert the §6 run directory has the required artifacts; return metrics.json."""
    assert run_dir.is_dir()
    metrics_path = run_dir / "metrics.json"
    assert metrics_path.exists(), "metrics.json must be written"
    assert (run_dir / "metrics.csv").exists()
    assert (run_dir / "config.yaml").exists()
    assert (run_dir / "model.pt").exists()
    assert (run_dir / "logs" / "train.jsonl").exists()
    assert (run_dir / "run_context.json").exists()
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    # EER family + by-user bootstrap block present.
    assert "eer" in metrics and "roc_auc" in metrics
    assert "eer_by_user_bootstrap" in metrics
    assert {"mean", "ci_lo", "ci_hi"} <= set(metrics["eer_by_user_bootstrap"])
    assert "param_count" in metrics
    return metrics


def test_train_m0_dense_smoke(dataset_dir: Path, tmp_path: Path) -> None:
    """M0 (sensor_only dense) trains 1 epoch and writes a run dir."""
    cfg = _smoke_cfg("m0")
    run_dir = run_experiment(cfg, dataset_dir, tmp_path / "results", tag="m0")
    metrics = _assert_run_dir(run_dir)
    assert metrics["model_kind"] == "dense"
    assert metrics["feature_mode"] == "sensor_only"


def test_train_m7_weak_moe_smoke(dataset_dir: Path, tmp_path: Path) -> None:
    """M7 (learned weak-supervised MoE at k*) trains 1 epoch and writes a run dir."""
    cfg = _smoke_cfg("m7", kstar=2)
    run_dir = run_experiment(cfg, dataset_dir, tmp_path / "results", tag="m7")
    metrics = _assert_run_dir(run_dir)
    assert metrics["model_kind"] == "moe"
    assert metrics["router"] == "learned"
    assert metrics["top_k"] == 2
    # MoE runs carry routing diagnostics (one per scenario/expert).
    assert len(metrics.get("expert_utilization", [])) == N_SCENARIOS
    assert len(metrics.get("router_probs_mean", [])) == N_SCENARIOS
