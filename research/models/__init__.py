"""Neural models for the ContextAuth research layer (build contract §4).

* :class:`research.models.dense.DenseAuthenticator` — MLP encoder baseline.
* :class:`research.models.moe.MoEAuthenticator` — 7-expert mixture with top-k
  sparse gating (k ∈ 1..7; k=7 == dense-all).
* :func:`research.models.routing.build_router` — the 5 router variants.
* :mod:`research.models.losses` — auth / KL-to-weak / load-balance /
  temporal-smoothness / total loss.

All modules run on CPU with full type hints and read ``input_dim`` from the
dataset feature manifest (never hardcoded).
"""

from __future__ import annotations

from research.models.dense import DenseAuthenticator
from research.models.losses import (
    auth_loss,
    kl_weak,
    load_balance,
    temporal_smoothness,
    total_loss,
)
from research.models.moe import MoEAuthenticator
from research.models.routing import ROUTER_KINDS, build_router

__all__ = [
    "DenseAuthenticator",
    "MoEAuthenticator",
    "ROUTER_KINDS",
    "auth_loss",
    "build_router",
    "kl_weak",
    "load_balance",
    "temporal_smoothness",
    "total_loss",
]
