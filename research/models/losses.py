"""Loss terms for the MoE authenticator (build contract §4, spec §6.3).

* :func:`auth_loss` — identity loss. ``ce_proto`` = user cross-entropy on the
  classification head plus a prototype-cosine pull (embedding toward its class
  mean); ``triplet`` = batch-hard triplet margin loss on embeddings.
* :func:`kl_weak` — KL(router ‖ weak-label) supervision, weighted per sample by
  the weak-label confidence (low-confidence windows contribute little).
* :func:`load_balance` — Shwartz-Ziv/Switch-style load-balance penalty pushing
  the mean router distribution toward uniform.
* :func:`temporal_smoothness` — penalises router-distribution jumps between
  windows sharing a session (adjacent windows should route similarly).
* :func:`total_loss` — the weighted sum, returning ``(loss, parts)``.

All functions are pure ``Tensor -> Tensor`` and safe on empty / degenerate
batches (they return a differentiable zero rather than a NaN).
"""

from __future__ import annotations

from typing import Any

import torch
from torch import Tensor
from torch.nn import functional as F

from research import N_SCENARIOS

_EPS = 1e-8


def _zero_like(ref: Tensor) -> Tensor:
    """Return a differentiable scalar zero on ``ref``'s device/dtype."""
    return torch.zeros((), device=ref.device, dtype=ref.dtype)


def _prototype_cosine_loss(embeddings: Tensor, user_labels: Tensor) -> Tensor:
    """Mean ``1 - cos`` between each embedding and its class prototype.

    The per-class prototype is the mean embedding of that class within the
    batch. Classes with a single sample contribute their own mean (loss ~0),
    which is harmless.

    Args:
        embeddings: ``[B, emb]`` embeddings.
        user_labels: ``[B]`` integer identity labels.

    Returns:
        A scalar loss.
    """
    normed = F.normalize(embeddings, dim=-1)
    loss = _zero_like(embeddings)
    count = 0
    for label in torch.unique(user_labels):
        mask = user_labels == label
        if int(mask.sum()) == 0:
            continue
        proto = F.normalize(normed[mask].mean(dim=0, keepdim=True), dim=-1)
        cos = (normed[mask] * proto).sum(dim=-1)
        loss = loss + (1.0 - cos).mean()
        count += 1
    return loss / max(1, count)


def _batch_hard_triplet(embeddings: Tensor, user_labels: Tensor, margin: float) -> Tensor:
    """Batch-hard triplet loss on L2-normalised embeddings.

    For each anchor: hardest positive (farthest same-class) and hardest negative
    (closest other-class) by Euclidean distance; loss ``relu(d_pos - d_neg +
    margin)`` averaged over valid anchors.

    Args:
        embeddings: ``[B, emb]`` embeddings.
        user_labels: ``[B]`` integer identity labels.
        margin: Triplet margin.

    Returns:
        A scalar loss (zero when no anchor has both a positive and a negative).
    """
    normed = F.normalize(embeddings, dim=-1)
    dist = torch.cdist(normed, normed, p=2)  # [B, B]
    labels = user_labels.view(-1, 1)
    same = labels == labels.t()
    diff = ~same
    eye = torch.eye(dist.shape[0], dtype=torch.bool, device=dist.device)
    pos_mask = same & ~eye

    losses: list[Tensor] = []
    for i in range(dist.shape[0]):
        if not bool(pos_mask[i].any()) or not bool(diff[i].any()):
            continue
        hardest_pos = dist[i][pos_mask[i]].max()
        hardest_neg = dist[i][diff[i]].min()
        losses.append(F.relu(hardest_pos - hardest_neg + margin))
    if not losses:
        return _zero_like(embeddings)
    return torch.stack(losses).mean()


def auth_loss(
    embeddings: Tensor,
    user_labels: Tensor,
    *,
    user_logits: Tensor | None = None,
    kind: str = "ce_proto",
    margin: float = 0.3,
    proto_weight: float = 0.5,
) -> Tensor:
    """Identity/authentication loss.

    Args:
        embeddings: ``[B, emb]`` embeddings.
        user_labels: ``[B]`` integer identity labels.
        user_logits: ``[B, n_users]`` classification logits (required for
            ``ce_proto``).
        kind: ``"ce_proto"`` (cross-entropy + prototype cosine) or ``"triplet"``.
        margin: Triplet margin (``triplet`` only).
        proto_weight: Weight of the prototype-cosine term (``ce_proto`` only).

    Returns:
        A scalar loss.

    Raises:
        ValueError: If ``kind`` is unknown, or ``ce_proto`` without logits.
    """
    if embeddings.shape[0] == 0:
        return _zero_like(embeddings)
    if kind == "ce_proto":
        if user_logits is None:
            raise ValueError("auth_loss(kind='ce_proto') requires user_logits")
        ce = F.cross_entropy(user_logits, user_labels)
        proto = _prototype_cosine_loss(embeddings, user_labels)
        return ce + proto_weight * proto
    if kind == "triplet":
        return _batch_hard_triplet(embeddings, user_labels, margin)
    raise ValueError(f"unknown auth_loss kind: {kind!r}")


def kl_weak(router_logprobs: Tensor, weak_probs: Tensor, confidence: Tensor) -> Tensor:
    """Confidence-weighted KL(weak-label ‖ router) supervision.

    Args:
        router_logprobs: ``[B, n_experts]`` log-probabilities from the router
            (``log_softmax`` of the router logits).
        weak_probs: ``[B, n_experts]`` weak-label probability targets.
        confidence: ``[B]`` per-window weak-label confidence in ``[0, 1]``; each
            sample's KL is scaled by its confidence so low-confidence windows are
            (softly) skipped as the spec requires.

    Returns:
        A scalar loss (confidence-weighted mean of per-sample KL divergence).
    """
    if router_logprobs.shape[0] == 0:
        return _zero_like(router_logprobs)
    target = weak_probs.clamp_min(_EPS)
    # KL(target || router) = sum target * (log target - logq), per sample.
    per_sample = (target * (target.log() - router_logprobs)).sum(dim=-1)
    weight = confidence.clamp(0.0, 1.0)
    denom = weight.sum().clamp_min(_EPS)
    return (per_sample * weight).sum() / denom


def load_balance(router_probs: Tensor) -> Tensor:
    """Load-balance penalty pushing mean expert usage toward uniform.

    Uses the squared L2 distance between the batch-mean router distribution and
    the uniform distribution, scaled by ``n_experts`` so the term is O(1). Zero
    is achieved exactly at uniform usage.

    Args:
        router_probs: ``[B, n_experts]`` dense router probabilities.

    Returns:
        A scalar loss.
    """
    if router_probs.shape[0] == 0:
        return _zero_like(router_probs)
    n_experts = router_probs.shape[-1]
    mean_usage = router_probs.mean(dim=0)
    uniform = torch.full_like(mean_usage, 1.0 / n_experts)
    return float(n_experts) * ((mean_usage - uniform) ** 2).sum()


def temporal_smoothness(router_probs: Tensor, session_ids: Tensor) -> Tensor:
    """Penalise router-distribution jumps between windows of the same session.

    For every pair of *adjacent* rows in the batch that share a session id, adds
    the squared L2 distance between their router distributions. Adjacent windows
    (which overlap heavily) should route to similar experts.

    Args:
        router_probs: ``[B, n_experts]`` dense router probabilities.
        session_ids: ``[B]`` integer session ids aligned with ``router_probs``
            (windows of one session should be contiguous / grouped for this to
            capture temporal adjacency; grouping is arbitrary but consistent).

    Returns:
        A scalar loss (zero when no adjacent same-session pair exists).
    """
    if router_probs.shape[0] < 2:
        return _zero_like(router_probs)
    same = session_ids[1:] == session_ids[:-1]
    if not bool(same.any()):
        return _zero_like(router_probs)
    diff = router_probs[1:] - router_probs[:-1]
    sq = (diff**2).sum(dim=-1)
    mask = same.to(sq.dtype)
    denom = mask.sum().clamp_min(_EPS)
    return (sq * mask).sum() / denom


def total_loss(outputs: dict[str, Tensor], batch: dict[str, Tensor], cfg: dict[str, Any]) -> tuple[Tensor, dict[str, float]]:
    """Combine the loss terms per the config weights.

    Expected ``batch`` keys: ``user_labels`` ``[B]``; optional ``weak_probs``
    ``[B,7]``, ``confidence`` ``[B]``, ``session_ids`` ``[B]``. Expected
    ``outputs`` keys: ``embedding``, ``user_logits`` and — for MoE — ``router_logits``
    / ``router_probs``. The ``cfg["loss"]`` block supplies ``lambda_scene``,
    ``lambda_balance``, ``lambda_smooth`` and ``auth_kind``.

    Args:
        outputs: A model forward output dict.
        batch: The training batch tensors.
        cfg: The merged experiment config.

    Returns:
        Tuple ``(loss, parts)`` where ``parts`` maps each term name to its float
        value (``auth``, ``kl``, ``balance``, ``smooth``, ``total``).
    """
    loss_cfg = cfg.get("loss", {})
    lambda_scene = float(loss_cfg.get("lambda_scene", 1.0))
    lambda_balance = float(loss_cfg.get("lambda_balance", 0.005))
    lambda_smooth = float(loss_cfg.get("lambda_smooth", 0.1))
    auth_kind = str(loss_cfg.get("auth_kind", "ce_proto"))

    embeddings = outputs["embedding"]
    user_labels = batch["user_labels"]
    l_auth = auth_loss(
        embeddings,
        user_labels,
        user_logits=outputs.get("user_logits"),
        kind=auth_kind,
    )

    parts: dict[str, float] = {"auth": float(l_auth.detach())}
    loss = l_auth

    router_logits = outputs.get("router_logits")
    router_probs = outputs.get("router_probs")

    # KL-to-weak-label (only when a router + weak targets are present).
    if router_logits is not None and "weak_probs" in batch and lambda_scene > 0:
        confidence = batch.get("confidence")
        if confidence is None:
            confidence = torch.ones(embeddings.shape[0], device=embeddings.device)
        l_kl = kl_weak(F.log_softmax(router_logits, dim=-1), batch["weak_probs"], confidence)
        loss = loss + lambda_scene * l_kl
        parts["kl"] = float(l_kl.detach())

    # Load-balance.
    if router_probs is not None and lambda_balance > 0:
        l_balance = load_balance(router_probs)
        loss = loss + lambda_balance * l_balance
        parts["balance"] = float(l_balance.detach())

    # Temporal smoothness.
    if router_probs is not None and "session_ids" in batch and lambda_smooth > 0:
        l_smooth = temporal_smoothness(router_probs, batch["session_ids"])
        loss = loss + lambda_smooth * l_smooth
        parts["smooth"] = float(l_smooth.detach())

    parts["total"] = float(loss.detach())
    return loss, parts
