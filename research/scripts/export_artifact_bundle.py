"""Export a reproducibility artifact bundle (S4).

Collects the key research artifacts — dataset manifests, per-run metrics/config/
run_context, the top-k sweep + k* provenance, the report + figures + LaTeX tables
— into a single ``.zip`` (or a copied directory tree). No raw batch payloads or
model weights larger than a threshold are included, and a ``MANIFEST.json`` lists
everything with sizes for auditability.

Run:
    python -m research.scripts.export_artifact_bundle \
        --out data/results/artifact_bundle.zip \
        [--results data/results] [--datasets data/datasets] [--dir]
"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

from research.utils.logging import get_logger

LOGGER = get_logger("research.export_artifact_bundle")

#: Filenames collected from each run directory (small, text, reproducible).
_RUN_FILES = (
    "config.yaml",
    "metrics.json",
    "metrics.csv",
    "per_user_metrics.csv",
    "per_scene_metrics.csv",
    "expert_utilization.csv",
    "expert_scene_matrix.csv",
    "run_context.json",
    "logs/train.jsonl",
)
#: Top-level results files collected once.
_RESULTS_FILES = ("runs_index.json", "topk_sweep.csv", "topk_kstar.json", "report.md", "latex_tables.tex")
#: Dataset manifest files collected per dataset dir.
_DATASET_FILES = ("split_manifest.json", "feature_manifest.json")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        A configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="export_artifact_bundle",
        description="Bundle manifests + metrics + report + figures into a reproducibility archive.",
    )
    parser.add_argument("--out", type=Path, required=True, help="output .zip path (or dir path with --dir)")
    parser.add_argument("--results", type=Path, default=Path("data/results"), help="results root to collect")
    parser.add_argument("--datasets", type=Path, default=Path("data/datasets"), help="datasets root (manifests)")
    parser.add_argument("--dir", action="store_true", help="write a directory tree instead of a .zip")
    parser.add_argument(
        "--include-models",
        action="store_true",
        help="also include model.pt weights (off by default to keep the bundle small)",
    )
    return parser


def _collect(results: Path, datasets: Path, include_models: bool) -> list[tuple[Path, str]]:
    """Collect ``(source_path, archive_name)`` pairs to bundle.

    Args:
        results: The results root.
        datasets: The datasets root.
        include_models: Whether to include ``model.pt`` weights.

    Returns:
        A list of (existing source path, archive-relative name) pairs.
    """
    items: list[tuple[Path, str]] = []

    for name in _RESULTS_FILES:
        path = results / name
        if path.exists():
            items.append((path, f"results/{name}"))

    if results.exists():
        for run_dir in sorted(p for p in results.iterdir() if p.is_dir()):
            for rel in _RUN_FILES:
                path = run_dir / rel
                if path.exists():
                    items.append((path, f"results/{run_dir.name}/{rel}"))
            if include_models and (run_dir / "model.pt").exists():
                items.append((run_dir / "model.pt", f"results/{run_dir.name}/model.pt"))
        plots_dir = results / "plots"
        if plots_dir.exists():
            for fig in sorted(plots_dir.glob("*")):
                if fig.suffix in (".pdf", ".png"):
                    items.append((fig, f"results/plots/{fig.name}"))

    if datasets.exists():
        for ds_dir in sorted(p for p in datasets.iterdir() if p.is_dir()):
            for rel in _DATASET_FILES:
                path = ds_dir / rel
                if path.exists():
                    items.append((path, f"datasets/{ds_dir.name}/{rel}"))
    return items


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector.

    Returns:
        Process exit code (0 on success, 1 if nothing was collected).
    """
    args = build_arg_parser().parse_args(argv)
    items = _collect(args.results, args.datasets, args.include_models)

    manifest = {
        "n_files": len(items),
        "files": [{"archive_name": arc, "bytes": src.stat().st_size} for src, arc in items],
        "include_models": bool(args.include_models),
    }

    if args.dir:
        out_root = Path(args.out)
        out_root.mkdir(parents=True, exist_ok=True)
        for src, arc in items:
            dest = out_root / arc
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
        (out_root / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        target = out_root
    else:
        out_zip = Path(args.out)
        out_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for src, arc in items:
                zf.writestr(arc, src.read_bytes())
            zf.writestr("MANIFEST.json", json.dumps(manifest, indent=2, sort_keys=True))
        target = out_zip

    print("=== export_artifact_bundle summary ===")
    print(f"output   : {target}")
    print(f"n_files  : {len(items)}")
    print(f"models   : {'included' if args.include_models else 'excluded'}")
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
