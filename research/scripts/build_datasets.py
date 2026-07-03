"""Build a leakage-checked dataset from preprocessed windows (S3 deliverable).

Applies a split protocol + matched-impostor sampling to ``windows.parquet`` and
writes ``data/datasets/{name}/{train,val,test}.parquet`` +
``impostor_pairs.parquet`` + ``split_manifest.json`` + ``feature_manifest.json``
(see :func:`research.datasets.builders.build_dataset`). The build asserts every
``leakage_check`` in the split manifest is True and raises otherwise.

Run:
    python -m research.scripts.build_datasets \
        --input data/processed --output data/datasets \
        --protocol leave_session_out [--feature-mode ui_sensor]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research.config import load_config
from research.datasets.builders import build_dataset
from research.datasets.splits import PROTOCOLS
from research.utils.logging import get_logger

LOGGER = get_logger("research.build_datasets")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        A configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="build_datasets",
        description="Split preprocessed windows into leakage-checked train/val/test datasets.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="processed dir (containing windows.parquet) or the parquet path itself",
    )
    parser.add_argument("--output", type=Path, required=True, help="datasets root output directory")
    parser.add_argument(
        "--protocol",
        type=str,
        default="leave_session_out",
        choices=sorted(PROTOCOLS),
        help="split protocol",
    )
    parser.add_argument(
        "--feature-mode",
        type=str,
        default=None,
        help="feature mode (default: from configs/default.yaml features.mode)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="optional config override YAML (defaults merged over default.yaml)",
    )
    parser.add_argument("--name", type=str, default=None, help="dataset dir name (default: {protocol}__{feature_mode})")
    parser.add_argument(
        "--n-impostor-per-genuine",
        type=int,
        default=1,
        help="impostor windows sampled per genuine test window",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector.

    Returns:
        Process exit code (0 on success).
    """
    args = build_arg_parser().parse_args(argv)
    cfg = load_config(args.config)
    feature_mode = args.feature_mode or cfg["features"]["mode"]
    seed = int(cfg.get("seed", 42))

    ds_dir = build_dataset(
        args.input,
        protocol=args.protocol,
        out_dir=args.output,
        feature_mode=feature_mode,
        seed=seed,
        n_impostor_per_genuine=int(args.n_impostor_per_genuine),
        name=args.name,
    )
    manifest = json.loads((ds_dir / "split_manifest.json").read_text(encoding="utf-8"))

    LOGGER.info("built dataset at %s", ds_dir)
    print("=== build_datasets summary ===")
    print(f"dataset_dir       : {ds_dir}")
    print(f"protocol          : {manifest['protocol']}")
    print(f"feature_mode      : {manifest['feature_mode']}")
    print(f"scene_taxonomy    : {manifest['scene_taxonomy']}")
    print(f"input_dim         : {manifest['input_dim']}")
    print(f"n_windows (t/v/te): {manifest['n_windows_train']}/{manifest['n_windows_val']}/{manifest['n_windows_test']}")
    print(f"n_genuine_pairs   : {manifest['n_genuine_pairs']}")
    print(f"n_impostor_pairs  : {manifest['n_impostor_pairs']}")
    print(f"users             : {len(manifest['users'])}")
    print(f"weak_label_dist   : {manifest['weak_label_distribution']}")
    print(f"leakage_check     : {manifest['leakage_check']}")
    all_true = all(manifest["leakage_check"].values())
    print(f"leakage_all_true  : {all_true}")
    return 0 if all_true else 1


if __name__ == "__main__":
    raise SystemExit(main())
