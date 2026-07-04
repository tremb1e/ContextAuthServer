"""Run a single experiment (train + evaluate) and write the §6 run dir (S4).

Trains the configured model deterministically (early stop, best checkpoint),
evaluates with prototype/cosine verification under enroll/query-session
disjointness, and writes ``data/results/{run_id}/`` with ``metrics.json`` etc.

Run:
    python -m research.scripts.run_experiment \
        --config research/configs/default.yaml --data data/datasets/<name> \
        --out data/results [--tag m7] [--smoke]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research.config import load_config
from research.experiments.runner import run_experiment
from research.utils.logging import get_logger

LOGGER = get_logger("research.run_experiment")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        A configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="run_experiment",
        description="Train + evaluate one config; write a §6 run directory with metrics.json.",
    )
    parser.add_argument("--config", type=Path, default=None, help="override YAML (merged over configs/default.yaml)")
    parser.add_argument(
        "--data",
        type=Path,
        required=True,
        help="dataset dir (with train/val/test parquet + feature_manifest.json) or datasets root",
    )
    parser.add_argument("--out", type=Path, required=True, help="results root; run written under out/{run_id}")
    parser.add_argument("--tag", type=str, default=None, help="optional run tag prefix (e.g. m7)")
    parser.add_argument("--smoke", action="store_true", help="force runtime.smoke=true (tiny/fast)")
    return parser


def _resolve_data_dir(data: Path) -> Path:
    """Resolve a dataset dir: a dir with a manifest, else the first child that has one."""
    if (data / "feature_manifest.json").exists():
        return data
    if data.is_dir():
        for child in sorted(data.iterdir()):
            if child.is_dir() and (child / "feature_manifest.json").exists():
                return child
    return data


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector.

    Returns:
        Process exit code (0 on success, 1 if metrics.json was not written).
    """
    args = build_arg_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.smoke:
        cfg.setdefault("runtime", {})["smoke"] = True

    data_dir = _resolve_data_dir(args.data)
    run_dir = run_experiment(cfg, data_dir, args.out, tag=args.tag)
    metrics_path = run_dir / "metrics.json"
    ok = metrics_path.exists()

    print("=== run_experiment summary ===")
    print(f"run_dir     : {run_dir}")
    print(f"data_dir    : {data_dir}")
    if ok:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        pooled = metrics.get("eer_pooled_bootstrap", {})
        by_user = metrics.get("eer_by_user_bootstrap", {})
        print(f"run_id      : {run_dir.name}")
        print(f"EER         : {metrics.get('eer')}")
        print(f"ROC-AUC     : {metrics.get('roc_auc')}")
        print(f"pooled CI   : [{pooled.get('ci_lo')}, {pooled.get('ci_hi')}]  (§18.3 primary)")
        print(f"by-user CI  : [{by_user.get('ci_lo')}, {by_user.get('ci_hi')}]  (secondary)")
        print(f"FRR@FAR=1%  : {metrics.get('frr_at_far_1pct')}   detection: {metrics.get('detection_policy', {}).get('kind')}")
        print(f"pairs (g/i) : {metrics.get('n_genuine_pairs')}/{metrics.get('n_impostor_pairs')}")
        print(f"metrics.json: {metrics_path}")
    else:
        print("metrics.json NOT written")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
