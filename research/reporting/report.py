"""Chinese markdown report generator (build contract §11 S4, spec §12).

:func:`make_report` renders every publication figure (into ``plots/`` beside the
report), writes ``latex_tables.tex``, and composes a **conclusion-first Chinese**
``report.md`` organised as Executive Summary -> Dataset Summary -> RQ1..RQ7 ->
Expert Specialization -> Limitations -> Reproducibility. The figures themselves
contain NO Chinese and NO titles (that constraint lives in
:mod:`research.reporting.plots`); all Chinese narrative is in the markdown.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research.reporting import plots, tables


def _load_runs_index(results_dir: Path) -> dict[str, Any]:
    """Load ``runs_index.json`` (empty dict when absent)."""
    path = results_dir / "runs_index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _run_metrics(results_dir: Path) -> dict[str, dict[str, Any]]:
    """Return ``{baseline: metrics.json}`` from the runs index."""
    index = _load_runs_index(results_dir)
    out: dict[str, dict[str, Any]] = {}
    for name, info in index.get("runs", {}).items():
        mpath = Path(info.get("run_dir", "")) / "metrics.json"
        if mpath.exists():
            out[name] = json.loads(mpath.read_text(encoding="utf-8"))
    return out


def _fmt(value: Any, precision: int = 4) -> str:
    """Format a metric for markdown (``N/A`` for nan/None)."""
    try:
        if value is None or str(value).lower() == "nan":
            return "N/A"
        return f"{float(value):.{precision}f}"
    except (TypeError, ValueError):
        return str(value)


def _eer(metrics: dict[str, dict[str, Any]], name: str) -> str:
    """EER of a baseline as a formatted string (``N/A`` if the run is missing)."""
    return _fmt(metrics.get(name, {}).get("eer")) if name in metrics else "N/A"


def _kstar(results_dir: Path) -> Any:
    """Return the frozen k* from ``topk_kstar.json`` (or ``None``)."""
    path = results_dir / "topk_kstar.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")).get("kstar")
    index = _load_runs_index(results_dir)
    return index.get("kstar")


def _executive_summary(results_dir: Path, metrics: dict[str, dict[str, Any]]) -> list[str]:
    """Build the conclusion-first Chinese executive-summary lines."""
    kstar = _kstar(results_dir)
    m7 = metrics.get("m7", {})
    boot = m7.get("eer_by_user_bootstrap", {})
    lines = [
        "## 一、执行摘要（结论先行）",
        "",
        "- **核心方法（M7 weak-MoE）**：7 专家（C0–C6）Mixture-of-Experts，"
        "学习式弱监督路由 + top-$k^*$ 稀疏门控，认证采用原型/余弦验证（enroll 与 query 会话严格不相交）。",
        f"- **最优专家数 $k^*$**：在**验证集**上冻结选出 $k^*={kstar}$，"
        "遵循与窗口长度搜索一致的“仅在验证/调参子集上选择、测试集只评一次”的纪律。",
        f"- **M7 等错误率 EER = {_fmt(m7.get('eer'))}**"
        f"（by-user 自助法 95% CI = [{_fmt(boot.get('ci_lo'))}, {_fmt(boot.get('ci_hi'))}]，"
        f"ROC-AUC = {_fmt(m7.get('roc_auc'))}）。",
        "- **与基线对比（EER，越低越好）**："
        f"M0 传感器-Dense={_eer(metrics, 'm0')}，M1 UI-Dense={_eer(metrics, 'm1')}，"
        f"M4 固定规则-top1={_eer(metrics, 'm4')}，M5 固定规则-top$k^*$={_eer(metrics, 'm5')}，"
        f"M6 仅认证-MoE={_eer(metrics, 'm6')}，M7={_eer(metrics, 'm7')}，"
        f"M8 去包名={_eer(metrics, 'm8')}。",
        "- **严肃声明（P0）**：本报告基于**合成数据**跑通端到端流水线，"
        "仅用于验证方法与工程正确性，**不能替代真实多用户实证结论**；"
        "真实多用户数据下的效应量与显著性需在真机数据上复现。",
        "",
    ]
    return lines


def _dataset_summary(results_dir: Path, data_dir: Path | None) -> list[str]:
    """Build the dataset-summary section from a split manifest (if available)."""
    manifest: dict[str, Any] = {}
    manifest_path: Path | None = None
    if data_dir is not None and (Path(data_dir) / "split_manifest.json").exists():
        manifest_path = Path(data_dir) / "split_manifest.json"
    else:
        candidates = sorted(Path("data/datasets").glob("*/split_manifest.json")) if Path("data/datasets").exists() else []
        manifest_path = candidates[0] if candidates else None
    if manifest_path is not None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    lines = ["## 二、数据集概况", ""]
    if not manifest:
        lines += ["- 未找到 `split_manifest.json`，跳过数据集概况。", ""]
        return lines
    leak = manifest.get("leakage_check", {})
    lines += [
        f"- 划分协议：`{manifest.get('protocol')}`，特征模式：`{manifest.get('feature_mode')}`，"
        f"输入维度 input_dim = {manifest.get('input_dim')}，任务映射：`{manifest.get('task_mapping', 'recommended')}`。",
        f"- 用户数 = {len(manifest.get('users', []))}，会话数 = {len(manifest.get('sessions', []))}，"
        f"天数 = {len(manifest.get('days', []))}，包名桶 = {len(manifest.get('package_buckets', []))}。",
        f"- 窗口数（train/val/test）= {manifest.get('n_windows_train')}/{manifest.get('n_windows_val')}/{manifest.get('n_windows_test')}；"
        f"匹配 impostor 对 = {manifest.get('n_impostor_pairs')}。",
        f"- 弱标签分布（top1）：{manifest.get('weak_label_distribution', {})}。",
        f"- 受控任务金标签分布（canonical C0–C6）：{manifest.get('task_category_distribution', {})}；"
        f"原始 app 任务分布：{manifest.get('raw_task_category_distribution', {})}。",
        "- **泄漏自检**（全部必须为真）："
        + "，".join(f"{k}={v}" for k, v in leak.items())
        + "。",
        "- 已排除的 4 个泄漏列：`estimated_context_category`、`game_like_score`、"
        "`viewIdResourceName`、`coarse_orientation`（IMU 自导出的 `orient_landscape` 布尔量允许使用）。",
        "",
    ]
    return lines


def _rq_sections(results_dir: Path, metrics: dict[str, dict[str, Any]]) -> list[str]:
    """Build the RQ1..RQ7 comparison sections (conclusion + evidence)."""
    kstar = _kstar(results_dir)
    return [
        "## 三、研究问题（RQ1–RQ7）",
        "",
        "### RQ1 UI 结构是否有帮助？（M0 / M1 / M7）",
        f"- 证据：M0 传感器-Dense EER={_eer(metrics, 'm0')}，M1 UI+传感器-Dense EER={_eer(metrics, 'm1')}，"
        f"M7 弱监督-MoE EER={_eer(metrics, 'm7')}。加入 UI 结构与专家路由后 EER 变化见 `eer_bar.pdf`。",
        "",
        "### RQ2 MoE 是否优于容量匹配的 Dense？（M1 / M2 / M7）",
        f"- 证据：M2 容量匹配-Dense EER={_eer(metrics, 'm2')}（参数量与 M7 近似，用于排除“容量”混淆），"
        f"对比 M7 EER={_eer(metrics, 'm7')}。参数量记录于各自 `metrics.json`。",
        "",
        "### RQ3 学习式弱路由 vs 固定规则？（M4 / M5 / M7）",
        f"- 证据：M4 固定规则-top1 EER={_eer(metrics, 'm4')}，M5 固定规则-top$k^*$ EER={_eer(metrics, 'm5')}"
        f"（强基线，使用同一 $k^*={kstar}$，未被削弱），对比 M7 学习式路由 EER={_eer(metrics, 'm7')}。",
        "",
        "### RQ4 弱监督 vs 仅认证的 MoE？（M6 / M7）",
        f"- 证据：M6 去掉路由 KL 弱监督（$\\lambda_{{scene}}=0$）EER={_eer(metrics, 'm6')}，"
        f"对比 M7（含弱监督）EER={_eer(metrics, 'm7')}；二者架构与 $k^*$ 相同。",
        "",
        "### RQ5 top-$k$ 与 $k^*$ 选择？（1..7 扫描 + Pareto）",
        f"- 结论：$k^*={kstar}$，在**验证集**上按“最小代价且 EER 不显著劣于最优”的准则冻结选出。"
        "详见 `topk_ablation.pdf`、`topk_eer_latency_pareto.pdf` 与 `topk_sweep.csv`；"
        "$k^*$ 溯源与 ±1 敏感性记录于 `topk_kstar.json`。",
        "",
        "### RQ6 是否依赖 App/包名？（M3 / M7 / M8 + leave_app_out）",
        f"- 证据：M3 仅包名路由 EER={_eer(metrics, 'm3')}，M8 去包名特征 EER={_eer(metrics, 'm8')}，"
        f"对比 M7 EER={_eer(metrics, 'm7')}。若 M8 与 M7 接近，则说明模型未过度依赖包名。见 `package_ablation.pdf`。",
        "",
        "### RQ7 隐私 / 成本 / 部署",
        "- 记录：每窗口推理延迟（`topk_sweep.csv` 的 `latency_ms`）、活跃专家数、活跃参数量、"
        "LZ4 压缩、`encryption:none`（TLS 机密性 + 对压缩字节做 SHA-256 完整性）、drop-all-text。"
        "隐私/特征模式代价见 `privacy_ablation.pdf` 与 `feature_ablation.pdf`。",
        "",
    ]


def _expert_specialization(results_dir: Path, metrics: dict[str, dict[str, Any]]) -> list[str]:
    """Build the expert-specialization section (router/expert diagnostics)."""
    m7 = metrics.get("m7", {})
    return [
        "## 四、专家专化分析",
        "",
        f"- 路由熵 router_entropy = {_fmt(m7.get('router_entropy'))}，"
        f"专家利用熵 expert_utilization_entropy = {_fmt(m7.get('expert_utilization_entropy'))}。",
        "- 每场景的专家激活矩阵见 `expert_scene_heatmap.pdf`（行=弱标签场景，列=专家 C0–C6），"
        "专家利用率见 `expert_utilization.pdf`。",
        "- 在 `leave_app_out` 协议下应额外关注弱标签分布漂移、KL 与场景激活漂移，"
        "以区分“编码器退化”与“路由弱监督失效”（OOD 路由鲁棒性）。",
        "",
    ]


def _limitations_repro(results_dir: Path) -> list[str]:
    """Build the limitations + reproducibility closing sections."""
    return [
        "## 五、局限性",
        "",
        "- **合成数据（P0）**：结论仅证明流水线可运行与方法自洽，不代表真实世界效应。",
        "- **最小可用实现**：TTD / 每小时误报为事件级最小实现；容量匹配（M2）为近似（记录参数量）；"
        "频域特征用 numpy rfft；小数据下 by-user 自助法与配对显著性检验功效有限。",
        "- 现有 Android app 仍是 `I0..I7` 八任务协议；server 研究层通过 `raw_task_category -> C0..C6`"
        "映射兼容真实采集数据。若 app 未来迁移为原生 `C0..C6`，映射会退化为恒等。",
        "",
        "## 六、可复现性",
        "",
        "- 每次 run 保存：`config.yaml`、`metrics.json`、`metrics.csv`、`per_user_metrics.csv`、"
        "`per_scene_metrics.csv`、`expert_utilization.csv`、`expert_scene_matrix.csv`、`model.pt`、"
        "`logs/train.jsonl`、`run_context.json`。",
        "- 确定性种子、早停与最优 checkpoint；`run_context.json` 记录 python/torch/numpy 版本、"
        "git commit、配置哈希与硬件信息。",
        "- 复现命令：`build_datasets` → `run_all_experiments` → `make_report`。",
        "",
    ]


def make_report(results_dir: str | Path, out_md: str | Path, *, data_dir: str | Path | None = None) -> Path:
    """Render figures + LaTeX tables and write the Chinese ``report.md``.

    Args:
        results_dir: The results root (with ``runs_index.json`` / sweep CSV / run
            dirs). If only a single ad-hoc run exists (no ``runs_index.json``),
            the figures that require the index skip with a message and the report
            is still produced.
        out_md: Destination path for ``report.md``.
        data_dir: Optional dataset dir (for the dataset summary + weak-label
            distribution figure).

    Returns:
        The path to the written ``report.md``.
    """
    results = Path(results_dir)
    out_path = Path(out_md)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig_dir = out_path.parent / "plots"

    # 1) figures (skip-with-message on missing inputs).
    written = plots.make_all_plots(results, fig_dir, data_dir=data_dir)
    produced = {name: [str(p) for p in paths] for name, paths in written.items() if paths}

    # 2) LaTeX tables.
    tex_path = out_path.parent / "latex_tables.tex"
    tables.write_latex_tables(results, tex_path)

    # 3) Chinese markdown (conclusion-first).
    metrics = _run_metrics(results)
    lines: list[str] = [
        "# ContextAuth 实验报告",
        "",
        "> 本报告由 `research.reporting.report.make_report` 自动生成；图表为出版级"
        "（Times New Roman、无标题、无中文、PDF+PNG@300dpi），中文叙述仅在本 markdown 中。",
        "",
    ]
    lines += _executive_summary(results, metrics)
    lines += _dataset_summary(results, Path(data_dir) if data_dir else None)
    lines += _rq_sections(results, metrics)
    lines += _expert_specialization(results, metrics)

    # Figure index (only those actually produced).
    lines += ["## 七、图表索引", ""]
    if produced:
        for name, paths in produced.items():
            pdfs = [p for p in paths if p.endswith(".pdf")]
            lines.append(f"- `{name}`：{pdfs[0] if pdfs else paths[0]}")
    else:
        lines.append("- （无可用结果，图表全部跳过；请先运行 `run_all_experiments`。）")
    lines += ["", f"- LaTeX 表格：`{tex_path.name}`（需 `\\usepackage{{booktabs}}`）。", ""]

    lines += _limitations_repro(results)

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
