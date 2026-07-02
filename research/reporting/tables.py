"""LaTeX table generation for the experiment report (build contract §11 S4).

Produces booktabs-style LaTeX from the run metrics / sweep CSVs. Numbers are
formatted with a fixed precision and ``nan`` is rendered as ``--``. The tables
are written to a single ``latex_tables.tex`` by :func:`write_latex_tables` and
also embedded (as source) in the Chinese ``report.md``.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

# Baseline display order + short labels (kept ASCII for LaTeX friendliness).
_MODEL_ORDER = ["m0", "m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9", "m10"]
_MODEL_LABELS = {
    "m0": "M0 sensor-dense",
    "m1": "M1 ui-dense",
    "m2": "M2 cap-dense",
    "m3": "M3 pkg-router",
    "m4": "M4 rule-top1",
    "m5": "M5 rule-topk*",
    "m6": "M6 auth-MoE",
    "m7": "M7 weak-MoE",
    "m8": "M8 weak-MoE-nopkg",
    "m9": "M9 rand-MoE",
    "m10": "M10 hash-MoE",
}
_SCENES = ["C0", "C1", "C2", "C3", "C4", "C5", "C6"]


def _fmt(value: Any, precision: int = 4) -> str:
    """Format a numeric value for LaTeX (``--`` for nan/None/blank)."""
    try:
        if value is None or value == "" or str(value).lower() == "nan":
            return "--"
        return f"{float(value):.{precision}f}"
    except (TypeError, ValueError):
        return str(value)


def _tex(value: Any) -> str:
    """Escape a small plain-text value for LaTeX table cells."""
    text = str(value)
    return (
        text.replace("\\", r"\textbackslash{}")
        .replace("&", r"\&")
        .replace("%", r"\%")
        .replace("$", r"\$")
        .replace("#", r"\#")
        .replace("_", r"\_")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read a CSV into dict rows (empty if missing)."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _run_metrics(results_dir: Path) -> dict[str, dict[str, Any]]:
    """Load ``{baseline: metrics.json}`` from ``runs_index.json``."""
    index_path = results_dir / "runs_index.json"
    if not index_path.exists():
        return {}
    index = json.loads(index_path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for name, info in index.get("runs", {}).items():
        mpath = Path(info.get("run_dir", "")) / "metrics.json"
        if mpath.exists():
            out[name] = json.loads(mpath.read_text(encoding="utf-8"))
    return out


def main_results_table(results_dir: str | Path) -> str:
    """Return a LaTeX table of EER / ROC-AUC / CI / pairs per baseline.

    Args:
        results_dir: The results root (with ``runs_index.json`` + run dirs).

    Returns:
        A LaTeX ``table`` environment string (a placeholder note if no runs).
    """
    metrics = _run_metrics(Path(results_dir))
    if not metrics:
        return "% main_results_table: no runs found\n"
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{Main results: per-baseline EER, ROC-AUC and by-user bootstrap 95\% CI.}",
        r"\label{tab:main-results}",
        r"\begin{tabular}{lrrrrr}",
        r"\toprule",
        r"Model & EER & ROC-AUC & CI$_{\text{lo}}$ & CI$_{\text{hi}}$ & \#pairs \\",
        r"\midrule",
    ]
    for name in _MODEL_ORDER:
        if name not in metrics:
            continue
        m = metrics[name]
        boot = m.get("eer_by_user_bootstrap", {})
        n_pairs = int(m.get("n_genuine_pairs", 0)) + int(m.get("n_impostor_pairs", 0))
        lines.append(
            f"{_MODEL_LABELS.get(name, name)} & {_fmt(m.get('eer'))} & {_fmt(m.get('roc_auc'))} & "
            f"{_fmt(boot.get('ci_lo'))} & {_fmt(boot.get('ci_hi'))} & {n_pairs} \\\\"
        )
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    return "\n".join(lines) + "\n"


def topk_table(results_dir: str | Path) -> str:
    """Return a LaTeX table of the top-k sweep (k, EER, AUC, cost, params).

    Args:
        results_dir: The results root (with ``topk_sweep.csv``).

    Returns:
        A LaTeX ``table`` environment string (a placeholder note if no sweep).
    """
    rows = _read_csv(Path(results_dir) / "topk_sweep.csv")
    if not rows:
        return "% topk_table: no topk_sweep.csv\n"
    kstar_path = Path(results_dir) / "topk_kstar.json"
    kstar = json.loads(kstar_path.read_text(encoding="utf-8")).get("kstar") if kstar_path.exists() else None
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{Top-$k$ sweep on validation ($k^*$ marked). EER, ROC-AUC, cost and parameters.}",
        r"\label{tab:topk-sweep}",
        r"\begin{tabular}{rrrrrr}",
        r"\toprule",
        r"$k$ & EER & ROC-AUC & active exp. & latency (ms) & active params \\",
        r"\midrule",
    ]
    for r in rows:
        k = int(float(r["k"]))
        mark = r"$^{*}$" if kstar is not None and k == int(kstar) else ""
        lines.append(
            f"{k}{mark} & {_fmt(r.get('eer'))} & {_fmt(r.get('roc_auc'))} & "
            f"{_fmt(r.get('avg_active_experts'), 1)} & {_fmt(r.get('latency_ms'), 3)} & "
            f"{_fmt(r.get('active_param_count'), 0)} \\\\"
        )
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    return "\n".join(lines) + "\n"


def per_scene_table(results_dir: str | Path, baseline: str = "m7") -> str:
    """Return a LaTeX table of per-scene EER for one baseline.

    Args:
        results_dir: The results root.
        baseline: Which baseline's per-scene EER to tabulate (falls back to any).

    Returns:
        A LaTeX ``table`` environment string (a placeholder note if no runs).
    """
    metrics = _run_metrics(Path(results_dir))
    if not metrics:
        return "% per_scene_table: no runs found\n"
    name = baseline if baseline in metrics else next(iter(metrics))
    per_scene = metrics[name].get("per_scene_eer", {})
    header = " & ".join(f"${s}$" for s in _SCENES)
    values = " & ".join(_fmt(per_scene.get(s)) for s in _SCENES)
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        rf"\caption{{Per-scene EER for {_MODEL_LABELS.get(name, name)}.}}",
        r"\label{tab:per-scene}",
        r"\begin{tabular}{l" + "r" * len(_SCENES) + "}",
        r"\toprule",
        f"Scene & {header} \\\\",
        r"\midrule",
        f"EER & {values} \\\\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
    ]
    return "\n".join(lines) + "\n"


def ablation_table(results_dir: str | Path) -> str:
    """Return a compact LaTeX table over all ablation CSV summaries.

    Args:
        results_dir: The results root.

    Returns:
        A LaTeX table string, or a placeholder comment if no ablation CSVs exist.
    """
    root = Path(results_dir)
    rows: list[dict[str, str]] = []
    for filename, kind in (
        ("feature_ablation.csv", "feature"),
        ("privacy_ablation.csv", "privacy"),
        ("mapping_ablation.csv", "mapping"),
        ("sensor_channel_ablation.csv", "sensor"),
    ):
        for row in _read_csv(root / filename):
            if row.get("error"):
                continue
            label = row.get("name") or row.get("mapping") or row.get("channel") or row.get("privacy_level") or "--"
            rows.append({"kind": kind, "label": label, "eer": row.get("eer", ""), "roc_auc": row.get("roc_auc", "")})
    if not rows:
        return "% ablation_table: no ablation CSVs found\n"
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{Ablation results: feature, privacy, mapping and sensor-channel variants.}",
        r"\label{tab:ablations}",
        r"\begin{tabular}{llrr}",
        r"\toprule",
        r"Group & Variant & EER & ROC-AUC \\",
        r"\midrule",
    ]
    for row in rows:
        lines.append(f"{_tex(row['kind'])} & {_tex(row['label'])} & {_fmt(row['eer'])} & {_fmt(row['roc_auc'])} \\\\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    return "\n".join(lines) + "\n"


def write_latex_tables(results_dir: str | Path, out_path: str | Path) -> Path:
    """Write all LaTeX tables to a single ``.tex`` file.

    Args:
        results_dir: The results root.
        out_path: Destination ``.tex`` path.

    Returns:
        The destination path.
    """
    parts = [
        "% Auto-generated LaTeX tables (research.reporting.tables)",
        "% Requires \\usepackage{booktabs} in the document preamble.",
        "",
        main_results_table(results_dir),
        topk_table(results_dir),
        per_scene_table(results_dir),
        ablation_table(results_dir),
    ]
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(parts), encoding="utf-8")
    return destination
