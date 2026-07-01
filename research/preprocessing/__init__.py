"""Preprocessing: loaders, alignment, sessionization, windowing, features."""

from __future__ import annotations

from .align import (
    align_batches,
    attach_base_elapsed_nanos,
    channel_presence,
    detect_clock_jumps,
    index_batches,
)
from .feature_extractors import (
    build_feature_columns,
    build_feature_manifest,
    build_package_columns,
    extract_window_features,
)
from .loaders import (
    REQUIRED_BATCH_KEYS,
    iter_windows,
    load_batches,
    load_envelope,
)
from .quality import QUALITY_FLAG_VOCAB, quality_flags
from .sessionize import session_summary, sessionize
from .windowing import make_windows

__all__ = [
    "REQUIRED_BATCH_KEYS",
    "load_batches",
    "load_envelope",
    "iter_windows",
    "align_batches",
    "attach_base_elapsed_nanos",
    "channel_presence",
    "detect_clock_jumps",
    "index_batches",
    "sessionize",
    "session_summary",
    "make_windows",
    "build_feature_columns",
    "build_package_columns",
    "build_feature_manifest",
    "extract_window_features",
    "quality_flags",
    "QUALITY_FLAG_VOCAB",
]
