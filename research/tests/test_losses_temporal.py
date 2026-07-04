"""Temporal-smoothness penalises TRUE adjacent windows only (SRV-7).

Covers :func:`temporal_smoothness_pairs`, the ``succ_idx`` construction in the
dataset bundle, and the total_loss backward-compat guard (no ``succ_positions``
-> identical to the legacy adjacent-row term).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import torch

from research.models.losses import temporal_smoothness, temporal_smoothness_pairs, total_loss


def test_temporal_smoothness_pairs_values() -> None:
    """Only the declared (j, succ) pairs are penalised; -1 rows are ignored."""
    probs = torch.tensor([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5], [0.5, 0.5]])
    # pairs: (0->1) dist^2 = 2, (2->3) dist^2 = 0; rows 1,3 have no successor.
    succ = torch.tensor([1, -1, 3, -1])
    val = temporal_smoothness_pairs(probs, succ)
    assert abs(float(val) - 1.0) < 1e-6  # mean of {2, 0}
    # No valid pair -> differentiable zero.
    assert float(temporal_smoothness_pairs(probs, torch.tensor([-1, -1, -1, -1]))) == 0.0
    # Backward works.
    p = probs.clone().requires_grad_(True)
    temporal_smoothness_pairs(p, succ).backward()
    assert p.grad is not None and torch.isfinite(p.grad).all()


def test_succ_idx_construction() -> None:
    """succ_idx links same-session consecutive rows only (session boundary -> -1)."""
    from research.experiments._data import DatasetBundle

    # Build a tiny in-memory bundle by monkeypatching the frame loader is overkill;
    # instead reconstruct the succ logic used in DatasetBundle.tensors on a frame
    # of sessions [A,A,A,B,B] with increasing start times.
    session_ids = torch.tensor([0, 0, 0, 1, 1])
    start_ns = torch.tensor([0.0, 1.0, 2.0, 0.0, 1.0], dtype=torch.float64)
    n = 5
    succ = torch.full((n,), -1, dtype=torch.long)
    same = session_ids[1:] == session_ids[:-1]
    same = same & (start_ns[1:] >= start_ns[:-1])
    succ[:-1] = torch.where(same, torch.arange(1, n), torch.full((n - 1,), -1))
    assert succ.tolist() == [1, 2, -1, 4, -1]
    assert isinstance(DatasetBundle, type)  # module importable


def test_total_loss_backward_compat_without_succ() -> None:
    """total_loss without succ_positions == legacy adjacent-row smoothness."""
    torch.manual_seed(0)
    router_probs = torch.softmax(torch.randn(6, 7), dim=-1)
    outputs = {
        "embedding": torch.randn(6, 8),
        "user_logits": torch.randn(6, 3),
        "router_logits": torch.randn(6, 7),
        "router_probs": router_probs,
    }
    session_ids = torch.tensor([0, 0, 1, 1, 1, 2])
    batch = {
        "user_labels": torch.tensor([0, 1, 2, 0, 1, 2]),
        "weak_probs": torch.softmax(torch.randn(6, 7), dim=-1),
        "confidence": torch.ones(6),
        "session_ids": session_ids,
    }
    cfg = {"loss": {"lambda_scene": 1.0, "lambda_balance": 0.005, "lambda_smooth": 0.1, "auth_kind": "ce_proto"}}
    _, parts = total_loss(outputs, batch, cfg)
    # The smooth part must equal the legacy adjacent-row term exactly.
    legacy = float(temporal_smoothness(router_probs, session_ids))
    assert abs(parts["smooth"] - legacy) < 1e-9


def test_true_adjacent_dataset_bundle(tmp_path) -> None:
    """Bundle succ_idx over a real split parquet links only same-session neighbours."""
    from research.experiments._data import DatasetBundle

    rows = []
    for sess in ("A", "B"):
        for w in range(3):
            rows.append({
                "window_id": f"dev:{sess}:{w}", "user_id": "u0", "session_id": sess, "day_id": "d0",
                "start_elapsed_ns": float(w), "weak_label_top1": "I0",
                "weak_label_probs_json": "[1,0,0,0,0,0,0]", "weak_label_confidence": 0.9, "feat0": float(w),
            })
    df = pd.DataFrame(rows)
    ds = tmp_path / "ds"
    ds.mkdir()
    for split in ("train", "val", "test"):
        df.to_parquet(ds / f"{split}.parquet", index=False)
    (ds / "feature_manifest.json").write_text('{"feature_columns": ["feat0"], "input_dim": 1, "package_columns": []}', encoding="utf-8")
    bundle = DatasetBundle(ds)
    succ = bundle.tensors("train").succ_idx.tolist()
    # Session A rows 0,1,2 -> [1,2,-1]; session B rows 3,4,5 -> [4,5,-1].
    assert succ == [1, 2, -1, 4, 5, -1]
