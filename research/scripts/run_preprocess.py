"""Preprocess raw/synthetic batches into a windowed, weakly-labeled parquet.

Pipeline (``_BUILD_CONTRACT.md`` §11 S2 deliverable):

    load  -> align -> sessionize -> window -> features -> weak_label -> quality

Reads the ingest / synthetic ``devices/`` tree (and, if present, the
``envelopes/`` sidecars are ignored here — the batch files are authoritative),
flattens + sorts sensor samples, cuts sessions (gap / day / restart), slides
windows, extracts leakage-free features, weakly labels each window, computes
quality flags, and writes:

* ``{output}/windows.parquet`` — one row per window: flat feature columns +
  ``weak_label_top1`` / ``weak_label_topk_json`` / ``weak_label_probs_json`` /
  ``weak_label_confidence`` / ``weak_label_entropy`` + ``quality_flags_json`` +
  provenance ids + gold ``task_category`` (present for BUILTIN_TASK batches).
* ``{output}/feature_manifest.json`` — the model input contract (feature +
  package columns, ``input_dim``, ``leakage_free: True``).
* ``{output}/preprocess_report.json`` — counts + weak-label distribution +
  a leakage assertion result.

Run:
    python -m research.scripts.run_preprocess \
        --input data/synthetic --output data/processed \
        --window-size-sec 5 --stride-sec 1
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research import LEAKAGE_COLUMNS, SCENARIOS, canonical_scene_for_task
from research.config import load_config
from research.preprocessing.align import align_batches, index_batches
from research.preprocessing.feature_extractors import (
    build_feature_columns,
    build_feature_manifest,
    extract_window_features,
)
from research.preprocessing.loaders import iter_device_ids, load_batches
from research.preprocessing.quality import quality_flags
from research.preprocessing.sessionize import DEFAULT_STUDY_TIMEZONE, sessionize
from research.preprocessing.windowing import make_windows
from research.labeling.interaction_states import weak_label
from research.utils.io import ensure_dir, write_json
from research.utils.logging import get_logger

LOGGER = get_logger("research.preprocess")


def _touch_rate(window_ctx: dict[str, Any]) -> float:
    """Touch events per second inside a window (a labeling cue for I5/I6).

    Args:
        window_ctx: A window context.

    Returns:
        Touches per second over the window's nominal duration.
    """
    touches = window_ctx.get("touch_events") or []
    duration = float(window_ctx.get("window_duration_sec", 5.0))
    return float(len(touches)) / max(1e-9, duration)


def _window_row(
    window_ctx: dict[str, Any],
    feature_mode: str,
    *,
    temperature: float,
    low_conf_prob: float,
    low_conf_margin: float,
) -> dict[str, Any]:
    """Build the flat parquet row for one window context.

    Extracts features, weakly labels (injecting ``touch_rate`` as a cue), and
    computes quality flags. The gold ``task_category`` is copied from the
    window's IMU rows when the source is a BUILTIN_TASK batch (else ``None``).

    Args:
        window_ctx: A window context.
        feature_mode: Feature mode selecting the feature columns.
        temperature: Weak-label softmax temperature.
        low_conf_prob: Low-confidence probability threshold.
        low_conf_margin: Low-confidence margin threshold.

    Returns:
        A flat dict combining ids, features, weak-label fields, quality flags
        and the gold label.
    """
    features = extract_window_features(window_ctx, feature_mode=feature_mode)

    # The labeler reads a small set of cues; touch_rate is a window-context
    # signal (NOT a stored feature column) that I5/I6 use. We pass a shallow
    # superset dict; the labeler restricts itself to its allow-list.
    label_inputs = dict(features)
    label_inputs["touch_rate"] = _touch_rate(window_ctx)
    label = weak_label(
        label_inputs,
        temperature=temperature,
        low_conf_prob=low_conf_prob,
        low_conf_margin=low_conf_margin,
    )

    flags = quality_flags(window_ctx)
    if label["low_confidence"]:
        flags = list(flags) + ["low_confidence_label"]

    # Gold task_category (BUILTIN_TASK only): read from the window's IMU rows.
    # Store the canonical I0..I6 scene in task_category and keep the raw app code
    # separately for protocol/debugging. task_name is read alongside so the
    # mapping can distinguish the new wrist I6 from the deleted scan I6, and
    # legacy ids (I7 -> I6; C*/scan-I6 -> None) are digested here.
    imu = window_ctx.get("imu_samples")
    raw_task_category: str | None = None
    task_category: str | None = None
    if imu is not None and not imu.empty and "task_category" in imu:
        values = [v for v in imu["task_category"].tolist() if v is not None]
        if values:
            raw_task_category = str(values[0])
            raw_task_name: str | None = None
            if "task_name" in imu:
                names = [v for v in imu["task_name"].tolist() if v is not None]
                if names:
                    raw_task_name = str(names[0])
            task_category = canonical_scene_for_task(raw_task_category, raw_task_name)

    row: dict[str, Any] = {
        "device_id": window_ctx["device_id"],
        "session_id": window_ctx["session_id"],
        "day_id": window_ctx["day_id"],
        "window_id": window_ctx["window_id"],
        "user_id": window_ctx["user_id"],
        "package_bucket": window_ctx["package_bucket"],
        "start_elapsed_ns": int(window_ctx["start_elapsed_ns"]),
        "end_elapsed_ns": int(window_ctx["end_elapsed_ns"]),
        "start_wall_ms": int(window_ctx["start_wall_ms"]),
        "end_wall_ms": int(window_ctx["end_wall_ms"]),
    }
    row.update(features)  # flat feature columns
    row["weak_label_top1"] = label["top1"]
    row["weak_label_topk_json"] = json.dumps(label["topk"], ensure_ascii=False)
    row["weak_label_probs_json"] = json.dumps([float(p) for p in label["probs"]], ensure_ascii=False)
    row["weak_label_confidence"] = float(label["confidence"])
    row["weak_label_entropy"] = float(label["entropy"])
    row["weak_label_low_confidence"] = bool(label["low_confidence"])
    row["quality_flags_json"] = json.dumps(flags, ensure_ascii=False)
    row["task_category"] = task_category
    row["raw_task_category"] = raw_task_category
    return row


def _drop_short_sessions(
    sessioned: pd.DataFrame, min_session_seconds: float
) -> tuple[pd.DataFrame, list[str]]:
    """Drop analysis sessions whose wall-clock span is below the threshold.

    Removes carry-in fragment sessions (task 秒进秒出 and third-party interstitial
    fragments) BEFORE windowing so their windows never enter the pool (APP-10-B).
    The quality-flag vocabulary is untouched; session span is derivable from the
    data.

    Args:
        sessioned: A sessionized sensor frame.
        min_session_seconds: Minimum wall span (seconds); ``<= 0`` disables.

    Returns:
        ``(kept_frame, dropped_session_ids)``.
    """
    if min_session_seconds <= 0 or sessioned.empty:
        return sessioned, []
    span_sec = sessioned.groupby("session_id")["wall_time_estimated_millis"].agg(
        lambda s: (int(s.max()) - int(s.min())) / 1000.0
    )
    dropped = sorted(str(sid) for sid, span in span_sec.items() if span < min_session_seconds)
    if not dropped:
        return sessioned, []
    kept = sessioned[~sessioned["session_id"].astype(str).isin(dropped)].reset_index(drop=True)
    return kept, dropped


def _process_batch_group(
    batches: list[dict[str, Any]],
    *,
    window_size_sec: float,
    stride_sec: float,
    feature_mode: str,
    gap_min: float,
    study_timezone: str,
    min_session_seconds: float,
    drop_self_app_windows: bool,
    self_app_package: str,
    temperature: float,
    low_conf_prob: float,
    low_conf_margin: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run align -> sessionize -> drop-short -> window -> featurize for one group.

    A "group" is either every batch (one-shot) or a single device's batches (the
    SRV-11 per-device streaming path). Devices are independent through align /
    sessionize / windowing, so the union of per-device rows equals the one-shot
    rows (modulo order) and the returned stats aggregate cleanly (sums / set
    unions). This is the single code path both modes share.

    Args:
        batches: The batch dicts for this group.
        (others): Same meaning as :func:`run_preprocess`.

    Returns:
        ``(rows, stats)`` — the flat window rows and an aggregatable stats dict.
    """
    frame = align_batches(batches)
    batch_index = index_batches(batches)
    sessioned = sessionize(frame, gap_min=gap_min, study_timezone=study_timezone)
    n_sessions_pre = int(sessioned["session_id"].nunique()) if not sessioned.empty else 0
    sessioned, dropped_short = _drop_short_sessions(sessioned, min_session_seconds)
    windows = make_windows(
        sessioned, batch_index, window_size_sec=window_size_sec, stride_sec=stride_sec
    )
    rows = [
        _window_row(
            ctx,
            feature_mode,
            temperature=temperature,
            low_conf_prob=low_conf_prob,
            low_conf_margin=low_conf_margin,
        )
        for ctx in windows
    ]
    n_self_app = 0
    if drop_self_app_windows and rows:
        kept: list[dict[str, Any]] = []
        for row in rows:
            if row.get("raw_task_category") is None and str(row.get("package_bucket")) == self_app_package:
                n_self_app += 1
                continue
            kept.append(row)
        rows = kept
    stats = {
        "n_sensor_rows": int(len(frame)),
        "n_sessions_pre": n_sessions_pre,
        "n_sessions": int(sessioned["session_id"].nunique()) if not sessioned.empty else 0,
        "day_ids": set(sessioned["day_id"].astype(str)) if not sessioned.empty else set(),
        "device_ids": set(sessioned["device_id"].astype(str)) if not sessioned.empty else set(),
        "dropped_short_sessions": list(dropped_short),
        "n_self_app_windows_dropped": n_self_app,
    }
    return rows, stats


def run_preprocess(
    input_dir: str | Path,
    output_dir: str | Path,
    *,
    window_size_sec: float,
    stride_sec: float,
    feature_mode: str = "ui_sensor",
    gap_min: float = 10.0,
    temperature: float = 1.0,
    low_conf_prob: float = 0.35,
    low_conf_margin: float = 0.10,
    study_timezone: str = DEFAULT_STUDY_TIMEZONE,
    min_session_seconds: float = 5.0,
    drop_self_app_windows: bool = True,
    self_app_package: str = "com.contextauth",
    stream_by_device: bool = False,
) -> dict[str, Any]:
    """Run the full preprocessing pipeline and write outputs to ``output_dir``.

    Args:
        input_dir: Dataset root containing ``devices/``.
        output_dir: Destination directory for the parquet + manifests.
        window_size_sec: Window length in seconds.
        stride_sec: Window stride in seconds.
        feature_mode: Feature mode (selects feature columns).
        gap_min: Session-cut inter-sample gap in minutes.
        temperature: Weak-label softmax temperature.
        low_conf_prob: Low-confidence probability threshold.
        low_conf_margin: Low-confidence margin threshold.
        study_timezone: IANA timezone for the ``day_id`` calendar day (SRV-12).
        min_session_seconds: Drop sessions whose wall span is below this before
            windowing (APP-10-B; ``<= 0`` disables).
        drop_self_app_windows: Drop THIRD_PARTY windows whose ``package_bucket``
            is the collector's own app (APP-2-B; gold BUILTIN windows are never
            dropped). ``False`` disables.
        self_app_package: The collector's own package name (APP-2-B).
        stream_by_device: If True, load + process ONE device at a time (SRV-11),
            bounding peak memory to a single device. Devices are independent, so
            the windowed output is identical to the one-shot path modulo row
            order. Falls back to one-shot when there is no ``devices/`` dir.

    Returns:
        The preprocess report dict (also written to
        ``preprocess_report.json``).
    """
    input_dir = Path(input_dir)
    output_dir = ensure_dir(output_dir)

    process_kwargs = dict(
        window_size_sec=window_size_sec,
        stride_sec=stride_sec,
        feature_mode=feature_mode,
        gap_min=gap_min,
        study_timezone=study_timezone,
        min_session_seconds=min_session_seconds,
        drop_self_app_windows=drop_self_app_windows,
        self_app_package=self_app_package,
        temperature=temperature,
        low_conf_prob=low_conf_prob,
        low_conf_margin=low_conf_margin,
    )

    # SRV-11: process one device at a time (peak memory bounded to a device) or
    # all at once. Both call the SAME _process_batch_group; devices are
    # independent so the union of rows is identical modulo order.
    device_ids = iter_device_ids(input_dir) if stream_by_device else []
    rows: list[dict[str, Any]] = []
    n_batches = 0
    agg_sensor_rows = 0
    agg_sessions_pre = 0
    agg_sessions = 0
    agg_day_ids: set[str] = set()
    agg_device_ids: set[str] = set()
    dropped_short_sessions: list[str] = []
    n_self_app_windows_dropped = 0

    if stream_by_device and device_ids:
        LOGGER.info("streaming %d device shard(s) from %s", len(device_ids), input_dir)
        # Lazy: each device's batches are loaded (and freed) one at a time, so
        # peak memory is bounded to a single device — the point of SRV-11.
        groups = ((dev, list(load_batches(input_dir, strict=False, device_id=dev))) for dev in device_ids)
    else:
        if stream_by_device:
            LOGGER.info("no devices/ dir under %s; falling back to one-shot", input_dir)
        groups = iter([("__all__", list(load_batches(input_dir, strict=False)))])

    for _group_key, group_batches in groups:
        if not group_batches:
            continue
        n_batches += len(group_batches)
        group_rows, stats = _process_batch_group(group_batches, **process_kwargs)
        rows.extend(group_rows)
        agg_sensor_rows += int(stats["n_sensor_rows"])
        agg_sessions_pre += int(stats["n_sessions_pre"])
        agg_sessions += int(stats["n_sessions"])
        agg_day_ids |= stats["day_ids"]
        agg_device_ids |= stats["device_ids"]
        dropped_short_sessions.extend(stats["dropped_short_sessions"])
        n_self_app_windows_dropped += int(stats["n_self_app_windows_dropped"])

    dropped_short_sessions = sorted(dropped_short_sessions)
    n_sessions_pre_filter = agg_sessions_pre
    LOGGER.info("loaded %d batches from %s", n_batches, input_dir)
    LOGGER.info("built %d windows", len(rows))
    if dropped_short_sessions:
        LOGGER.info(
            "dropped %d sub-%.1fs sessions before windowing", len(dropped_short_sessions), min_session_seconds
        )
    if n_self_app_windows_dropped:
        LOGGER.info("dropped %d self-app (%s) third-party windows", n_self_app_windows_dropped, self_app_package)

    feature_columns = build_feature_columns(feature_mode)

    # Assemble the parquet with a stable, explicit column order.
    id_columns = [
        "device_id",
        "session_id",
        "day_id",
        "window_id",
        "user_id",
        "package_bucket",
        "start_elapsed_ns",
        "end_elapsed_ns",
        "start_wall_ms",
        "end_wall_ms",
    ]
    label_columns = [
        "weak_label_top1",
        "weak_label_topk_json",
        "weak_label_probs_json",
        "weak_label_confidence",
        "weak_label_entropy",
        "weak_label_low_confidence",
        "quality_flags_json",
        "task_category",
        "raw_task_category",
    ]
    all_columns = id_columns + feature_columns + label_columns
    if rows:
        df = pd.DataFrame(rows)
        df = df.reindex(columns=all_columns)
    else:
        df = pd.DataFrame(columns=all_columns)

    # Hard leakage guard: no stored column may be a leakage column.
    leaked = sorted(set(df.columns) & LEAKAGE_COLUMNS)
    if leaked:  # pragma: no cover - defensive, feature vocab excludes these
        raise AssertionError(f"leakage columns present in windows parquet: {leaked}")

    parquet_path = output_dir / "windows.parquet"
    df.to_parquet(parquet_path, engine="pyarrow", index=False)

    manifest = build_feature_manifest(feature_mode)
    write_json(output_dir / "feature_manifest.json", manifest)

    # Weak-label distribution over the 7 scenarios.
    if rows:
        top1_counts = df["weak_label_top1"].value_counts().to_dict()
    else:
        top1_counts = {}
    weak_label_distribution = {scenario: int(top1_counts.get(scenario, 0)) for scenario in SCENARIOS}

    gold_agreement: float | None = None
    if rows and df["task_category"].notna().any():
        labeled = df[df["task_category"].notna()]
        gold_agreement = float(np.mean(labeled["weak_label_top1"].values == labeled["task_category"].values))

    report: dict[str, Any] = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "feature_mode": feature_mode,
        "window_size_sec": float(window_size_sec),
        "stride_sec": float(stride_sec),
        "gap_min": float(gap_min),
        "study_timezone": study_timezone,
        "day_id_timezone": study_timezone,
        "min_session_seconds": float(min_session_seconds),
        "drop_self_app_windows": bool(drop_self_app_windows),
        "self_app_package": self_app_package,
        "stream_by_device": bool(stream_by_device),
        "n_batches": n_batches,
        "n_sensor_rows": agg_sensor_rows,
        "n_sessions_pre_short_filter": n_sessions_pre_filter,
        "n_short_sessions_dropped": len(dropped_short_sessions),
        "short_session_ids": dropped_short_sessions,
        "n_self_app_windows_dropped": n_self_app_windows_dropped,
        "n_sessions": agg_sessions,
        "n_days": len(agg_day_ids),
        "n_devices": len(agg_device_ids),
        "n_windows": int(len(df)),
        "n_feature_columns": len(feature_columns),
        "weak_label_distribution": weak_label_distribution,
        "weak_label_top1_vs_gold_agreement": gold_agreement,
        "n_low_confidence": int(df["weak_label_low_confidence"].sum()) if rows else 0,
        "leakage_columns_in_parquet": leaked,
        "leakage_free": True,
        "example_feature_columns": feature_columns[:12],
    }
    write_json(output_dir / "preprocess_report.json", report)
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        A configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        prog="run_preprocess",
        description="Preprocess raw/synthetic batches into a windowed, weakly-labeled parquet.",
    )
    parser.add_argument("--input", type=Path, required=True, help="dataset root containing devices/")
    parser.add_argument("--output", type=Path, required=True, help="output directory for parquet + manifests")
    parser.add_argument("--window-size-sec", type=float, default=5.0, help="window length (seconds)")
    parser.add_argument("--stride-sec", type=float, default=1.0, help="window stride (seconds)")
    parser.add_argument(
        "--feature-mode",
        type=str,
        default=None,
        help="feature mode (default: from configs/default.yaml features.mode)",
    )
    parser.add_argument(
        "--min-session-seconds",
        type=float,
        default=None,
        help="drop sessions shorter than this wall span before windowing "
        "(default: from preprocess.min_session_seconds, 5.0)",
    )
    parser.add_argument(
        "--study-timezone",
        type=str,
        default=None,
        help="IANA timezone for the day_id calendar day (default: from preprocess.study_timezone)",
    )
    parser.add_argument(
        "--self-app-package",
        type=str,
        default=None,
        help="collector's own package for self-app window drop (default: from preprocess.self_app_package)",
    )
    parser.add_argument(
        "--keep-self-app-windows",
        action="store_true",
        help="disable the APP-2-B self-app third-party window drop",
    )
    parser.add_argument(
        "--stream-by-device",
        action="store_true",
        help="SRV-11: process one device at a time to bound peak memory "
        "(output identical to one-shot modulo row order)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="optional config override YAML (defaults merged over default.yaml)",
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
    labeling_cfg = cfg.get("labeling", {})
    preprocess_cfg = cfg.get("preprocess", {})

    study_timezone = args.study_timezone or str(preprocess_cfg.get("study_timezone", DEFAULT_STUDY_TIMEZONE))
    min_session_seconds = (
        args.min_session_seconds
        if args.min_session_seconds is not None
        else float(preprocess_cfg.get("min_session_seconds", 5.0))
    )
    self_app_package = args.self_app_package or str(preprocess_cfg.get("self_app_package", "com.contextauth"))
    drop_self_app_windows = (
        False if args.keep_self_app_windows else bool(preprocess_cfg.get("drop_self_app_windows", True))
    )

    report = run_preprocess(
        args.input,
        args.output,
        window_size_sec=args.window_size_sec,
        stride_sec=args.stride_sec,
        feature_mode=feature_mode,
        gap_min=float(preprocess_cfg.get("gap_min", 10.0)),
        temperature=float(labeling_cfg.get("temperature", 1.0)),
        low_conf_prob=float(labeling_cfg.get("low_conf_prob", 0.35)),
        low_conf_margin=float(labeling_cfg.get("low_conf_margin", 0.10)),
        study_timezone=study_timezone,
        min_session_seconds=min_session_seconds,
        drop_self_app_windows=drop_self_app_windows,
        self_app_package=self_app_package,
        stream_by_device=bool(args.stream_by_device),
    )

    print("=== preprocess summary ===")
    print(f"input_dir         : {report['input_dir']}")
    print(f"output_dir        : {report['output_dir']}")
    print(f"feature_mode      : {report['feature_mode']}")
    print(f"study_timezone    : {report['study_timezone']}")
    print(f"n_batches         : {report['n_batches']}")
    print(f"n_sensor_rows     : {report['n_sensor_rows']}")
    print(f"n_devices         : {report['n_devices']}")
    print(f"n_sessions        : {report['n_sessions']}")
    print(f"short_sess_dropped: {report['n_short_sessions_dropped']} (min {report['min_session_seconds']}s)")
    print(f"self_app_dropped  : {report['n_self_app_windows_dropped']} (pkg {report['self_app_package']})")
    print(f"n_days            : {report['n_days']}")
    print(f"n_windows         : {report['n_windows']}")
    print(f"n_feature_columns : {report['n_feature_columns']}")
    print(f"weak_label_dist   : {report['weak_label_distribution']}")
    print(f"top1_vs_gold_agree: {report['weak_label_top1_vs_gold_agreement']}")
    print(f"n_low_confidence  : {report['n_low_confidence']}")
    print(f"leakage_free      : {report['leakage_free']}")
    print(f"example_features  : {report['example_feature_columns']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
