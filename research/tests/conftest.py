"""Shared pytest fixtures — build ONE tiny synthetic dataset for the whole suite.

A single session-scoped fixture (:func:`_pipeline`) runs the real pipeline end to
end on a tiny synthetic run (5 users, 2 days, 2 sessions/day, seed 42) in a
session-scoped temp dir:

    generate_synthetic_data (+ envelopes)  ->  run_preprocess  ->  build_dataset

and exposes the resulting paths as small fixtures (``synthetic_dir``,
``processed_dir``, ``dataset_dir``, ``windows_parquet``, ``feature_manifest``).
Everything downstream is tiny so the suite is SMOKE-fast.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from research.scripts.generate_synthetic_data import generate
from research.scripts.run_preprocess import run_preprocess
from research.datasets.builders import build_dataset

# Tiny fixture dataset knobs (keep small -> fast; still enough for splits/pairs).
_USERS = 5
_DAYS = 2
_SESSIONS_PER_DAY = 2
_SEED = 42
_WINDOW_SIZE_SEC = 5.0
_STRIDE_SEC = 1.0
_PROTOCOL = "leave_session_out"
_FEATURE_MODE = "ui_sensor"


@dataclass(frozen=True)
class _Pipeline:
    """Resolved on-disk paths of the built tiny synthetic dataset.

    Attributes:
        synthetic_dir: Root of the generated ``devices/`` + ``envelopes/`` tree.
        processed_dir: Directory holding ``windows.parquet`` + manifests.
        dataset_dir: The built dataset dir (split parquets + manifests + pairs).
        windows_parquet: Path to ``processed_dir/windows.parquet``.
        feature_manifest: Path to the dataset's ``feature_manifest.json``.
        preprocess_report: The preprocess report dict.
        seed: The generation seed.
    """

    synthetic_dir: Path
    processed_dir: Path
    dataset_dir: Path
    windows_parquet: Path
    feature_manifest: Path
    preprocess_report: dict[str, Any]
    seed: int


@pytest.fixture(scope="session")
def _pipeline(tmp_path_factory: pytest.TempPathFactory) -> _Pipeline:
    """Build the tiny synthetic dataset once for the whole test session.

    Args:
        tmp_path_factory: pytest's session-scoped temp path factory.

    Returns:
        A :class:`_Pipeline` with all the resolved paths.
    """
    root = tmp_path_factory.mktemp("contextauth_fixture")
    synthetic_dir = root / "synthetic"
    processed_dir = root / "processed"
    datasets_root = root / "datasets"

    # 1) synthetic data (+ envelopes for the loader-roundtrip test).
    generate(
        users=_USERS,
        days=_DAYS,
        sessions_per_day=_SESSIONS_PER_DAY,
        out=synthetic_dir,
        seed=_SEED,
        emit_envelopes=True,
    )

    # 2) preprocess -> windows.parquet + feature_manifest.json.
    report = run_preprocess(
        synthetic_dir,
        processed_dir,
        window_size_sec=_WINDOW_SIZE_SEC,
        stride_sec=_STRIDE_SEC,
        feature_mode=_FEATURE_MODE,
    )

    # 3) leakage-checked leave_session_out dataset (asserts leakage_check True).
    dataset_dir = build_dataset(
        processed_dir / "windows.parquet",
        protocol=_PROTOCOL,
        out_dir=datasets_root,
        feature_mode=_FEATURE_MODE,
        seed=_SEED,
        n_impostor_per_genuine=1,
    )

    return _Pipeline(
        synthetic_dir=synthetic_dir,
        processed_dir=processed_dir,
        dataset_dir=dataset_dir,
        windows_parquet=processed_dir / "windows.parquet",
        feature_manifest=dataset_dir / "feature_manifest.json",
        preprocess_report=report,
        seed=_SEED,
    )


@pytest.fixture(scope="session")
def synthetic_dir(_pipeline: _Pipeline) -> Path:
    """Root of the generated synthetic ``devices/`` + ``envelopes/`` tree."""
    return _pipeline.synthetic_dir


@pytest.fixture(scope="session")
def processed_dir(_pipeline: _Pipeline) -> Path:
    """Directory holding ``windows.parquet`` + preprocessing manifests."""
    return _pipeline.processed_dir


@pytest.fixture(scope="session")
def dataset_dir(_pipeline: _Pipeline) -> Path:
    """The built dataset dir (split parquets + manifests + impostor pairs)."""
    return _pipeline.dataset_dir


@pytest.fixture(scope="session")
def windows_parquet(_pipeline: _Pipeline) -> Path:
    """Path to the preprocessed ``windows.parquet``."""
    return _pipeline.windows_parquet


@pytest.fixture(scope="session")
def feature_manifest(_pipeline: _Pipeline) -> dict[str, Any]:
    """The dataset's parsed ``feature_manifest.json`` (models' input contract)."""
    return json.loads(_pipeline.feature_manifest.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def preprocess_report(_pipeline: _Pipeline) -> dict[str, Any]:
    """The preprocess report dict from ``run_preprocess``."""
    return _pipeline.preprocess_report


@pytest.fixture(scope="session")
def split_manifest(_pipeline: _Pipeline) -> dict[str, Any]:
    """The dataset's parsed ``split_manifest.json`` (§3d, leakage_check block)."""
    return json.loads((_pipeline.dataset_dir / "split_manifest.json").read_text(encoding="utf-8"))
