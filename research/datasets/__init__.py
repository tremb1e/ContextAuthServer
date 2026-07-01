"""Dataset assembly for the ContextAuth research layer.

This subpackage turns the preprocessed ``windows.parquet`` into leakage-free
train/val/test splits plus matched-impostor pairs and the on-disk manifests the
models and experiment runner consume (build contract §11 S3, §3d).

Public surface:

* :func:`research.datasets.splits.make_split` and the four protocol helpers
  (:func:`leave_session_out`, :func:`leave_day_out`, :func:`leave_app_out`,
  :func:`combined_day_app`).
* :func:`research.datasets.impostors.sample_matched_impostors`.
* :func:`research.datasets.builders.build_dataset`.
"""

from __future__ import annotations

from research.datasets.builders import build_dataset
from research.datasets.impostors import sample_matched_impostors
from research.datasets.splits import (
    PROTOCOLS,
    SplitResult,
    combined_day_app,
    leave_app_out,
    leave_day_out,
    leave_session_out,
    make_split,
)

__all__ = [
    "PROTOCOLS",
    "SplitResult",
    "build_dataset",
    "combined_day_app",
    "leave_app_out",
    "leave_day_out",
    "leave_session_out",
    "make_split",
    "sample_matched_impostors",
]
