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


def _find_split_manifest(results_dir: Path, data_dir: Path | None) -> dict[str, Any]:
    """Locate + load a ``split_manifest.json`` (data_dir first, else data/datasets)."""
    manifest_path: Path | None = None
    if data_dir is not None and (Path(data_dir) / "split_manifest.json").exists():
        manifest_path = Path(data_dir) / "split_manifest.json"
    else:
        candidates = sorted(Path("data/datasets").glob("*/split_manifest.json")) if Path("data/datasets").exists() else []
        manifest_path = candidates[0] if candidates else None
    return json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path is not None else {}


def _infer_provenance(manifest: dict[str, Any]) -> str:
    """Infer ``synthetic`` / ``real`` data provenance from a split manifest (SRV-15).

    Prefers an explicit ``data_provenance`` field; otherwise recomputes the
    synthetic generator's deterministic device ids (a hash of a fixed
    ``contextauth-synth-user`` prefix) — real device hashes never match it — so a
    real-data run is never mislabelled "synthetic".
    """
    if not manifest:
        return "unknown"
    if manifest.get("data_provenance"):
        return str(manifest["data_provenance"])
    devices = [str(d) for d in manifest.get("devices", [])] or [str(u) for u in manifest.get("users", [])]
    if not devices:
        return "unknown"
    try:
        from research.scripts.generate_synthetic_data import _device_id_for_user
    except Exception:  # pragma: no cover - generator always importable in-repo
        return "unknown"
    n = max(len(devices), int(manifest.get("n_users", len(devices)))) + 2
    synthetic_ids = {_device_id_for_user(i, s) for s in {int(manifest.get("seed", 42)), 42, 0} for i in range(n)}
    return "synthetic" if set(devices).issubset(synthetic_ids) else "real"


def _provenance_summary_line(provenance: str) -> str:
    """Return the conclusion-first data-provenance bullet for the executive summary."""
    if provenance == "real":
        return (
            "- **数据来源（真实采集）**：本报告基于**真实采集数据**端到端跑通流水线，"
            "效应量与显著性以真实数据为准；当前数据规模/覆盖度局限见第五节。"
        )
    if provenance == "synthetic":
        return (
            "- **严肃声明（合成数据）**：本报告基于**合成数据**跑通端到端流水线，"
            "仅用于验证方法与工程正确性，**不能替代真实多用户实证结论**；"
            "真实多用户数据下的效应量与显著性需在真机数据上复现。"
        )
    return (
        "- **数据来源（未标注）**：本次运行未显式标注数据来源（provenance=unknown）；"
        "若为合成数据，其结论仅证明流水线自洽，不代表真实世界效应。"
    )


def _pooled_ci(m: dict[str, Any]) -> dict[str, Any]:
    """Return the primary pooled-bootstrap CI dict (fallback to by-user; SRV-3)."""
    pooled = m.get("eer_pooled_bootstrap") or {}
    if pooled and str(pooled.get("ci_lo")).lower() != "nan":
        return pooled
    return m.get("eer_by_user_bootstrap", {})


def _undertraining_warnings(metrics: dict[str, dict[str, Any]]) -> list[str]:
    """List baselines that hit the epoch ceiling without early-stopping (SRV-9)."""
    flagged: list[str] = []
    for name, m in metrics.items():
        configured = m.get("epochs_configured")
        run = m.get("epochs_run")
        stopped = m.get("early_stopped")
        if configured and run and int(run) >= int(configured) and not stopped and int(configured) > 2:
            flagged.append(f"{name}(epochs_run={run}=上限)")
    return flagged


def _kstar(results_dir: Path) -> Any:
    """Return the frozen k* from ``topk_kstar.json`` (or ``None``)."""
    path = results_dir / "topk_kstar.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")).get("kstar")
    index = _load_runs_index(results_dir)
    return index.get("kstar")


def _executive_summary(results_dir: Path, metrics: dict[str, dict[str, Any]], provenance: str) -> list[str]:
    """Build the conclusion-first Chinese executive-summary lines."""
    kstar = _kstar(results_dir)
    m7 = metrics.get("m7", {})
    boot = _pooled_ci(m7)
    ci_label = "池化自助法" if (m7.get("eer_pooled_bootstrap") and str(m7.get("eer_pooled_bootstrap", {}).get("ci_lo")).lower() != "nan") else "by-user 自助法"
    lines = [
        "## 一、执行摘要（结论先行）",
        "",
        "- **核心方法（M7 weak-MoE）**：7 专家（I0–I6，即 App 的 7 个任务类）Mixture-of-Experts，"
        "学习式弱监督路由 + top-$k^*$ 稀疏门控，认证采用原型/余弦验证（enroll 与 query 会话严格不相交）。",
        f"- **最优专家数 $k^*$**：在**验证集**上冻结选出 $k^*={kstar}$，"
        "遵循与窗口长度搜索一致的“仅在验证/调参子集上选择、测试集只评一次”的纪律。",
        f"- **M7 等错误率 EER = {_fmt(m7.get('eer'))}**"
        f"（{ci_label} 95% CI = [{_fmt(boot.get('ci_lo'))}, {_fmt(boot.get('ci_hi'))}]，"
        f"ROC-AUC = {_fmt(m7.get('roc_auc'))}；主口径为 §18.3 池化重采样自助法）。",
        f"- **操作点（§9.7）**：FRR@FAR=1% = {_fmt(m7.get('frr_at_far_1pct'))}，"
        f"FRR@FAR=5% = {_fmt(m7.get('frr_at_far_5pct'))}，FAR@FRR=5% = {_fmt(m7.get('far_at_frr_5pct'))}；"
        f"检测策略（验证集选定、测试固定）= `{m7.get('detection_policy', {}).get('kind', 'raw')}`。",
        "- **与基线对比（EER，越低越好）**："
        f"M0 传感器-Dense={_eer(metrics, 'm0')}，M1 UI-Dense={_eer(metrics, 'm1')}，"
        f"M4 固定规则-top1={_eer(metrics, 'm4')}，M5 固定规则-top$k^*$={_eer(metrics, 'm5')}，"
        f"M6 仅认证-MoE={_eer(metrics, 'm6')}，M7={_eer(metrics, 'm7')}，"
        f"M8 去包名={_eer(metrics, 'm8')}（M7-vs-基线同索引配对 delta 与 Holm 校正见 `paired_deltas.csv`）。",
        _provenance_summary_line(provenance),
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
        f"输入维度 input_dim = {manifest.get('input_dim')}，场景体系：`{manifest.get('scene_taxonomy', 'I0..I6')}`（App 原生 7 任务，1:1，无 8→7 映射）。",
        f"- 用户数 = {len(manifest.get('users', []))}，会话数 = {len(manifest.get('sessions', []))}，"
        f"天数 = {len(manifest.get('days', []))}，包名桶 = {len(manifest.get('package_buckets', []))}。",
        f"- 窗口数（train/val/test）= {manifest.get('n_windows_train')}/{manifest.get('n_windows_val')}/{manifest.get('n_windows_test')}；"
        f"匹配 impostor 对 = {manifest.get('n_impostor_pairs')}。",
        f"- 弱标签分布（top1）：{manifest.get('weak_label_distribution', {})}。",
        f"- 受控任务金标签分布（canonical I0–I6）：{manifest.get('task_category_distribution', {})}；"
        f"原始 app 任务分布（含 legacy I7/C*）：{manifest.get('raw_task_category_distribution', {})}。",
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
        "- 每场景的专家激活矩阵见 `expert_scene_heatmap.pdf`（行=弱标签场景，列=专家 I0–I6），"
        "专家利用率见 `expert_utilization.pdf`。",
        "- 在 `leave_app_out` 协议下应额外关注弱标签分布漂移、KL 与场景激活漂移，"
        "以区分“编码器退化”与“路由弱监督失效”（OOD 路由鲁棒性）。",
        "",
    ]


def _limitations_repro(results_dir: Path, metrics: dict[str, dict[str, Any]], provenance: str) -> list[str]:
    """Build the limitations + reproducibility closing sections."""
    if provenance == "real":
        data_line = "- **真实数据**：结论以真实采集数据为准；受当前样本规模/用户覆盖度限制，认证类指标在单用户/少冒充对下可能退化为 N/A（见数据集概况的覆盖度字段）。"
    elif provenance == "synthetic":
        data_line = "- **合成数据（P0）**：结论仅证明流水线可运行与方法自洽，不代表真实世界效应。"
    else:
        data_line = "- **数据来源未标注**：若为合成数据，结论仅证明流水线自洽；请以 `--data-provenance` 显式标注以消除歧义。"
    undertrained = _undertraining_warnings(metrics)
    lines = [
        "## 五、局限性",
        "",
        data_line,
    ]
    if undertrained:
        lines.append(
            "- **欠训练风险（SRV-9）**：以下基线达到 epoch 上限且未触发早停，可能未收敛，建议提高 epochs 复跑："
            + "、".join(undertrained)
            + "。"
        )
    lines += [
        "- **事件级检测**：TTD / 每小时误报 / 攻击检出率按 (用户,会话) 时间流计算，检测策略（raw/k-of-n/EWMA）在验证集选定后测试固定；"
        "容量匹配（M2）为近似（记录参数量）；频域特征用 numpy rfft；小数据下自助法与配对显著性检验功效有限。",
        "- **任务体系（2026-07-03）**：金标/场景/专家 = App 原生 7 任务 `I0..I6`（1:1，恒等，无 8→7 映射）。"
        "旧盘数据经 legacy 重映射消化：`I7`→`I6`（手腕转动重编号）；旧 `I6` 空间采集（task_name=\"Scan, frame, and capture\"）"
        "与旧 `C0..C6` 一律置 `scene=None`、不计金标。弱标注规则为按新 7 类重键的启发式初版，质量校准属后续 P1 工作。",
        "",
        "## 六、可复现性",
        "",
        "- 每次 run 保存：`config.yaml`、`metrics.json`、`metrics.csv`、`per_user_metrics.csv`、"
        "`per_scene_metrics.csv`、`expert_utilization.csv`、`expert_scene_matrix.csv`、`model.pt`、"
        "`logs/train.jsonl`、`run_context.json`；并落盘逐对分数 `scores.parquet`/`scores_val.parquet`/"
        "`pair_scores.parquet` 与 `roc_points.csv`（供 ROC 重绘、§18.3 池化 CI 与同索引配对 delta 事后复算）。",
        "- 确定性种子、早停与最优 checkpoint；`run_context.json` 记录 python/torch/numpy 版本、"
        "git commit、配置哈希与硬件信息；`metrics.json` 记录 `epochs_run/epochs_configured/early_stopped`。",
        "- 复现命令：`build_datasets` → `run_all_experiments` → `make_report`。",
        "",
    ]
    return lines


def make_report(
    results_dir: str | Path,
    out_md: str | Path,
    *,
    data_dir: str | Path | None = None,
    data_provenance: str | None = None,
) -> Path:
    """Render figures + LaTeX tables and write the Chinese ``report.md``.

    Args:
        results_dir: The results root (with ``runs_index.json`` / sweep CSV / run
            dirs). If only a single ad-hoc run exists (no ``runs_index.json``),
            the figures that require the index skip with a message and the report
            is still produced.
        out_md: Destination path for ``report.md``.
        data_dir: Optional dataset dir (for the dataset summary + weak-label
            distribution figure).
        data_provenance: Explicit ``"synthetic"`` / ``"real"`` override for the
            data-source wording (SRV-15). ``None`` auto-infers from the split
            manifest so a real-data run is never mislabelled "synthetic".

    Returns:
        The path to the written ``report.md``.
    """
    results = Path(results_dir)
    out_path = Path(out_md)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig_dir = out_path.parent / "plots"

    # 0) data provenance (explicit override, else inferred from the split manifest).
    manifest = _find_split_manifest(results, Path(data_dir) if data_dir else None)
    provenance = str(data_provenance) if data_provenance else _infer_provenance(manifest)

    # 1) figures (skip-with-message on missing inputs; each gets a Chinese .md note).
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
        "（Times New Roman、无标题、无中文、PDF+PNG@300dpi），中文叙述仅在本 markdown 中；"
        f"数据来源：**{provenance}**。",
        "",
    ]
    lines += _executive_summary(results, metrics, provenance)
    lines += _dataset_summary(results, Path(data_dir) if data_dir else None)
    lines += _rq_sections(results, metrics)
    lines += _expert_specialization(results, metrics)

    # Figure index (only those actually produced; each has a sibling .md caption).
    lines += ["## 七、图表索引", ""]
    if produced:
        for name, paths in produced.items():
            pdfs = [p for p in paths if p.endswith(".pdf")]
            lines.append(f"- `{name}`：{pdfs[0] if pdfs else paths[0]}（中文说明见 `plots/{name}.md`）")
    else:
        lines.append("- （无可用结果，图表全部跳过；请先运行 `run_all_experiments`。）")
    lines += ["", f"- LaTeX 表格：`{tex_path.name}`（需 `\\usepackage{{booktabs}}`）。", ""]

    lines += _limitations_repro(results, metrics, provenance)

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
