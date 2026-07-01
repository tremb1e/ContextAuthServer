"""DenseAuthenticator — MLP encoder baseline (build contract §4).

A plain multilayer-perceptron encoder mapping a window feature vector to an
L2-normalisable embedding, with an auxiliary user-classification head used at
training time. At evaluation the embedding feeds prototype/cosine verification
(the classification head is discarded), matching the one-class auth protocol of
spec §5. ``input_dim`` is supplied by the caller from the dataset feature
manifest — it is never hardcoded.
"""

from __future__ import annotations

from collections.abc import Sequence

import torch
from torch import Tensor, nn


class DenseAuthenticator(nn.Module):
    """Feed-forward encoder + user-classification head.

    Args:
        input_dim: Feature-vector dimension (from ``feature_manifest.json``).
        hidden_dims: Sizes of the hidden layers of the encoder.
        embedding_dim: Dimension of the output embedding.
        n_users: Number of identities for the auxiliary classification head.
        dropout: Dropout probability applied after each hidden activation.
        layer_norm: Whether to apply LayerNorm after each hidden linear layer.
    """

    def __init__(
        self,
        input_dim: int,
        hidden_dims: Sequence[int] = (256, 128),
        embedding_dim: int = 128,
        n_users: int = 2,
        dropout: float = 0.1,
        layer_norm: bool = False,
    ) -> None:
        super().__init__()
        if input_dim <= 0:
            raise ValueError(f"input_dim must be positive, got {input_dim}")
        self.input_dim = int(input_dim)
        self.embedding_dim = int(embedding_dim)
        self.n_users = int(max(1, n_users))

        layers: list[nn.Module] = []
        prev = self.input_dim
        for hidden in hidden_dims:
            layers.append(nn.Linear(prev, int(hidden)))
            if layer_norm:
                layers.append(nn.LayerNorm(int(hidden)))
            layers.append(nn.ReLU())
            if dropout > 0:
                layers.append(nn.Dropout(float(dropout)))
            prev = int(hidden)
        self.encoder = nn.Sequential(*layers)
        self.embed = nn.Linear(prev, self.embedding_dim)
        self.classifier = nn.Linear(self.embedding_dim, self.n_users)

    def forward(self, x: Tensor) -> dict[str, Tensor]:
        """Encode a batch of feature vectors.

        Args:
            x: Float tensor ``[B, input_dim]``.

        Returns:
            Dict with ``embedding`` ``[B, embedding_dim]`` and ``user_logits``
            ``[B, n_users]``.
        """
        hidden = self.encoder(x)
        embedding = self.embed(hidden)
        user_logits = self.classifier(embedding)
        return {"embedding": embedding, "user_logits": user_logits}

    @torch.no_grad()
    def embed_normalized(self, x: Tensor) -> Tensor:
        """Return the L2-normalised embedding for cosine/prototype eval.

        Args:
            x: Float tensor ``[B, input_dim]``.

        Returns:
            Unit-norm embedding tensor ``[B, embedding_dim]``.
        """
        emb = self.forward(x)["embedding"]
        return nn.functional.normalize(emb, dim=-1)

    def param_count(self) -> int:
        """Return the total number of trainable parameters."""
        return int(sum(p.numel() for p in self.parameters() if p.requires_grad))
