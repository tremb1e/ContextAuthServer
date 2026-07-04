"""Router variants for the MoE authenticator (build contract §4, spec §6).

Every router is an ``nn.Module`` exposing ``forward(x, weak_probs, ids) ->
router_logits[B, n_experts]``. The 5 kinds:

* ``learned`` — a small MLP on the feature vector ``x`` (trained end-to-end).
* ``fixed_rule`` — ``log`` of the weak-label probability vector, no gradient
  (the M4/M5 fixed-rule baseline; router is untrained).
* ``random`` — fixed-seed PER-WINDOW random logits (M9); each window draws an
  independent random expert subset from a seed-deterministic slot table, so the
  routing is genuinely random per window yet fully reproducible.
* ``hash`` — hashes the per-window ``ids`` to a one-hot expert (M10).
* ``package_only`` — a learned MLP on the package feature slice only (M3);
  the caller passes ``x`` already sliced to the package columns.

Routers are intentionally small so the whole suite runs fast on CPU.
"""

from __future__ import annotations

import torch
from torch import Tensor, nn

from research import N_SCENARIOS

#: The supported router kinds.
ROUTER_KINDS = ("learned", "fixed_rule", "random", "hash", "package_only")

_LOG_EPS = 1e-8


class LearnedRouter(nn.Module):
    """A small MLP router over the feature vector.

    Args:
        input_dim: Dimension of the router input (full features, or the package
            slice for the ``package_only`` variant).
        n_experts: Number of experts (== ``N_SCENARIOS``).
        hidden: Hidden width of the router MLP.
    """

    def __init__(self, input_dim: int, n_experts: int = N_SCENARIOS, hidden: int = 64) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(int(input_dim), int(hidden)),
            nn.ReLU(),
            nn.Linear(int(hidden), int(n_experts)),
        )

    def forward(self, x: Tensor, weak_probs: Tensor | None = None, ids: Tensor | None = None) -> Tensor:
        """Return learned router logits ``[B, n_experts]``.

        Args:
            x: Feature tensor ``[B, input_dim]``.
            weak_probs: Unused (present for interface parity).
            ids: Unused (present for interface parity).

        Returns:
            Router logits.
        """
        return self.net(x)


class FixedRuleRouter(nn.Module):
    """Router that emits ``log(weak_probs)`` with no learnable parameters.

    This realises the M4/M5 fixed-rule baselines: the routing distribution is
    exactly the weak-label distribution (top-k gating then selects k of them).

    Args:
        n_experts: Number of experts.
    """

    def __init__(self, n_experts: int = N_SCENARIOS) -> None:
        super().__init__()
        self.n_experts = int(n_experts)
        # A buffer so the module has a device/dtype and ``.to()`` works.
        self.register_buffer("_zero", torch.zeros(1), persistent=False)

    def forward(self, x: Tensor, weak_probs: Tensor | None = None, ids: Tensor | None = None) -> Tensor:
        """Return ``log(weak_probs)`` logits ``[B, n_experts]`` (uniform fallback).

        Args:
            x: Feature tensor ``[B, input_dim]`` (only used for batch shape).
            weak_probs: Weak-label probabilities ``[B, n_experts]``; if ``None``
                a uniform distribution is used.
            ids: Unused.

        Returns:
            Detached log-probability logits (no gradient flows to the router).
        """
        batch = x.shape[0]
        if weak_probs is None:
            probs = torch.full((batch, self.n_experts), 1.0 / self.n_experts, device=x.device, dtype=x.dtype)
        else:
            probs = weak_probs.to(device=x.device, dtype=x.dtype)
        return torch.log(probs.clamp_min(_LOG_EPS)).detach()


class RandomRouter(nn.Module):
    """Router emitting PER-WINDOW random logits from a seed-deterministic table.

    Realises M9: each window is routed to a random expert subset that depends on
    its per-window ``ids`` (the stable hash id), so different windows generally
    activate different experts — a genuine random-routing baseline rather than a
    single fixed expert subset. The routing is fully reproducible: a seed-fixed
    ``[n_slots, n_experts]`` table is indexed by ``slot = id % n_slots``, so the
    same ``seed`` + same window id always yields the same logits (order- and
    epoch-independent), and a different ``seed`` yields a different routing.

    The table is a NON-persistent buffer (it is fully reconstructable from
    ``seed``), so checkpoints do not grow and are unaffected by ``n_slots``.

    Args:
        n_experts: Number of experts.
        seed: RNG seed for the random logits table.
        n_slots: Number of distinct slot rows in the table. 8192 slots make
            collisions among a few hundred–thousand windows rare and harmless
            (a collision merely shares one random subset — still random routing).
    """

    def __init__(self, n_experts: int = N_SCENARIOS, seed: int = 42, n_slots: int = 8192) -> None:
        super().__init__()
        self.n_experts = int(n_experts)
        self.n_slots = int(n_slots)
        generator = torch.Generator().manual_seed(int(seed))
        table = torch.randn(self.n_slots, self.n_experts, generator=generator)
        # Non-persistent: the table is fully determined by ``seed``; keeping it
        # out of state_dict leaves checkpoints unchanged in size/content.
        self.register_buffer("logits_table", table, persistent=False)

    def forward(self, x: Tensor, weak_probs: Tensor | None = None, ids: Tensor | None = None) -> Tensor:
        """Return per-window random logits indexed by ``ids`` (hash ids).

        Args:
            x: Feature tensor ``[B, input_dim]`` (only used for batch shape/device).
            weak_probs: Unused.
            ids: Integer per-window hash ids ``[B]``; if ``None`` (only when the
                router is exercised outside the training/eval pipeline, which
                always passes hash ids) a positional ``arange % n_slots`` fallback
                is used so the call still returns per-row-distinct logits.

        Returns:
            Router logits ``[B, n_experts]`` (generally distinct rows per window).
        """
        batch = x.shape[0]
        if ids is None:
            slot = torch.arange(batch, device=x.device, dtype=torch.long) % self.n_slots
        else:
            slot = ids.to(device=x.device, dtype=torch.long) % self.n_slots
        return self.logits_table.to(device=x.device, dtype=x.dtype)[slot]


class HashRouter(nn.Module):
    """Router that hashes per-window integer ids to a one-hot expert.

    Realises M10: each window is deterministically assigned to a single expert
    by ``id % n_experts``; the one-hot is scaled to a large logit so top-1
    gating selects exactly that expert.

    Args:
        n_experts: Number of experts.
        scale: Logit magnitude for the selected expert.
    """

    def __init__(self, n_experts: int = N_SCENARIOS, scale: float = 10.0) -> None:
        super().__init__()
        self.n_experts = int(n_experts)
        self.scale = float(scale)
        self.register_buffer("_zero", torch.zeros(1), persistent=False)

    def forward(self, x: Tensor, weak_probs: Tensor | None = None, ids: Tensor | None = None) -> Tensor:
        """Return one-hot-style logits from hashed ids.

        Args:
            x: Feature tensor ``[B, input_dim]`` (used for batch shape/device).
            weak_probs: Unused.
            ids: Integer tensor ``[B]`` of per-window hash ids; if ``None`` a
                zero id is used for every row.

        Returns:
            Router logits ``[B, n_experts]`` with a large value on the hashed
            expert and zeros elsewhere.
        """
        batch = x.shape[0]
        if ids is None:
            expert = torch.zeros(batch, dtype=torch.long, device=x.device)
        else:
            expert = (ids.to(device=x.device, dtype=torch.long) % self.n_experts)
        logits = torch.zeros(batch, self.n_experts, device=x.device, dtype=x.dtype)
        logits.scatter_(1, expert.unsqueeze(1), self.scale)
        return logits


def build_router(
    kind: str,
    input_dim: int,
    n_experts: int = N_SCENARIOS,
    *,
    seed: int = 42,
    hidden: int = 64,
) -> nn.Module:
    """Construct a router module by kind.

    Args:
        kind: One of :data:`ROUTER_KINDS`.
        input_dim: Router input dimension. For ``learned`` this is the full
            feature dim; for ``package_only`` the caller passes the package-slice
            dim and must feed the router the sliced ``x``.
        n_experts: Number of experts.
        seed: Seed for the ``random`` router's per-window random logits table.
        hidden: Hidden width for learned routers.

    Returns:
        The router ``nn.Module``.

    Raises:
        ValueError: If ``kind`` is unknown.
    """
    if kind == "learned":
        return LearnedRouter(input_dim, n_experts, hidden=hidden)
    if kind == "package_only":
        return LearnedRouter(input_dim, n_experts, hidden=hidden)
    if kind == "fixed_rule":
        return FixedRuleRouter(n_experts)
    if kind == "random":
        return RandomRouter(n_experts, seed=seed)
    if kind == "hash":
        return HashRouter(n_experts)
    raise ValueError(f"unknown router kind: {kind!r} (valid: {ROUTER_KINDS})")
