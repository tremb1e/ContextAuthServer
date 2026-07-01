"""Report generation: report.md + figures with no title / no CJK — §15.1.9.

Builds a minimal results tree (a ``runs_index.json`` + one M7 run's metrics +
the top-k sweep CSV), then:

* asserts :func:`research.reporting.report.make_report` writes ``report.md`` +
  ``latex_tables.tex`` and at least one figure PDF *and* PNG;
* captures every produced :class:`matplotlib.figure.Figure` (by monkeypatching
  the single ``plots.save`` chokepoint) and asserts each figure has **NO axes
  title** and that **NO in-figure text element contains a CJK character** (axes
  titles, axis labels, tick labels, legend entries, annotations, suptitle).
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib
import pytest

matplotlib.use("Agg")

from research.reporting import plots, report

_SCENES = ["C0", "C1", "C2", "C3", "C4", "C5", "C6"]


def _has_cjk(text: str) -> bool:
    """Return True if ``text`` contains any CJK / fullwidth character."""
    for ch in text:
        code = ord(ch)
        if (
            0x4E00 <= code <= 0x9FFF      # CJK Unified Ideographs
            or 0x3400 <= code <= 0x4DBF   # CJK Extension A
            or 0x3000 <= code <= 0x303F   # CJK symbols/punctuation
            or 0xFF00 <= code <= 0xFFEF   # fullwidth forms
        ):
            return True
    return False


def _figure_texts(fig: "matplotlib.figure.Figure") -> list[str]:
    """Collect every rendered text string on a figure (labels, ticks, legend...)."""
    texts: list[str] = [t.get_text() for t in fig.texts]
    if fig._suptitle is not None:  # type: ignore[attr-defined]
        texts.append(fig._suptitle.get_text())  # type: ignore[attr-defined]
    for ax in fig.axes:
        texts.append(ax.get_title())
        texts.append(ax.get_xlabel())
        texts.append(ax.get_ylabel())
        texts += [t.get_text() for t in ax.get_xticklabels()]
        texts += [t.get_text() for t in ax.get_yticklabels()]
        texts += [t.get_text() for t in ax.texts]  # annotations
        legend = ax.get_legend()
        if legend is not None:
            texts += [t.get_text() for t in legend.get_texts()]
    return [t for t in texts if t]


@pytest.fixture()
def results_dir(tmp_path: Path) -> Path:
    """Build a minimal but plot-renderable results tree."""
    res = tmp_path / "results"
    run = res / "m7run"
    run.mkdir(parents=True)
    (run / "metrics.json").write_text(
        json.dumps(
            {
                "eer": 0.2, "roc_auc": 0.82, "pr_auc": 0.7,
                "eer_by_user_bootstrap": {"mean": 0.2, "ci_lo": 0.15, "ci_hi": 0.25},
                "per_scene_eer": {"C0": 0.10, "C1": 0.22, "C3": 0.31},
                "router_entropy": 1.4, "expert_utilization_entropy": 1.5,
                "router_probs_mean": [0.2, 0.1, 0.1, 0.15, 0.15, 0.15, 0.15],
                "expert_utilization": [0.3, 0.1, 0.1, 0.1, 0.1, 0.1, 0.2],
                "n_genuine_pairs": 40, "n_impostor_pairs": 40,
            }
        ),
        encoding="utf-8",
    )
    (run / "expert_utilization.csv").write_text(
        "expert,utilization,router_prob_mean\n" + "".join(f"{s},0.14,0.14\n" for s in _SCENES),
        encoding="utf-8",
    )
    header = "scene," + ",".join(f"expert_{s}" for s in _SCENES)
    matrix = "\n".join([header] + [s + "," + ",".join(["0.14"] * 7) for s in _SCENES])
    (run / "expert_scene_matrix.csv").write_text(matrix + "\n", encoding="utf-8")

    (res / "runs_index.json").write_text(
        json.dumps({"kstar": 2, "runs": {"m7": {"label": "weak_moe", "run_dir": str(run), "eer": 0.2}}}),
        encoding="utf-8",
    )
    (res / "topk_sweep.csv").write_text(
        "k,eer,roc_auc,avg_active_experts,latency_ms,param_count,active_param_count\n"
        "1,0.30,0.70,1.0,0.5,1000,500\n2,0.20,0.82,2.0,0.6,1000,700\n",
        encoding="utf-8",
    )
    (res / "topk_kstar.json").write_text(json.dumps({"kstar": 2}), encoding="utf-8")
    return res


def test_make_report_writes_md_and_figures(results_dir: Path, tmp_path: Path) -> None:
    """report.md + latex_tables.tex + at least one PDF/PNG figure are produced."""
    out_md = tmp_path / "out" / "report.md"
    report_path = report.make_report(results_dir, out_md, data_dir=None)
    assert report_path.exists()
    assert (report_path.parent / "latex_tables.tex").exists()
    plots_dir = report_path.parent / "plots"
    pdfs = list(plots_dir.glob("*.pdf"))
    pngs = list(plots_dir.glob("*.png"))
    assert pdfs, "expected at least one PDF figure"
    assert pngs, "expected at least one PNG figure"
    # The report is Chinese narrative (contains CJK) — that is expected in markdown.
    assert _has_cjk(report_path.read_text(encoding="utf-8"))


def test_figures_have_no_title_and_no_cjk(results_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every produced figure has no axes title and no CJK in any text element."""
    captured: list[tuple[str, list[str], list[str]]] = []
    original_save = plots.save

    def _capturing_save(fig, name, fig_dir):  # type: ignore[no-untyped-def]
        titles = [ax.get_title() for ax in fig.axes]
        captured.append((name, titles, _figure_texts(fig)))
        return original_save(fig, name, fig_dir)

    monkeypatch.setattr(plots, "save", _capturing_save)

    out_md = tmp_path / "out" / "report.md"
    report.make_report(results_dir, out_md, data_dir=None)

    assert captured, "expected at least one figure to be rendered"
    for name, titles, texts in captured:
        assert all(t == "" for t in titles), f"figure {name!r} has a non-empty axes title: {titles}"
        offenders = [t for t in texts if _has_cjk(t)]
        assert not offenders, f"figure {name!r} contains CJK text: {offenders}"
