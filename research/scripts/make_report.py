"""Generate the Chinese markdown report + LaTeX tables + publication plots (S4).

Renders every required figure into ``<out_dir>/plots/`` (skip-with-message on
missing inputs), writes ``latex_tables.tex``, and composes a conclusion-first
Chinese ``report.md``.

Run:
    python -m research.scripts.make_report \
        --results data/results --out data/results/report.md [--data data/datasets/<name>]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from research.reporting.report import make_report
from research.utils.logging import get_logger

LOGGER = get_logger("research.make_report")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        A configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="make_report",
        description="Render figures + LaTeX tables + a Chinese conclusion-first report.md.",
    )
    parser.add_argument("--results", type=Path, required=True, help="results root (runs_index.json / topk_sweep.csv / run dirs)")
    parser.add_argument("--out", type=Path, required=True, help="destination report.md path")
    parser.add_argument("--data", type=Path, default=None, help="optional dataset dir (for dataset summary + weak-label figure)")
    parser.add_argument(
        "--data-provenance",
        choices=["synthetic", "real"],
        default=None,
        help="explicit data-source wording (default: auto-infer from the split manifest)",
    )
    return parser


def _resolve_data_dir(data: Path | None) -> Path | None:
    """Resolve an optional dataset dir to one containing a split manifest."""
    if data is None:
        return None
    if (data / "split_manifest.json").exists():
        return data
    if data.is_dir():
        for child in sorted(data.iterdir()):
            if child.is_dir() and (child / "split_manifest.json").exists():
                return child
    return data


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument vector.

    Returns:
        Process exit code (0 if report.md + at least one plot pair were written).
    """
    args = build_arg_parser().parse_args(argv)
    data_dir = _resolve_data_dir(args.data)
    report_path = make_report(args.results, args.out, data_dir=data_dir, data_provenance=args.data_provenance)

    plots_dir = report_path.parent / "plots"
    pdfs = sorted(plots_dir.glob("*.pdf")) if plots_dir.exists() else []
    pngs = sorted(plots_dir.glob("*.png")) if plots_dir.exists() else []

    print("=== make_report summary ===")
    print(f"report.md   : {report_path}  (exists={report_path.exists()})")
    print(f"latex_tables: {report_path.parent / 'latex_tables.tex'}")
    print(f"plots dir   : {plots_dir}")
    print(f"pdf figures : {len(pdfs)}  ({', '.join(p.name for p in pdfs) if pdfs else 'none'})")
    print(f"png figures : {len(pngs)}")
    ok = report_path.exists() and len(pdfs) >= 1 and len(pngs) >= 1
    print(f"ok          : {ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
