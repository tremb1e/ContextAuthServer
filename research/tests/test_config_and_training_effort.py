"""Formal training defaults + config propagation to M0..M10 (SRV-9).

Asserts the default config is formal-scale (early stopping reachable) and that a
caller's ``base_cfg`` overrides (``--config`` / ``--smoke``) now deep-merge THROUGH
``_resolve_experiment_cfg`` into every baseline (the pre-fix "rebase to default"
dropped them).
"""

from __future__ import annotations

from pathlib import Path

from research.config import load_config
from research.experiments._data import DatasetBundle
from research.experiments.runner import _resolve_experiment_cfg
from research.experiments.trainer import train_model


def test_default_config_is_formal_scale() -> None:
    """Default epochs are formal-scale and early stopping is reachable."""
    cfg = load_config()
    assert cfg["train"]["epochs"] >= 50
    assert cfg["train"]["early_stop_patience"] < cfg["train"]["epochs"]


def test_base_cfg_overrides_reach_baselines() -> None:
    """base_cfg train/runtime overrides propagate into a yaml-backed baseline."""
    base = load_config()
    base.setdefault("train", {})["epochs"] = 7
    base.setdefault("runtime", {})["smoke"] = True
    configs_dir = Path("research/configs/experiments")
    cfg = _resolve_experiment_cfg(base, "m7", kstar=3, configs_dir=configs_dir)
    # The override reaches M7 (pre-fix these reverted to the default epochs/smoke).
    assert cfg["train"]["epochs"] == 7
    assert cfg["runtime"]["smoke"] is True
    # ...and the baseline's own thin override still wins where it sets a key.
    assert cfg["model"]["router"] == "learned"
    assert cfg["model"]["top_k"] == 3


def test_pure_default_base_is_byte_equivalent() -> None:
    """With a pure-default base_cfg the resolved M7 matches a fresh yaml load."""
    base = load_config()
    configs_dir = Path("research/configs/experiments")
    resolved = _resolve_experiment_cfg(base, "m7", kstar=2, configs_dir=configs_dir)
    fresh = load_config(configs_dir / "m7.yaml")
    fresh.setdefault("model", {})["top_k"] = 2  # k* sentinel resolution
    assert resolved["train"] == fresh["train"]
    assert resolved["model"] == fresh["model"]


def test_early_stop_reachable_and_recorded(dataset_dir: Path, tmp_path: Path) -> None:
    """A val plateau triggers early stop before the epoch ceiling; history records it."""
    cfg = {
        "model": {"kind": "dense", "hidden_dims": [16]},
        "features": {"mode": "ui_sensor"},
        "train": {"epochs": 8, "lr": 0.0, "batch_size": 64, "early_stop_patience": 2},  # lr=0 -> val never improves
        "runtime": {"smoke": False},
        "seed": 0,
    }
    _, history = train_model(cfg, DatasetBundle(dataset_dir), tmp_path / "run")
    assert history["early_stopped"] is True
    assert history["epochs_run"] < 8  # stopped before the ceiling
    assert history["epochs_configured"] == 8
