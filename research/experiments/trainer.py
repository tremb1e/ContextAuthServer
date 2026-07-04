"""Deterministic training loop with early stop, best checkpoint, JSONL log (§5/§6).

:func:`build_model` constructs a :class:`~research.models.dense.DenseAuthenticator`
or :class:`~research.models.moe.MoEAuthenticator` from the merged config, reading
``input_dim`` / ``n_users`` / package indices from the dataset bundle (never
hardcoded). :func:`train_model` runs a deterministic, smoke-fast training loop
(seeded), evaluates the total loss on the validation split each epoch, keeps the
best-by-val-loss checkpoint (restored at the end), early-stops on patience, and
appends one JSON record per epoch to ``logs/train.jsonl``.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch import Tensor, nn

from research import N_SCENARIOS
from research.experiments._data import DatasetBundle, SplitTensors
from research.models.dense import DenseAuthenticator
from research.models.moe import MoEAuthenticator
from research.models.losses import total_loss
from research.utils.logging import JsonlLogger
from research.utils.seed import set_seed


def build_model(cfg: dict[str, Any], bundle: DatasetBundle) -> nn.Module:
    """Construct the model for a config, sized from the dataset bundle.

    Args:
        cfg: The merged experiment config (``model`` block selects kind/router).
        bundle: The loaded dataset bundle (supplies ``input_dim`` / ``n_users`` /
            package indices).

    Returns:
        A ``DenseAuthenticator`` or ``MoEAuthenticator``.

    Raises:
        ValueError: If ``model.kind`` is unknown.
    """
    model_cfg = cfg.get("model", {})
    kind = str(model_cfg.get("kind", "moe"))
    input_dim = bundle.input_dim
    n_users = bundle.n_users
    embedding_dim = int(model_cfg.get("embedding_dim", 128))
    dropout = float(model_cfg.get("dropout", 0.1))
    smoke = bool(cfg.get("runtime", {}).get("smoke", False))

    if kind == "dense":
        hidden = list(model_cfg.get("hidden_dims", [256, 128]))
        if smoke:
            hidden = [min(64, h) for h in hidden] or [64]
        return DenseAuthenticator(
            input_dim=input_dim,
            hidden_dims=hidden,
            embedding_dim=embedding_dim,
            n_users=n_users,
            dropout=dropout,
            layer_norm=bool(model_cfg.get("layer_norm", False)),
        )
    if kind == "moe":
        expert_hidden = list(model_cfg.get("expert_hidden", [128]))
        if smoke:
            expert_hidden = [min(64, h) for h in expert_hidden] or [64]
        router = str(model_cfg.get("router", "learned"))
        package_indices = bundle.package_indices() if router == "package_only" else None
        if router == "package_only" and not package_indices:
            # No package features in this mode -> fall back to a learned router
            # on the full vector (documented graceful degradation).
            router = "learned"
        return MoEAuthenticator(
            input_dim=input_dim,
            n_experts=int(model_cfg.get("n_experts", N_SCENARIOS)),
            top_k=int(model_cfg.get("top_k", 2)),
            expert_hidden=expert_hidden,
            embedding_dim=embedding_dim,
            n_users=n_users,
            router=router,
            dropout=dropout,
            package_indices=package_indices,
            router_seed=int(cfg.get("seed", 42)),
        )
    raise ValueError(f"unknown model.kind: {kind!r}")


def _forward(model: nn.Module, features: Tensor, weak_probs: Tensor, hash_ids: Tensor) -> dict[str, Tensor]:
    """Call a Dense or MoE model with the arguments each accepts."""
    if isinstance(model, MoEAuthenticator):
        return model(features, weak_probs, hash_ids)
    return model(features)


def _batch_dict(split: SplitTensors, index: Tensor) -> dict[str, Tensor]:
    """Slice a :class:`SplitTensors` into a training-batch dict for the loss."""
    return {
        "user_labels": split.user_labels[index],
        "weak_probs": split.weak_probs[index],
        "confidence": split.confidence[index],
        "session_ids": split.session_ids[index],
    }


@torch.no_grad()
def _val_loss(model: nn.Module, split: SplitTensors, cfg: dict[str, Any]) -> float:
    """Compute the total loss on a whole split (validation), or ``nan`` if empty."""
    if split.features.shape[0] == 0:
        return float("nan")
    model.eval()
    outputs = _forward(model, split.features, split.weak_probs, split.hash_ids)
    index = torch.arange(split.features.shape[0])
    loss, _ = total_loss(outputs, _batch_dict(split, index), cfg)
    return float(loss.detach())


def train_model(
    cfg: dict[str, Any],
    bundle: DatasetBundle,
    out_dir: str | Path,
) -> tuple[nn.Module, dict[str, Any]]:
    """Train a model deterministically with early stop + best checkpoint.

    Args:
        cfg: The merged experiment config.
        bundle: The loaded dataset bundle.
        out_dir: The run directory; the epoch log is written to
            ``out_dir/logs/train.jsonl``.

    Returns:
        Tuple ``(model, history)`` where ``model`` holds the best-val-loss
        weights (restored) and ``history`` records per-epoch train/val loss, the
        best epoch, and the model's parameter counts.
    """
    seed = int(cfg.get("seed", 42))
    set_seed(seed)
    train_cfg = cfg.get("train", {})
    epochs = int(train_cfg.get("epochs", 2))
    lr = float(train_cfg.get("lr", 1e-3))
    batch_size = int(train_cfg.get("batch_size", 64))
    patience = int(train_cfg.get("early_stop_patience", 3))
    if bool(cfg.get("runtime", {}).get("smoke", False)):
        epochs = min(epochs, 2)

    model = build_model(cfg, bundle)
    model.train()

    train_split = bundle.tensors("train")
    val_split = bundle.tensors("val")
    n_train = train_split.features.shape[0]

    logger = JsonlLogger(Path(out_dir) / "logs" / "train.jsonl")
    history: dict[str, Any] = {
        "epochs": [],
        "best_epoch": -1,
        "best_val_loss": float("inf"),
        "param_count": int(sum(p.numel() for p in model.parameters())),
    }
    if hasattr(model, "active_param_count"):
        history["active_param_count"] = int(model.active_param_count())  # type: ignore[operator]

    if n_train == 0:
        logger.log("train_skipped", reason="empty_train_split")
        return model, history

    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    generator = torch.Generator().manual_seed(seed)
    best_state = copy.deepcopy(model.state_dict())
    best_val = float("inf")
    no_improve = 0

    for epoch in range(epochs):
        model.train()
        perm = torch.randperm(n_train, generator=generator)
        epoch_losses: list[float] = []
        parts_acc: dict[str, float] = {}
        for start in range(0, n_train, batch_size):
            index = perm[start : start + batch_size]
            outputs = _forward(model, train_split.features[index], train_split.weak_probs[index], train_split.hash_ids[index])
            loss, parts = total_loss(outputs, _batch_dict(train_split, index), cfg)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            epoch_losses.append(float(loss.detach()))
            for key, value in parts.items():
                parts_acc[key] = parts_acc.get(key, 0.0) + value

        train_loss = float(np.mean(epoch_losses)) if epoch_losses else float("nan")
        val_loss = _val_loss(model, val_split, cfg)
        record = {
            "epoch": epoch,
            "train_loss": train_loss,
            "val_loss": val_loss,
            "loss_parts": {k: v / max(1, len(epoch_losses)) for k, v in parts_acc.items()},
        }
        history["epochs"].append(record)
        logger.log("epoch_end", **record)

        # Best-checkpoint / early-stop on val loss (train loss if val is empty).
        monitor = val_loss if np.isfinite(val_loss) else train_loss
        if np.isfinite(monitor) and monitor < best_val - 1e-6:
            best_val = monitor
            best_state = copy.deepcopy(model.state_dict())
            history["best_epoch"] = epoch
            history["best_val_loss"] = float(best_val)
            no_improve = 0
        else:
            no_improve += 1
            if no_improve >= patience:
                logger.log("early_stop", epoch=epoch, best_epoch=history["best_epoch"])
                break

    model.load_state_dict(best_state)
    if history["best_epoch"] < 0:  # never improved (e.g. single degenerate epoch)
        history["best_epoch"] = len(history["epochs"]) - 1
        history["best_val_loss"] = float(best_val) if np.isfinite(best_val) else float("nan")
    return model, history
