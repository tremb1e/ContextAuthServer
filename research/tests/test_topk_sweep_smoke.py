"""Top-k sweep smoke: k in 1..7 -> topk_sweep.csv + frozen k* — §15.1.8.

Runs the real :func:`research.experiments.runner.run_topk_sweep` on the tiny
fixture dataset (smoke config) over every k in 1..7, asserting it writes a
``topk_sweep.csv`` with one row per k (carrying EER, per-scene EER, cost proxies
and param counts) and a ``topk_kstar.json`` whose ``kstar`` is in 1..7 and
selected on validation. Also unit-tests :func:`select_kstar_pareto`'s
"smallest cost not significantly worse than best" rule.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from research.config import load_config
from research.experiments.runner import run_topk_sweep, select_kstar_pareto


def _smoke_base_cfg() -> dict:
    """Base config with smoke on, 1 epoch, full 1..7 sweep on validation."""
    cfg = load_config()
    cfg.setdefault("runtime", {})["smoke"] = True
    cfg.setdefault("train", {})["epochs"] = 1
    cfg["model"] = {**cfg.get("model", {}), "kind": "moe", "router": "learned", "expert_hidden": [16], "embedding_dim": 8}
    cfg["topk"] = {"sweep": [1, 2, 3, 4, 5, 6, 7], "select_on": "val"}
    return cfg


def test_topk_sweep_writes_csv_and_kstar(dataset_dir: Path, tmp_path: Path) -> None:
    """The sweep produces a 7-row CSV and a validation-selected k* in 1..7."""
    out = tmp_path / "results"
    csv_path = run_topk_sweep(_smoke_base_cfg(), dataset_dir, out)
    assert csv_path.exists() and csv_path.name == "topk_sweep.csv"

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    ks = sorted(int(float(r["k"])) for r in rows)
    assert ks == [1, 2, 3, 4, 5, 6, 7], f"expected all k in 1..7, got {ks}"
    # Each row carries the required sweep columns.
    required = {"k", "eer", "roc_auc", "per_scene_eer", "matched_impostor_eer", "avg_active_experts", "latency_ms", "param_count"}
    assert required <= set(rows[0].keys())

    kstar_path = out / "topk_kstar.json"
    assert kstar_path.exists()
    provenance = json.loads(kstar_path.read_text(encoding="utf-8"))
    assert provenance["kstar"] in range(1, 8)


def test_select_kstar_prefers_smallest_equivalent_k() -> None:
    """k* is the smallest k whose per-user EER is not significantly worse than best."""
    # k=1 and k=3 are practically identical; k=3 is the numeric best by a hair.
    sweep_rows = [
        {"k": 1, "eer": 0.201},
        {"k": 2, "eer": 0.260},
        {"k": 3, "eer": 0.200},
    ]
    per_user_by_k = {
        1: {"u0": 0.20, "u1": 0.21, "u2": 0.19},
        2: {"u0": 0.26, "u1": 0.27, "u2": 0.25},
        3: {"u0": 0.20, "u1": 0.21, "u2": 0.19},
    }
    kstar, provenance = select_kstar_pareto(sweep_rows, per_user_by_k, seed=0)
    assert kstar == 1, f"expected smallest equivalent k=1, got {kstar} ({provenance})"
    assert provenance["best_k"] == 3


def test_select_kstar_no_finite_defaults_smallest() -> None:
    """With no finite EER anywhere, k* falls back to the smallest swept k."""
    rows = [{"k": 2, "eer": float("nan")}, {"k": 5, "eer": float("nan")}]
    kstar, provenance = select_kstar_pareto(rows, {}, seed=0)
    assert kstar == 2
    assert provenance["best_k"] is None
