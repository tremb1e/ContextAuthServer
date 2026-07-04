"""RandomRouter is per-window random + reproducible (SRV-8).

The M9 random-routing baseline must route each window to an independent random
expert subset (not one fixed subset for every window), while staying fully
reproducible under a fixed seed.
"""

from __future__ import annotations

import numpy as np
import torch

from research.models.moe import MoEAuthenticator
from research.models.routing import RandomRouter, build_router


def test_reproducible_same_seed_different_across_seeds() -> None:
    """Same seed+ids -> identical logits; different seed -> different table."""
    ids = torch.arange(32, dtype=torch.long)
    x = torch.zeros(32, 5)
    r1 = RandomRouter(n_experts=7, seed=42)
    r2 = RandomRouter(n_experts=7, seed=42)
    r3 = RandomRouter(n_experts=7, seed=43)
    out1 = r1(x, None, ids)
    out2 = r2(x, None, ids)
    assert torch.allclose(out1, out2)  # two instances, same seed -> identical
    assert not torch.allclose(out1, r3(x, None, ids))  # different seed -> different


def test_per_window_distinct_rows() -> None:
    """Distinct ids yield generally-distinct logit rows (not one constant row)."""
    ids = torch.arange(64, dtype=torch.long)
    x = torch.zeros(64, 5)
    logits = RandomRouter(n_experts=7, seed=42)(x, None, ids)
    # Not all rows identical (the old bug broadcast one fixed row to every window).
    assert torch.unique(logits, dim=0).shape[0] > 1
    # Through the MoE, different windows select different top-k expert sets.
    model = MoEAuthenticator(input_dim=5, n_experts=7, top_k=2, router="random", router_seed=42)
    with torch.no_grad():
        topk = model(torch.randn(64, 5), None, ids)["topk_indices"]
    distinct_sets = {tuple(sorted(row.tolist())) for row in topk}
    assert len(distinct_sets) > 1


def test_order_invariance() -> None:
    """Routing depends on the window id, not its batch position."""
    ids = torch.randint(0, 10000, (48,))
    x = torch.randn(48, 5)
    router = RandomRouter(n_experts=7, seed=7)
    base = router(x, None, ids)
    perm = torch.randperm(48)
    permuted = router(x[perm], None, ids[perm])
    assert torch.allclose(permuted, base[perm])


def test_ids_none_fallback_shape() -> None:
    """ids=None falls back to a positional slot mapping without crashing."""
    out = RandomRouter(n_experts=7, seed=1)(torch.zeros(8, 5), None, None)
    assert out.shape == (8, 7)


def test_expert_selection_roughly_uniform() -> None:
    """With k=1 over many ids each expert is picked ~1/7 of the time."""
    ids = torch.arange(4096, dtype=torch.long)
    model = MoEAuthenticator(input_dim=5, n_experts=7, top_k=1, router="random", router_seed=42)
    with torch.no_grad():
        topk = model(torch.zeros(4096, 5), None, ids)["topk_indices"].flatten()
    counts = np.bincount(topk.numpy(), minlength=7) / topk.numel()
    assert counts.min() > 0.08 and counts.max() < 0.20  # all experts exercised, ~1/7


def test_build_router_random_type() -> None:
    """build_router('random', ...) returns a RandomRouter."""
    assert isinstance(build_router("random", 5, 7, seed=42), RandomRouter)
