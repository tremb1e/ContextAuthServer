"""Run the M0..M10 suite + top-k sweep + Pareto k* and index the runs (S4).

Freezes k* on validation (top-k sweep), injects it into the k*-dependent
baselines, runs every baseline's §6 run dir, and writes ``runs_index.json``.

Run:
    python -m research.scripts.run_all_experiments \
        --config research/configs/default.yaml --data data/datasets/<name> \
        --out data/results [--smoke]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research.config import load_config
from research.experiments.runner import run_all_experiments
from research.utils.logging import get_logger

LOGGER = get_logger("research.run_all_experiments")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        A configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="run_all_experiments",
        description="Run M0..M10 + top-k sweep (frozen k* on validation); write runs_index.json.",
    )
    parser.add_argument("--config", type=Path, default=None, help="base override YAML (merged over configs/default.yaml)")
    parser.add_argument("--data", type=Path, required=True, help="dataset dir or datasets root")
    parser.add_argument("--out", type=Path, required=True, help="results root")
    parser.add_argument("--smoke", action="store_true", help="force runtime.smoke=true (tiny/fast)")
    return parser


def _resolve_data_dir(data: Path) -> Path:
    """Resolve a dataset dir (one with a feature manifest), else the first child."""
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
        Process exit code (0 on success).
    """
    args = build_arg_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.smoke:
        cfg.setdefault("runtime", {})["smoke"] = True

    data_dir = _resolve_data_dir(args.data)
    index_path = run_all_experiments(cfg, data_dir, args.out)
    index = json.loads(index_path.read_text(encoding="utf-8"))

    print("=== run_all_experiments summary ===")
    print(f"runs_index  : {index_path}")
    print(f"k*          : {index.get('kstar')}")
    print(f"data_dir    : {data_dir}")
    print("baselines   :")
    for name in [f"m{i}" for i in range(11)]:
        info = index.get("runs", {}).get(name, {})
        if "error" in info:
            print(f"  {name:>3} {info.get('label',''):<22} ERROR: {info['error']}")
        else:
            print(f"  {name:>3} {info.get('label',''):<22} EER={info.get('eer')}  k={info.get('top_k')}  router={info.get('router')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
