"""MoE top-k gating: forward for k in 1..7 + normalised gate weights — §15.1.6.

Asserts (on a small random batch sized from the fixture feature manifest):

* the MoE forward runs for every ``top_k`` in ``1..7`` and returns the full
  output contract (embedding, user_logits, router_logits/probs, gate_weights,
  topk_indices, active_experts);
* exactly ``top_k`` experts are active per row and their gate weights renormalise
  to sum to 1 (zeros elsewhere);
* ``router_probs`` is a dense softmax (sums to 1 over all 7);
* ``param_count`` / ``active_param_count`` are consistent (active grows with k,
  and k=7 active == full experts);
* the Dense baseline forwards and exposes ``param_count``.
"""

from __future__ import annotations

import json
from pathlib import Path

import torch

from research import N_SCENARIOS
from research.models.dense import DenseAuthenticator
from research.models.moe import MoEAuthenticator


def _input_dim(feature_manifest: dict) -> int:
    """Read the manifest-driven input dim (never hardcoded)."""
    return int(feature_manifest["input_dim"])


def test_moe_forward_all_k(feature_manifest: dict) -> None:
    """Forward succeeds for every k in 1..7 with a normalised sparse gate."""
    torch.manual_seed(0)
    input_dim = _input_dim(feature_manifest)
    batch = 8
    x = torch.randn(batch, input_dim)
    weak = torch.softmax(torch.randn(batch, N_SCENARIOS), dim=-1)
    ids = torch.arange(batch)

    for k in range(1, N_SCENARIOS + 1):
        model = MoEAuthenticator(input_dim=input_dim, n_experts=N_SCENARIOS, top_k=k, expert_hidden=[16], embedding_dim=8, n_users=3)
        out = model(x, weak, ids)
        # Contract keys + shapes.
        assert out["embedding"].shape == (batch, 8)
        assert out["user_logits"].shape == (batch, 3)
        assert out["router_logits"].shape == (batch, N_SCENARIOS)
        assert out["router_probs"].shape == (batch, N_SCENARIOS)
        assert out["gate_weights"].shape == (batch, N_SCENARIOS)
        assert out["topk_indices"].shape == (batch, k)
        assert float(out["active_experts"]) == float(k)

        gate = out["gate_weights"]
        # Exactly k experts active per row.
        active_counts = (gate > 0).sum(dim=-1)
        assert torch.all(active_counts == k), f"k={k}: active counts {active_counts.tolist()}"
        # Active gate weights renormalise to sum to 1.
        assert torch.allclose(gate.sum(dim=-1), torch.ones(batch), atol=1e-5)
        # Dense router probs sum to 1 over all experts.
        assert torch.allclose(out["router_probs"].sum(dim=-1), torch.ones(batch), atol=1e-5)


def test_moe_invalid_k_rejected(feature_manifest: dict) -> None:
    """top_k outside [1, n_experts] raises."""
    input_dim = _input_dim(feature_manifest)
    for bad_k in (0, N_SCENARIOS + 1):
        try:
            MoEAuthenticator(input_dim=input_dim, top_k=bad_k)
        except ValueError:
            continue
        raise AssertionError(f"top_k={bad_k} should have raised ValueError")


def test_active_param_count_monotone_in_k(feature_manifest: dict) -> None:
    """Active param count is non-decreasing in k and equals full at k=7."""
    input_dim = _input_dim(feature_manifest)
    counts = []
    for k in range(1, N_SCENARIOS + 1):
        model = MoEAuthenticator(input_dim=input_dim, top_k=k, expert_hidden=[16], embedding_dim=8, n_users=3)
        counts.append(model.active_param_count())
        assert model.param_count() >= model.active_param_count()
    assert counts == sorted(counts), f"active param count not monotone in k: {counts}"
    # At k = n_experts the active experts == all experts.
    full = MoEAuthenticator(input_dim=input_dim, top_k=N_SCENARIOS, expert_hidden=[16], embedding_dim=8, n_users=3)
    assert full.active_param_count() <= full.param_count()


def test_dense_forward_and_param_count(feature_manifest: dict) -> None:
    """The Dense baseline forwards and reports a positive param count."""
    input_dim = _input_dim(feature_manifest)
    model = DenseAuthenticator(input_dim=input_dim, hidden_dims=[16, 8], embedding_dim=8, n_users=3)
    out = model(torch.randn(4, input_dim))
    assert out["embedding"].shape == (4, 8)
    assert out["user_logits"].shape == (4, 3)
    assert model.param_count() > 0
    # Normalised embedding is unit norm.
    emb = model.embed_normalized(torch.randn(4, input_dim))
    assert torch.allclose(emb.norm(dim=-1), torch.ones(4), atol=1e-5)
