"""MoEAuthenticator — 7-expert mixture with top-k sparse gating (contract §4).

Seven MLP-encoder experts (one per scenario I0..I6) each map the window feature
vector to an embedding. A router (:mod:`research.models.routing`) produces expert
logits; **top-k sparse gating** keeps the ``top_k`` highest-weight experts,
renormalises their softmax weights to sum to 1, and zeroes the rest. The fused
embedding is the gate-weighted sum of expert embeddings; a classification head
on the fused embedding provides the auxiliary identity loss. ``k`` ∈ {1..7} are
all valid; ``k == n_experts`` is the dense-all mixture.

``forward`` returns (contract §4): ``embedding``, ``user_logits``,
``router_logits`` ``[B,7]``, ``router_probs`` ``[B,7]``, ``topk_indices``
``[B,k]``, ``gate_weights`` ``[B,7]`` (renormalised over the active experts,
zeros elsewhere), and ``active_experts`` (a float, == ``top_k``).
"""

from __future__ import annotations

from collections.abc import Sequence

import torch
from torch import Tensor, nn

from research import N_SCENARIOS
from research.models.routing import build_router


class _Expert(nn.Module):
    """A single MLP expert mapping features to an embedding.

    Args:
        input_dim: Feature-vector dimension.
        hidden_dims: Hidden layer widths.
        embedding_dim: Output embedding dimension.
        dropout: Dropout probability after each hidden activation.
    """

    def __init__(
        self,
        input_dim: int,
        hidden_dims: Sequence[int],
        embedding_dim: int,
        dropout: float,
    ) -> None:
        super().__init__()
        layers: list[nn.Module] = []
        prev = int(input_dim)
        for hidden in hidden_dims:
            layers.append(nn.Linear(prev, int(hidden)))
            layers.append(nn.ReLU())
            if dropout > 0:
                layers.append(nn.Dropout(float(dropout)))
            prev = int(hidden)
        layers.append(nn.Linear(prev, int(embedding_dim)))
        self.net = nn.Sequential(*layers)

    def forward(self, x: Tensor) -> Tensor:
        """Return the expert embedding ``[B, embedding_dim]`` for input ``x``."""
        return self.net(x)


class MoEAuthenticator(nn.Module):
    """Mixture-of-experts authenticator with top-k sparse gating.

    Args:
        input_dim: Feature-vector dimension (from ``feature_manifest.json``).
        n_experts: Number of experts (== ``N_SCENARIOS`` == 7).
        top_k: Number of active experts per window (1..``n_experts``).
        expert_hidden: Hidden widths of each expert MLP.
        embedding_dim: Fused embedding dimension.
        n_users: Identities for the auxiliary classification head.
        router: Router kind (see :data:`research.models.routing.ROUTER_KINDS`).
        dropout: Dropout probability inside each expert.
        package_indices: For the ``package_only`` router, the column indices of
            the package features within ``x`` (the router sees only these).
        router_seed: Seed for the ``random`` router.
    """

    def __init__(
        self,
        input_dim: int,
        n_experts: int = N_SCENARIOS,
        top_k: int = 2,
        expert_hidden: Sequence[int] = (128,),
        embedding_dim: int = 128,
        n_users: int = 2,
        router: str = "learned",
        dropout: float = 0.1,
        package_indices: Sequence[int] | None = None,
        router_seed: int = 42,
    ) -> None:
        super().__init__()
        if input_dim <= 0:
            raise ValueError(f"input_dim must be positive, got {input_dim}")
        self.input_dim = int(input_dim)
        self.n_experts = int(n_experts)
        if not 1 <= int(top_k) <= self.n_experts:
            raise ValueError(f"top_k must be in [1, {self.n_experts}], got {top_k}")
        self.top_k = int(top_k)
        self.embedding_dim = int(embedding_dim)
        self.n_users = int(max(1, n_users))
        self.router_kind = str(router)

        self.experts = nn.ModuleList(
            _Expert(self.input_dim, expert_hidden, self.embedding_dim, dropout) for _ in range(self.n_experts)
        )

        # The package_only router sees only the package feature slice.
        if router == "package_only":
            if not package_indices:
                raise ValueError("router='package_only' requires non-empty package_indices")
            self.register_buffer(
                "package_indices", torch.tensor(list(package_indices), dtype=torch.long), persistent=True
            )
            router_input_dim = len(package_indices)
        else:
            self.package_indices = None  # type: ignore[assignment]
            router_input_dim = self.input_dim
        self.router = build_router(router, router_input_dim, self.n_experts, seed=router_seed)

        self.classifier = nn.Linear(self.embedding_dim, self.n_users)

    def _router_input(self, x: Tensor) -> Tensor:
        """Return the tensor fed to the router (full ``x`` or package slice)."""
        if self.router_kind == "package_only" and self.package_indices is not None:
            return x.index_select(1, self.package_indices.to(x.device))
        return x

    def _topk_gate(self, router_logits: Tensor) -> tuple[Tensor, Tensor, Tensor]:
        """Compute dense router probs and sparse renormalised gate weights.

        Args:
            router_logits: Raw router logits ``[B, n_experts]``.

        Returns:
            Tuple ``(router_probs, gate_weights, topk_indices)`` where
            ``router_probs`` is the dense softmax ``[B, n_experts]``,
            ``gate_weights`` ``[B, n_experts]`` is zero everywhere except the
            top-k experts whose softmax weights are renormalised to sum to 1,
            and ``topk_indices`` is ``[B, top_k]``.
        """
        router_probs = torch.softmax(router_logits, dim=-1)
        topk_vals, topk_indices = torch.topk(router_probs, self.top_k, dim=-1)
        # Renormalise the kept weights so each row's active weights sum to 1.
        denom = topk_vals.sum(dim=-1, keepdim=True).clamp_min(1e-12)
        topk_weights = topk_vals / denom
        gate_weights = torch.zeros_like(router_probs)
        gate_weights.scatter_(1, topk_indices, topk_weights)
        return router_probs, gate_weights, topk_indices

    def forward(
        self,
        x: Tensor,
        weak_probs: Tensor | None = None,
        ids: Tensor | None = None,
    ) -> dict[str, Tensor]:
        """Run the mixture and return the full output dict (contract §4).

        Args:
            x: Feature tensor ``[B, input_dim]``.
            weak_probs: Optional weak-label probabilities ``[B, n_experts]`` for
                the ``fixed_rule`` router / KL supervision.
            ids: Optional per-window integer ids ``[B]`` for the ``hash`` router.

        Returns:
            Dict with ``embedding`` ``[B, emb]``, ``user_logits`` ``[B, n_users]``,
            ``router_logits`` / ``router_probs`` / ``gate_weights`` ``[B, 7]``,
            ``topk_indices`` ``[B, k]`` and ``active_experts`` (scalar float
            tensor == ``top_k``).
        """
        router_logits = self.router(self._router_input(x), weak_probs, ids)
        router_probs, gate_weights, topk_indices = self._topk_gate(router_logits)

        # Stack expert embeddings: [B, n_experts, emb].
        expert_embeddings = torch.stack([expert(x) for expert in self.experts], dim=1)
        # Weighted fusion by the sparse gate: [B, emb].
        fused = torch.einsum("be,bed->bd", gate_weights, expert_embeddings)
        user_logits = self.classifier(fused)

        return {
            "embedding": fused,
            "user_logits": user_logits,
            "router_logits": router_logits,
            "router_probs": router_probs,
            "gate_weights": gate_weights,
            "topk_indices": topk_indices,
            "active_experts": torch.tensor(float(self.top_k), device=x.device),
        }

    @torch.no_grad()
    def embed_normalized(self, x: Tensor, weak_probs: Tensor | None = None, ids: Tensor | None = None) -> Tensor:
        """Return the L2-normalised fused embedding for cosine/prototype eval.

        Args:
            x: Feature tensor ``[B, input_dim]``.
            weak_probs: Optional weak-label probabilities for routing.
            ids: Optional per-window ids for the hash router.

        Returns:
            Unit-norm fused embedding ``[B, embedding_dim]``.
        """
        emb = self.forward(x, weak_probs, ids)["embedding"]
        return nn.functional.normalize(emb, dim=-1)

    def param_count(self) -> int:
        """Return the total number of trainable parameters."""
        return int(sum(p.numel() for p in self.parameters() if p.requires_grad))

    def active_param_count(self) -> int:
        """Approximate parameters exercised per window under top-k gating.

        Counts the router + classifier + ``top_k`` of the ``n_experts`` experts
        (experts are identically sized), a cost proxy for the top-k sweep.

        Returns:
            The approximate active parameter count.
        """
        expert_params = sum(p.numel() for p in self.experts[0].parameters())
        router_params = sum(p.numel() for p in self.router.parameters())
        classifier_params = sum(p.numel() for p in self.classifier.parameters())
        return int(router_params + classifier_params + self.top_k * expert_params)
