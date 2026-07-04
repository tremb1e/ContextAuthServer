"""Publication figures — matplotlib + numpy ONLY (HMOG §7 rcParams VERBATIM).

The rcParams block below is copied verbatim from HMOG
``plot_publication_figures.py`` (_recon_hmog §7): Times New Roman serif, STIX
mathtext, large fonts, **no titles**, 300 dpi, tight bbox. Every figure is saved
as BOTH ``.pdf`` and ``.png`` via :func:`save`. No seaborn, no pandas plotting.
All in-figure text is English with LaTeX mathtext for symbols ($k$, $\\mathrm{EER}$,
$\\mathrm{FAR}$, $p(\\text{scene}\\mid x)$, $\\lambda_{\\text{scene}}$) — **NO CJK
characters** appear in any figure (the Chinese narrative lives in ``report.md``).

Each plotting function takes a results directory + a figures directory and
**skips with a printed message** when its input CSV/JSON is missing or empty, so
``make_report`` degrades gracefully on partial result sets.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Callable

from research import SCENARIOS

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

# --- HMOG §7 rcParams block (VERBATIM) --------------------------------------
plt.rcParams.update(
    {
        "font.family": "serif",
        "font.serif": ["Times New Roman", "Nimbus Roman", "Liberation Serif", "DejaVu Serif"],
        "font.size": 16,
        "axes.labelsize": 18,
        "xtick.labelsize": 14,
        "ytick.labelsize": 14,
        "legend.fontsize": 13,
        "mathtext.fontset": "stix",
        "axes.linewidth": 1.2,
        "figure.dpi": 300,
        "savefig.dpi": 300,
        "savefig.bbox": "tight",
    }
)

#: Stable display order + labels for the baselines (mirrors HMOG's DISPLAY dict).
MODEL_ORDER = ["m0", "m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9", "m10"]
MODEL_LABELS = {
    "m0": "M0 sensor-dense",
    "m1": "M1 ui-dense",
    "m2": "M2 cap-dense",
    "m3": "M3 pkg-router",
    "m4": "M4 rule-top1",
    "m5": "M5 rule-top$k^*$",
    "m6": "M6 auth-MoE",
    "m7": "M7 weak-MoE",
    "m8": "M8 weak-MoE-nopkg",
    "m9": "M9 rand-MoE",
    "m10": "M10 hash-MoE",
}
_SCENES = list(SCENARIOS)


def save(fig: "plt.Figure", name: str, fig_dir: Path) -> list[Path]:
    """Save a figure as BOTH pdf and png at 300 dpi (HMOG §7 ``save``).

    Args:
        fig: The matplotlib figure.
        name: The base filename (no extension).
        fig_dir: The output directory (created if needed).

    Returns:
        The list of written paths (``[.pdf, .png]``).
    """
    fig_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for ext in ("pdf", "png"):
        path = fig_dir / f"{name}.{ext}"
        fig.savefig(path)
        written.append(path)
    plt.close(fig)
    return written


# --- CSV / results readers (stdlib csv + json, no pandas) -------------------


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read a CSV into a list of dict rows (empty list if missing/empty)."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows


def _to_float(value: Any) -> float:
    """Parse a value to float, mapping blanks / ``nan`` strings to ``nan``."""
    try:
        if value is None or value == "" or str(value).lower() == "nan":
            return float("nan")
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _runs_index(results_dir: Path) -> dict[str, Any]:
    """Load ``runs_index.json`` if present (else empty dict)."""
    path = results_dir / "runs_index.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _run_metrics(results_dir: Path) -> dict[str, dict[str, Any]]:
    """Return ``{baseline: metrics.json}`` for the M0..M10 runs in the index.

    Args:
        results_dir: The results root (with ``runs_index.json`` + run dirs).

    Returns:
        Mapping baseline name -> parsed ``metrics.json`` (only runs that exist).
    """
    index = _runs_index(results_dir)
    out: dict[str, dict[str, Any]] = {}
    for name, info in index.get("runs", {}).items():
        run_dir = info.get("run_dir")
        if not run_dir:
            continue
        mpath = Path(run_dir) / "metrics.json"
        if mpath.exists():
            out[name] = json.loads(mpath.read_text(encoding="utf-8"))
    return out


def _skip(name: str, reason: str) -> None:
    """Print a uniform skip-with-message line for a missing figure input."""
    print(f"[plots] skip {name}: {reason}")


# --- required figures -------------------------------------------------------


def eer_bar(results_dir: Path, fig_dir: Path) -> list[Path]:
    """Bar chart of EER per baseline (with by-user bootstrap CI whiskers).

    Args:
        results_dir: The results root.
        fig_dir: The figures output dir.

    Returns:
        The written figure paths (empty if skipped).
    """
    metrics = _run_metrics(results_dir)
    if not metrics:
        _skip("eer_bar", "no baseline metrics found (run run_all_experiments)")
        return []
    names = [n for n in MODEL_ORDER if n in metrics]
    eers = [_to_float(metrics[n].get("eer")) for n in names]
    los, his = [], []
    for n in names:
        # PRIMARY: §18.3 pooled-bootstrap CI; fall back to the legacy by-user CI.
        pooled = metrics[n].get("eer_pooled_bootstrap") or {}
        lo, hi = _to_float(pooled.get("ci_lo")), _to_float(pooled.get("ci_hi"))
        if not (np.isfinite(lo) and np.isfinite(hi)):
            by_user = metrics[n].get("eer_by_user_bootstrap", {})
            lo, hi = _to_float(by_user.get("ci_lo")), _to_float(by_user.get("ci_hi"))
        los.append(lo)
        his.append(hi)
    lower = [max(0.0, e - lo) if np.isfinite(lo) and np.isfinite(e) else 0.0 for e, lo in zip(eers, los)]
    upper = [max(0.0, hi - e) if np.isfinite(hi) and np.isfinite(e) else 0.0 for e, hi in zip(eers, his)]

    fig, ax = plt.subplots(figsize=(9, 4.5))
    x = np.arange(len(names))
    ax.bar(x, eers, yerr=[lower, upper], capsize=3, color="#4C78A8", alpha=0.85)
    ax.set_xticks(x)
    ax.set_xticklabels([MODEL_LABELS.get(n, n) for n in names], rotation=40, ha="right")
    ax.set_ylabel(r"$\mathrm{EER}$")
    ax.grid(axis="y", alpha=0.25)
    return save(fig, "eer_bar", fig_dir)


def roc_curves(results_dir: Path, fig_dir: Path) -> list[Path]:
    """True per-baseline ROC curves from each run's ``roc_points.csv`` (SRV-4).

    Reads the persisted ROC vertices (fpr, tpr) each run writes and draws one
    faithful ROC curve per baseline plus the chance diagonal. When no run has a
    ``roc_points.csv`` (older result trees) it degrades to the ROC-AUC bar proxy.

    Args:
        results_dir: The results root.
        fig_dir: The figures output dir.

    Returns:
        The written figure paths (empty if skipped).
    """
    index = _runs_index(results_dir)
    runs = index.get("runs", {})
    curves: list[tuple[str, list[float], list[float]]] = []
    for name in MODEL_ORDER:
        info = runs.get(name)
        if not info or not info.get("run_dir"):
            continue
        points = _read_csv(Path(info["run_dir"]) / "roc_points.csv")
        finite = [
            (a, b)
            for a, b in ((_to_float(r.get("fpr")), _to_float(r.get("tpr"))) for r in points)
            if np.isfinite(a) and np.isfinite(b)
        ]
        if len(finite) >= 2:
            curves.append((name, [a for a, _ in finite], [b for _, b in finite]))
    if curves:
        fig, ax = plt.subplots(figsize=(6, 5))
        for name, fpr, tpr in curves:
            ax.plot(fpr, tpr, linewidth=1.4, label=MODEL_LABELS.get(name, name))
        ax.plot([0, 1], [0, 1], color="grey", linestyle="--", linewidth=1.0)
        ax.set_xlim(0.0, 1.0)
        ax.set_ylim(0.0, 1.0)
        ax.set_xlabel(r"$\mathrm{FAR}$")
        ax.set_ylabel(r"$\mathrm{TPR}$")
        ax.grid(alpha=0.25)
        ax.legend(fontsize=9, loc="lower right")
        return save(fig, "roc_curves", fig_dir)

    # Fallback: ROC-AUC bar proxy when no run persisted per-pair ROC points.
    metrics = _run_metrics(results_dir)
    if not metrics:
        _skip("roc_curves", "no roc_points.csv and no baseline metrics found")
        return []
    names = [n for n in MODEL_ORDER if n in metrics]
    aucs = [_to_float(metrics[n].get("roc_auc")) for n in names]
    fig, ax = plt.subplots(figsize=(9, 4.5))
    x = np.arange(len(names))
    ax.bar(x, aucs, color="#F58518", alpha=0.85)
    ax.axhline(0.5, color="grey", linestyle="--", linewidth=1.0)
    ax.set_xticks(x)
    ax.set_xticklabels([MODEL_LABELS.get(n, n) for n in names], rotation=40, ha="right")
    ax.set_ylabel(r"$\mathrm{ROC\text{-}AUC}$")
    ax.set_ylim(0.0, 1.0)
    ax.grid(axis="y", alpha=0.25)
    return save(fig, "roc_curves", fig_dir)


def topk_ablation(results_dir: Path, fig_dir: Path) -> list[Path]:
    """EER vs $k$ line plot from ``topk_sweep.csv``.

    Args:
        results_dir: The results root (containing ``topk_sweep.csv``).
        fig_dir: The figures output dir.

    Returns:
        The written figure paths (empty if skipped).
    """
    rows = _read_csv(results_dir / "topk_sweep.csv")
    if not rows:
        _skip("topk_ablation", "topk_sweep.csv missing/empty")
        return []
    ks = [int(_to_float(r["k"])) for r in rows]
    eers = [_to_float(r["eer"]) for r in rows]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(ks, eers, marker="o", color="#4C78A8")
    ax.set_xlabel(r"$k$ (active experts)")
    ax.set_ylabel(r"$\mathrm{EER}$")
    ax.set_xticks(ks)
    ax.grid(alpha=0.25)
    return save(fig, "topk_ablation", fig_dir)


def topk_eer_latency_pareto(results_dir: Path, fig_dir: Path) -> list[Path]:
    """EER-vs-cost Pareto scatter from ``topk_sweep.csv`` (HMOG §8).

    x = active-expert count (cost proxy), y = EER; the Pareto frontier (points
    with strictly-decreasing EER as cost falls) is highlighted, and k* (from
    ``topk_kstar.json``) is annotated.

    Args:
        results_dir: The results root.
        fig_dir: The figures output dir.

    Returns:
        The written figure paths (empty if skipped).
    """
    rows = _read_csv(results_dir / "topk_sweep.csv")
    if not rows:
        _skip("topk_eer_latency_pareto", "topk_sweep.csv missing/empty")
        return []
    ks = np.array([int(_to_float(r["k"])) for r in rows])
    cost = np.array([_to_float(r.get("avg_active_experts", r["k"])) for r in rows])
    eers = np.array([_to_float(r["eer"]) for r in rows])

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.scatter(cost, eers, color="#F58518", alpha=0.8, s=60, zorder=3)
    for k, c, e in zip(ks, cost, eers):
        if np.isfinite(e):
            ax.annotate(f"$k$={k}", (c, e), textcoords="offset points", xytext=(4, 4), fontsize=11)
    # Pareto frontier: sort by cost ascending, keep strictly-decreasing EER.
    order = np.argsort(cost)
    frontier_x, frontier_y = [], []
    best = np.inf
    for idx in order:
        if np.isfinite(eers[idx]) and eers[idx] < best - 1e-9:
            best = eers[idx]
            frontier_x.append(cost[idx])
            frontier_y.append(eers[idx])
    if frontier_x:
        ax.plot(frontier_x, frontier_y, color="#4C78A8", linewidth=1.5, zorder=2, label="Pareto frontier")
        ax.legend()
    kstar_path = results_dir / "topk_kstar.json"
    if kstar_path.exists():
        kstar = json.loads(kstar_path.read_text(encoding="utf-8")).get("kstar")
        if kstar is not None:
            sel = ks == int(kstar)
            if sel.any():
                ax.scatter(cost[sel], eers[sel], edgecolor="black", facecolor="none", s=180, linewidth=1.6, zorder=4)
    ax.set_xlabel(r"avg active experts (cost proxy)")
    ax.set_ylabel(r"$\mathrm{EER}$")
    ax.grid(alpha=0.25)
    return save(fig, "topk_eer_latency_pareto", fig_dir)


def per_scene_eer(results_dir: Path, fig_dir: Path, baseline: str = "m7") -> list[Path]:
    """Per-scene EER bar chart for one baseline (default M7).

    Args:
        results_dir: The results root.
        fig_dir: The figures output dir.
        baseline: Which baseline's per-scene EER to plot (falls back to any).

    Returns:
        The written figure paths (empty if skipped).
    """
    metrics = _run_metrics(results_dir)
    if not metrics:
        _skip("per_scene_eer", "no baseline metrics found")
        return []
    name = baseline if baseline in metrics else next(iter(metrics))
    per_scene = metrics[name].get("per_scene_eer", {})
    eers = [_to_float(per_scene.get(s)) for s in _SCENES]
    if not any(np.isfinite(e) for e in eers):
        _skip("per_scene_eer", f"no finite per-scene EER for {name}")
        return []
    fig, ax = plt.subplots(figsize=(7, 4))
    x = np.arange(len(_SCENES))
    ax.bar(x, [0.0 if not np.isfinite(e) else e for e in eers], color="#54A24B", alpha=0.85)
    ax.set_xticks(x)
    ax.set_xticklabels([f"${s}$" for s in _SCENES])
    ax.set_xlabel(r"scene")
    ax.set_ylabel(r"$\mathrm{EER}$")
    ax.grid(axis="y", alpha=0.25)
    return save(fig, "per_scene_eer", fig_dir)


def expert_utilization(results_dir: Path, fig_dir: Path, baseline: str = "m7") -> list[Path]:
    """Expert-utilisation bar chart from a baseline's ``expert_utilization.csv``.

    Args:
        results_dir: The results root.
        fig_dir: The figures output dir.
        baseline: Which baseline's utilisation to plot (default M7).

    Returns:
        The written figure paths (empty if skipped).
    """
    index = _runs_index(results_dir)
    info = index.get("runs", {}).get(baseline) or next(iter(index.get("runs", {}).values()), None)
    if not info or "run_dir" not in info:
        _skip("expert_utilization", "no MoE run dir found")
        return []
    rows = _read_csv(Path(info["run_dir"]) / "expert_utilization.csv")
    utils = [(_r.get("expert", ""), _to_float(_r.get("utilization"))) for _r in rows]
    utils = [(e, u) for e, u in utils if e]
    if not utils or not any(np.isfinite(u) for _, u in utils):
        _skip("expert_utilization", "expert_utilization.csv empty (dense model?)")
        return []
    experts = [e for e, _ in utils]
    values = [0.0 if not np.isfinite(u) else u for _, u in utils]
    fig, ax = plt.subplots(figsize=(7, 4))
    x = np.arange(len(experts))
    ax.bar(x, values, color="#B279A2", alpha=0.85)
    ax.set_xticks(x)
    ax.set_xticklabels([f"${e}$" for e in experts])
    ax.set_xlabel(r"expert")
    ax.set_ylabel(r"utilization (fraction top-$k$)")
    ax.grid(axis="y", alpha=0.25)
    return save(fig, "expert_utilization", fig_dir)


def expert_scene_heatmap(results_dir: Path, fig_dir: Path, baseline: str = "m7") -> list[Path]:
    """Heatmap of mean gate weight per (scene, expert) from a baseline's matrix.

    Args:
        results_dir: The results root.
        fig_dir: The figures output dir.
        baseline: Which baseline's ``expert_scene_matrix.csv`` to plot.

    Returns:
        The written figure paths (empty if skipped).
    """
    index = _runs_index(results_dir)
    info = index.get("runs", {}).get(baseline) or next(iter(index.get("runs", {}).values()), None)
    if not info or "run_dir" not in info:
        _skip("expert_scene_heatmap", "no MoE run dir found")
        return []
    rows = _read_csv(Path(info["run_dir"]) / "expert_scene_matrix.csv")
    if not rows:
        _skip("expert_scene_heatmap", "expert_scene_matrix.csv missing/empty")
        return []
    matrix = np.zeros((len(_SCENES), len(_SCENES)), dtype=float)
    scene_index = {s: i for i, s in enumerate(_SCENES)}
    any_value = False
    for row in rows:
        s = scene_index.get(row.get("scene", ""))
        if s is None:
            continue
        for j, expert in enumerate(_SCENES):
            v = _to_float(row.get(f"expert_{expert}"))
            matrix[s, j] = 0.0 if not np.isfinite(v) else v
            any_value = any_value or np.isfinite(v)
    if not any_value:
        _skip("expert_scene_heatmap", "matrix all-empty (dense model?)")
        return []
    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(matrix, cmap="viridis", aspect="auto")
    ax.set_xticks(np.arange(len(_SCENES)))
    ax.set_xticklabels([f"${s}$" for s in _SCENES])
    ax.set_yticks(np.arange(len(_SCENES)))
    ax.set_yticklabels([f"${s}$" for s in _SCENES])
    ax.set_xlabel(r"expert")
    ax.set_ylabel(r"weak-label scene")
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label=r"mean gate weight")
    return save(fig, "expert_scene_heatmap", fig_dir)


def weak_label_distribution(results_dir: Path, fig_dir: Path, data_dir: Path | None = None) -> list[Path]:
    """Stacked/again bar of the weak-label top1 distribution across splits.

    Reads ``split_manifest.json`` (from ``data_dir`` if given, else searched
    under the results root's sibling ``data/datasets``). Skips if none found.

    Args:
        results_dir: The results root.
        fig_dir: The figures output dir.
        data_dir: Optional explicit dataset dir with ``split_manifest.json``.

    Returns:
        The written figure paths (empty if skipped).
    """
    manifest_path: Path | None = None
    if data_dir is not None:
        cand = Path(data_dir) / "split_manifest.json"
        manifest_path = cand if cand.exists() else None
    if manifest_path is None:
        for cand in sorted(Path("data/datasets").glob("*/split_manifest.json")) if Path("data/datasets").exists() else []:
            manifest_path = cand
            break
    if manifest_path is None or not manifest_path.exists():
        _skip("weak_label_distribution", "no split_manifest.json found")
        return []
    dist = json.loads(manifest_path.read_text(encoding="utf-8")).get("weak_label_distribution", {})
    if not dist:
        _skip("weak_label_distribution", "weak_label_distribution empty")
        return []
    splits = [s for s in ("train", "val", "test") if s in dist]
    fig, ax = plt.subplots(figsize=(8, 4))
    x = np.arange(len(_SCENES))
    width = 0.8 / max(1, len(splits))
    colors = {"train": "#4C78A8", "val": "#F58518", "test": "#54A24B"}
    for i, split in enumerate(splits):
        counts = [int(dist[split].get(s, 0)) for s in _SCENES]
        ax.bar(x + i * width, counts, width=width, label=split, color=colors.get(split, None), alpha=0.85)
    ax.set_xticks(x + width * (len(splits) - 1) / 2)
    ax.set_xticklabels([f"${s}$" for s in _SCENES])
    ax.set_xlabel(r"weak-label scene")
    ax.set_ylabel(r"windows")
    ax.legend()
    ax.grid(axis="y", alpha=0.25)
    return save(fig, "weak_label_distribution", fig_dir)


def _grouped_eer_bar(
    results_dir: Path,
    fig_dir: Path,
    names: list[str],
    figure_name: str,
) -> list[Path]:
    """Shared helper: EER bar over a chosen subset of baselines."""
    metrics = _run_metrics(results_dir)
    present = [n for n in names if n in metrics]
    if not present:
        _skip(figure_name, f"none of {names} present in results")
        return []
    eers = [_to_float(metrics[n].get("eer")) for n in present]
    fig, ax = plt.subplots(figsize=(max(4, 1.4 * len(present)), 4))
    x = np.arange(len(present))
    ax.bar(x, [0.0 if not np.isfinite(e) else e for e in eers], color="#4C78A8", alpha=0.85)
    ax.set_xticks(x)
    ax.set_xticklabels([MODEL_LABELS.get(n, n) for n in present], rotation=30, ha="right")
    ax.set_ylabel(r"$\mathrm{EER}$")
    ax.grid(axis="y", alpha=0.25)
    return save(fig, figure_name, fig_dir)


def package_ablation(results_dir: Path, fig_dir: Path) -> list[Path]:
    """Package-dependence ablation (M3 vs M7 vs M8), EER bars (RQ6).

    Args:
        results_dir: The results root.
        fig_dir: The figures output dir.

    Returns:
        The written figure paths (empty if skipped).
    """
    return _grouped_eer_bar(results_dir, fig_dir, ["m3", "m7", "m8"], "package_ablation")


def privacy_ablation(results_dir: Path, fig_dir: Path) -> list[Path]:
    """Privacy/redaction-level ablation EER bars.

    Prefers the explicit ``privacy_ablation.csv`` written by
    ``run_all_experiments``. Falls back to the older baseline proxy
    (M1 full vs M8 no-package vs M0 sensor-only) for partial result trees.

    Args:
        results_dir: The results root.
        fig_dir: The figures output dir.

    Returns:
        The written figure paths (empty if skipped).
    """
    rows = _read_csv(results_dir / "privacy_ablation.csv")
    if rows:
        labels = [str(r.get("privacy_level") or r.get("name") or i) for i, r in enumerate(rows)]
        eers = [_to_float(r.get("eer")) for r in rows]
        fig, ax = plt.subplots(figsize=(max(5, 1.6 * len(labels)), 4))
        x = np.arange(len(labels))
        ax.bar(x, [0.0 if not np.isfinite(e) else e for e in eers], color="#59A14F", alpha=0.85)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=25, ha="right")
        ax.set_ylabel(r"$\mathrm{EER}$")
        ax.grid(axis="y", alpha=0.25)
        return save(fig, "privacy_ablation", fig_dir)
    return _grouped_eer_bar(results_dir, fig_dir, ["m0", "m1", "m8"], "privacy_ablation")


def feature_ablation(results_dir: Path, fig_dir: Path) -> list[Path]:
    """Feature/loss-family ablation EER bars from ``feature_ablation.csv``.

    Args:
        results_dir: The results root.
        fig_dir: The figures output dir.

    Returns:
        The written figure paths (empty if skipped).
    """
    rows = _read_csv(results_dir / "feature_ablation.csv")
    if not rows:
        _skip("feature_ablation", "feature_ablation.csv missing")
        return []
    labels = [str(r.get("name", i)) for i, r in enumerate(rows)]
    eers = [_to_float(r.get("eer")) for r in rows]
    fig, ax = plt.subplots(figsize=(max(6, 1.25 * len(labels)), 4.2))
    x = np.arange(len(labels))
    ax.bar(x, [0.0 if not np.isfinite(e) else e for e in eers], color="#9C755F", alpha=0.85)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=35, ha="right")
    ax.set_ylabel(r"$\mathrm{EER}$")
    ax.grid(axis="y", alpha=0.25)
    return save(fig, "feature_ablation", fig_dir)


def sensor_channel_ablation(results_dir: Path, fig_dir: Path) -> list[Path]:
    """Sensor-channel ablation (accel/gyro/mag) EER bars.

    Reads ``sensor_channel_ablation.csv`` (columns ``channel,eer``) if the S5
    ablation runner produced one; otherwise skips with a message.

    Args:
        results_dir: The results root.
        fig_dir: The figures output dir.

    Returns:
        The written figure paths (empty if skipped).
    """
    rows = _read_csv(results_dir / "sensor_channel_ablation.csv")
    if not rows:
        _skip("sensor_channel_ablation", "sensor_channel_ablation.csv missing (optional ablation)")
        return []
    labels = [str(r.get("channel", i)) for i, r in enumerate(rows)]
    eers = [_to_float(r.get("eer")) for r in rows]
    fig, ax = plt.subplots(figsize=(6, 4))
    x = np.arange(len(labels))
    ax.bar(x, [0.0 if not np.isfinite(e) else e for e in eers], color="#72B7B2", alpha=0.85)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=20, ha="right")
    ax.set_ylabel(r"$\mathrm{EER}$")
    ax.grid(axis="y", alpha=0.25)
    return save(fig, "sensor_channel_ablation", fig_dir)


#: All required plotting functions (contract §11 / spec §12), by figure name.
PLOT_FUNCTIONS: dict[str, Callable[..., list[Path]]] = {
    "eer_bar": eer_bar,
    "roc_curves": roc_curves,
    "topk_ablation": topk_ablation,
    "topk_eer_latency_pareto": topk_eer_latency_pareto,
    "per_scene_eer": per_scene_eer,
    "expert_utilization": expert_utilization,
    "expert_scene_heatmap": expert_scene_heatmap,
    "weak_label_distribution": weak_label_distribution,
    "package_ablation": package_ablation,
    "privacy_ablation": privacy_ablation,
    "feature_ablation": feature_ablation,
    "sensor_channel_ablation": sensor_channel_ablation,
}

#: Per-figure Chinese caption (§12.3): 含义 / 坐标轴 / 结论要点. Written to
#: ``plots/{name}.md`` beside each figure (the figures themselves stay CJK-free).
PLOT_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "eer_bar": {
        "含义": "各基线（M0–M10）的等错误率 EER 柱状对比，误差须为 §18.3 池化自助法 95% 置信区间（缺失时回退 by-user 口径）。",
        "坐标轴": "横轴=模型（M0–M10）；纵轴=EER（越低越好）。",
        "结论要点": "对比 M7（弱监督 MoE）与各基线的高低与 CI 是否重叠，判断改进是否稳健。",
    },
    "roc_curves": {
        "含义": "各基线的真实 ROC 曲线（由每个 run 落盘的 roc_points.csv 重绘）；无分数文件时回退为 ROC-AUC 柱状代理。",
        "坐标轴": "横轴=FAR（误accept率）；纵轴=TPR（真accept率）；虚线为随机对角线。",
        "结论要点": "曲线越靠左上越好；对比各模型在低 FAR 区的可用性。",
    },
    "topk_ablation": {
        "含义": "top-k 稀疏门控中激活专家数 k 对验证集 EER 的影响曲线。",
        "坐标轴": "横轴=激活专家数 k（1..7）；纵轴=EER。",
        "结论要点": "定位 EER 随 k 的拐点，支撑 k* 的选择（详见 Pareto 图）。",
    },
    "topk_eer_latency_pareto": {
        "含义": "EER 与推理代价（平均激活专家数）的 Pareto 散点；前沿高亮，k* 以黑圈标注。",
        "坐标轴": "横轴=平均激活专家数（代价代理）；纵轴=EER。",
        "结论要点": "k* 应落在“再增代价收益递减”的前沿拐点，验证集冻结选出。",
    },
    "per_scene_eer": {
        "含义": "M7 在 7 个交互场景（I0–I6）上的分场景 EER。",
        "坐标轴": "横轴=场景 I0–I6；纵轴=EER。",
        "结论要点": "识别难/易场景，解释专家专化与弱标签质量的场景差异。",
    },
    "expert_utilization": {
        "含义": "M7 每个专家在测试窗口 top-k 中被激活的比例（利用率）。",
        "坐标轴": "横轴=专家 I0–I6；纵轴=利用率（top-k 命中比例）。",
        "结论要点": "利用率是否均衡；单专家垄断说明路由退化（对照 M9 随机路由）。",
    },
    "expert_scene_heatmap": {
        "含义": "行=弱标签场景、列=专家的平均门控权重热力图，反映“场景→专家”的专化对齐。",
        "坐标轴": "横轴=专家 I0–I6；纵轴=弱标签场景 I0–I6；色深=平均门控权重。",
        "结论要点": "对角占优表示专家按场景专化；离散漂移提示路由弱监督失效。",
    },
    "weak_label_distribution": {
        "含义": "train/val/test 三划分上弱标签 top1 场景的窗口计数分布。",
        "坐标轴": "横轴=场景 I0–I6；纵轴=窗口数；分组=划分。",
        "结论要点": "检查场景覆盖与划分间的分布漂移（尤其 leave_app_out 下）。",
    },
    "package_ablation": {
        "含义": "包名依赖消融：M3（仅包名路由）/M7（全特征）/M8（去包名）EER 对比（RQ6）。",
        "坐标轴": "横轴=模型；纵轴=EER。",
        "结论要点": "M8≈M7 说明模型未过度依赖前台包名。",
    },
    "privacy_ablation": {
        "含义": "隐私/粗化级别的 EER 对比（SRV-10 已改为三档真实互异：粗化 bounds、基线、仅类别）。",
        "坐标轴": "横轴=隐私级别；纵轴=EER。",
        "结论要点": "量化隐私-效用权衡（RQ7）；级别越严 EER 通常上升。",
    },
    "feature_ablation": {
        "含义": "特征/损失族消融（去 UI、去传感器、去包名、去树 diff、去时间平滑、去负载均衡）的 EER。",
        "坐标轴": "横轴=消融项；纵轴=EER。",
        "结论要点": "各组件对判别力的边际贡献；升高越多越重要。",
    },
    "sensor_channel_ablation": {
        "含义": "传感器通道消融（去 accel/gyro/mag）的 EER。",
        "坐标轴": "横轴=去除的通道；纵轴=EER。",
        "结论要点": "定位主导 IMU 通道及三通道互补性。",
    },
}


def write_plot_notes(fig_dir: str | Path, names: list[str]) -> list[Path]:
    """Write a Chinese ``plots/{name}.md`` caption per produced figure (§12.3).

    Args:
        fig_dir: The figures directory (where the PDFs/PNGs live).
        names: The figure names that were actually produced.

    Returns:
        The list of written ``.md`` paths.
    """
    figs = Path(fig_dir)
    figs.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in names:
        desc = PLOT_DESCRIPTIONS.get(name)
        if not desc:
            continue
        lines = [f"# 图 `{name}` 说明", ""]
        for key in ("含义", "坐标轴", "结论要点"):
            if key in desc:
                lines.append(f"- **{key}**：{desc[key]}")
        lines += ["", f"> 图文件：`{name}.pdf` / `{name}.png`（Times New Roman、无标题、无中文、300dpi）。", ""]
        md_path = figs / f"{name}.md"
        md_path.write_text("\n".join(lines), encoding="utf-8")
        written.append(md_path)
    return written


def make_all_plots(results_dir: str | Path, fig_dir: str | Path, data_dir: str | Path | None = None) -> dict[str, list[Path]]:
    """Render every required figure, skipping any whose input is missing.

    Args:
        results_dir: The results root.
        fig_dir: The figures output dir.
        data_dir: Optional dataset dir (for the weak-label distribution figure).

    Returns:
        Mapping figure name -> written paths (empty list for skipped figures).
    """
    results = Path(results_dir)
    figs = Path(fig_dir)
    written: dict[str, list[Path]] = {}
    for name, func in PLOT_FUNCTIONS.items():
        if name == "weak_label_distribution":
            written[name] = func(results, figs, data_dir=Path(data_dir) if data_dir else None)
        else:
            written[name] = func(results, figs)
    # §12.3: a Chinese caption .md beside every figure actually produced.
    write_plot_notes(figs, [name for name, paths in written.items() if paths])
    return written
