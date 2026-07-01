"""Shared split-loading helpers for the trainer and evaluator (S4 internal).

Turns the ``{train,val,test}.parquet`` split files + ``feature_manifest.json``
produced by :mod:`research.datasets.builders` into torch tensors and light
metadata frames. ``input_dim`` always comes from the feature manifest (never
hardcoded), and the feature block is re-projected to exactly the manifest's
``feature_columns`` (missing columns filled ``0.0``) so a model built from the
manifest always lines up with the data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch import Tensor

from research import N_SCENARIOS, SCENARIO_INDEX
from research.datasets.splits import SESSION_COL, USER_COL, WINDOW_COL


def load_feature_manifest(data_dir: str | Path) -> dict:
    """Load a dataset's ``feature_manifest.json``.

    Args:
        data_dir: A dataset directory (containing the manifest) or the manifest
            file path itself.

    Returns:
        The parsed feature manifest.

    Raises:
        FileNotFoundError: If the manifest cannot be located.
    """
    path = Path(data_dir)
    if path.is_dir():
        path = path / "feature_manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"feature_manifest.json not found under {data_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def _decode_probs(raw: object) -> list[float]:
    """Decode a ``weak_label_probs_json`` cell into a length-7 float list."""
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
            if isinstance(value, list) and len(value) == N_SCENARIOS:
                return [float(v) for v in value]
        except (ValueError, TypeError):
            pass
    return [1.0 / N_SCENARIOS] * N_SCENARIOS


@dataclass
class SplitTensors:
    """Feature tensors + aligned metadata for one split.

    Attributes:
        features: Float feature tensor ``[N, input_dim]``.
        user_labels: Integer identity labels ``[N]`` (dense-coded per dataset).
        weak_probs: Weak-label probability tensor ``[N, 7]``.
        confidence: Per-window weak-label confidence ``[N]``.
        session_ids: Integer session ids ``[N]`` (dense-coded).
        hash_ids: Integer per-window hash ids ``[N]`` for the hash router.
        meta: The aligned metadata frame (string ids, scene, etc.).
    """

    features: Tensor
    user_labels: Tensor
    weak_probs: Tensor
    confidence: Tensor
    session_ids: Tensor
    hash_ids: Tensor
    meta: pd.DataFrame


class DatasetBundle:
    """Loads a dataset directory into per-split tensors with shared encoders.

    User and session id encoders are fit on the UNION of the three splits so the
    integer codes are consistent across train/val/test (the classification head
    covers every identity seen anywhere; unseen-at-train users still get a valid
    code for prototype/cosine eval).

    Args:
        data_dir: The dataset directory (with the split parquets + manifest).
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.manifest = load_feature_manifest(self.data_dir)
        self.feature_columns: list[str] = list(self.manifest["feature_columns"])
        self.input_dim: int = int(self.manifest["input_dim"])
        self.package_columns: list[str] = list(self.manifest.get("package_columns", []))

        self._frames: dict[str, pd.DataFrame] = {}
        for split in ("train", "val", "test"):
            path = self.data_dir / f"{split}.parquet"
            self._frames[split] = pd.read_parquet(path).reset_index(drop=True) if path.exists() else pd.DataFrame()

        union = pd.concat([f for f in self._frames.values() if not f.empty], ignore_index=True)
        self._users: list[str] = sorted(union[USER_COL].astype(str).unique()) if not union.empty else []
        self._user_code = {u: i for i, u in enumerate(self._users)}
        self._sessions: list[str] = sorted(union[SESSION_COL].astype(str).unique()) if not union.empty else []
        self._session_code = {s: i for i, s in enumerate(self._sessions)}

    @property
    def n_users(self) -> int:
        """Number of distinct identities across all splits (>= 1)."""
        return max(1, len(self._users))

    def package_indices(self) -> list[int]:
        """Column indices of the package features within the feature vector.

        Returns:
            The indices of ``package_columns`` in ``feature_columns`` (empty when
            the mode has no package features).
        """
        return [self.feature_columns.index(c) for c in self.package_columns if c in self.feature_columns]

    def raw_frame(self, split: str) -> pd.DataFrame:
        """Return the raw split DataFrame (possibly empty).

        Args:
            split: One of ``train`` / ``val`` / ``test``.

        Returns:
            The split frame.
        """
        return self._frames.get(split, pd.DataFrame())

    def tensors(self, split: str) -> SplitTensors:
        """Materialise a split as :class:`SplitTensors`.

        Args:
            split: One of ``train`` / ``val`` / ``test``.

        Returns:
            The split's tensors + aligned metadata (empty tensors if the split
            frame is empty).
        """
        frame = self._frames.get(split, pd.DataFrame())
        if frame.empty:
            empty_f = torch.zeros((0, self.input_dim), dtype=torch.float32)
            empty_i = torch.zeros((0,), dtype=torch.long)
            return SplitTensors(
                features=empty_f,
                user_labels=empty_i,
                weak_probs=torch.zeros((0, N_SCENARIOS), dtype=torch.float32),
                confidence=torch.zeros((0,), dtype=torch.float32),
                session_ids=empty_i,
                hash_ids=empty_i,
                meta=pd.DataFrame(),
            )

        feature_data = {
            col: (frame[col].astype(float).to_numpy() if col in frame.columns else np.zeros(len(frame)))
            for col in self.feature_columns
        }
        feat_np = np.column_stack([feature_data[c] for c in self.feature_columns]).astype(np.float32)
        features = torch.from_numpy(np.nan_to_num(feat_np, nan=0.0, posinf=0.0, neginf=0.0))

        users = frame[USER_COL].astype(str)
        user_labels = torch.tensor([self._user_code.get(u, 0) for u in users], dtype=torch.long)
        sessions = frame[SESSION_COL].astype(str)
        session_ids = torch.tensor([self._session_code.get(s, 0) for s in sessions], dtype=torch.long)

        probs_np = np.array([_decode_probs(v) for v in frame.get("weak_label_probs_json", [None] * len(frame))], dtype=np.float32)
        weak_probs = torch.from_numpy(probs_np)
        confidence = torch.tensor(
            frame.get("weak_label_confidence", pd.Series([0.0] * len(frame))).astype(float).to_numpy(),
            dtype=torch.float32,
        )
        # Deterministic per-window hash id (for the hash router / M10).
        hash_ids = torch.tensor(
            [abs(hash(str(w))) % (2**31) for w in frame.get(WINDOW_COL, pd.Series(range(len(frame))))],
            dtype=torch.long,
        )

        meta = frame[[c for c in (WINDOW_COL, USER_COL, SESSION_COL, "day_id", "weak_label_top1") if c in frame.columns]].copy()
        meta = meta.reset_index(drop=True)
        return SplitTensors(
            features=features,
            user_labels=user_labels,
            weak_probs=weak_probs,
            confidence=confidence,
            session_ids=session_ids,
            hash_ids=hash_ids,
            meta=meta,
        )


def scene_to_index(scene: str) -> int:
    """Map a scenario id (C0..C6) to its ordinal index (unknown -> 0).

    Args:
        scene: A scenario id string.

    Returns:
        The ordinal index in ``[0, 6]``.
    """
    return int(SCENARIO_INDEX.get(str(scene), 0))
