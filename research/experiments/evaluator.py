"""Prototype/cosine verification with enroll/query sessions DISJOINT (§5, §8.2).

The trained encoder maps every window to an embedding. For each tested user a
**prototype** is the mean L2-normalised embedding of that user's ENROLL windows
(the train + val split rows). Query windows are that user's TEST split rows —
enroll and query sessions are disjoint by construction of the leave-session-out
protocol (and asserted in ``split_manifest.leakage_check``), which prevents the
EER from being spuriously low via near-duplicate overlapping windows.

* **Genuine** score = cosine(query embedding, own user prototype) — same user,
  cross-session (enroll ∩ query sessions == ∅), label ``1``.
* **Impostor** score = cosine(impostor query embedding, ATTACKED user prototype)
  for each matched-impostor pair (scene-matched, user-disjoint), label ``0``.

:func:`evaluate` returns the pooled ``scores`` / ``labels`` / per-pair ``users``
(the attacked genuine user) / ``scenes`` (matched weak label) for the EER family,
plus router/expert utilisation statistics gathered on the query windows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from torch import Tensor, nn

from research import N_SCENARIOS, SCENARIOS
from research.datasets.splits import SESSION_COL, USER_COL, WINDOW_COL
from research.experiments._data import DatasetBundle, scene_to_index
from research.models.moe import MoEAuthenticator
from research.utils.logging import get_logger

LOGGER = get_logger("research.evaluator")


@dataclass
class EvalResult:
    """Pooled verification scores + routing diagnostics for one run.

    Attributes:
        scores: Pooled match scores (cosine; larger == genuine).
        labels: Binary labels aligned with ``scores`` (1 == genuine).
        users: The attacked (genuine) user id for each pair.
        scenes: The matched weak-label scenario id for each pair.
        n_genuine: Number of genuine pairs.
        n_impostor: Number of impostor pairs.
        query_window_ids: Per-pair query ``window_id`` (SRV-4) — the genuine test
            window for genuine pairs, the impostor window for impostor pairs. Lets
            scores be redrawn / event streams rebuilt post-hoc.
        impostor_user_ids: Per-pair impostor source ``user_id`` (``""`` for
            genuine pairs).
        session_ids: Per-pair query ``session_id`` (the test session for genuine
            pairs, the impostor window's session for impostor pairs).
        n_test_users: Distinct users present in the test split (SRV-5).
        n_evaluated_users: Test users that actually contributed a genuine pair
            (had an enroll prototype).
        dropped_users_no_enroll: Test users silently dropped for lack of any
            enroll (train/val) prototype.
        n_skipped_impostor_pairs_no_enroll: Impostor pairs skipped because the
            attacked user had no enroll prototype.
        router_probs_mean: Mean dense router distribution over query windows
            (length 7; empty for dense models).
        expert_utilization: Fraction of query windows for which each expert is in
            the active top-k set (length 7; empty for dense models).
        expert_scene_matrix: ``[7 scene x 7 expert]`` mean gate weight — expert
            activation conditioned on the query window's weak-label scene.
        active_experts: The configured number of active experts (top_k).
    """

    scores: np.ndarray
    labels: np.ndarray
    users: list[str]
    scenes: list[str]
    n_genuine: int
    n_impostor: int
    query_window_ids: list[str] = field(default_factory=list)
    impostor_user_ids: list[str] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    n_test_users: int = 0
    n_evaluated_users: int = 0
    dropped_users_no_enroll: list[str] = field(default_factory=list)
    n_skipped_impostor_pairs_no_enroll: int = 0
    router_probs_mean: list[float] = field(default_factory=list)
    expert_utilization: list[float] = field(default_factory=list)
    expert_scene_matrix: list[list[float]] = field(default_factory=list)
    active_experts: float = 0.0


def _embed_all(model: nn.Module, bundle: DatasetBundle, split: str) -> tuple[np.ndarray, pd.DataFrame]:
    """Return L2-normalised embeddings + aligned metadata for a whole split.

    Args:
        model: The trained encoder.
        bundle: The dataset bundle.
        split: One of ``train`` / ``val`` / ``test``.

    Returns:
        Tuple ``(embeddings[N, emb], meta)`` (meta indexed 0..N-1).
    """
    tensors = bundle.tensors(split)
    if tensors.features.shape[0] == 0:
        return np.zeros((0, getattr(model, "embedding_dim", 1))), pd.DataFrame()
    model.eval()
    with torch.no_grad():
        if isinstance(model, MoEAuthenticator):
            emb = model.embed_normalized(tensors.features, tensors.weak_probs, tensors.hash_ids)
        else:
            emb = model.embed_normalized(tensors.features)
    return emb.cpu().numpy(), tensors.meta.reset_index(drop=True)


def _prototypes(enroll_emb: np.ndarray, enroll_meta: pd.DataFrame) -> dict[str, np.ndarray]:
    """Build a unit-norm prototype per user from enroll embeddings.

    Args:
        enroll_emb: ``[N, emb]`` enroll embeddings.
        enroll_meta: Aligned metadata (needs ``user_id``).

    Returns:
        Mapping ``user_id -> unit-norm prototype`` (empty when no enroll data).
    """
    protos: dict[str, np.ndarray] = {}
    if enroll_emb.shape[0] == 0 or enroll_meta.empty:
        return protos
    for user in enroll_meta[USER_COL].astype(str).unique():
        mask = (enroll_meta[USER_COL].astype(str) == user).to_numpy()
        vecs = enroll_emb[mask]
        if vecs.shape[0] == 0:
            continue
        mean = vecs.mean(axis=0)
        norm = float(np.linalg.norm(mean))
        protos[str(user)] = mean / norm if norm > 1e-12 else mean
    return protos


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two vectors (0.0 if either is degenerate)."""
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _router_diagnostics(model: nn.Module, bundle: DatasetBundle) -> dict[str, object]:
    """Gather router-probability / expert-utilisation stats on query windows.

    Args:
        model: The trained model.
        bundle: The dataset bundle.

    Returns:
        Dict with ``router_probs_mean``, ``expert_utilization``,
        ``expert_scene_matrix`` and ``active_experts`` (empty lists for dense).
    """
    if not isinstance(model, MoEAuthenticator):
        return {
            "router_probs_mean": [],
            "expert_utilization": [],
            "expert_scene_matrix": [],
            "active_experts": 0.0,
        }
    tensors = bundle.tensors("test")
    if tensors.features.shape[0] == 0:
        return {
            "router_probs_mean": [0.0] * N_SCENARIOS,
            "expert_utilization": [0.0] * N_SCENARIOS,
            "expert_scene_matrix": [[0.0] * N_SCENARIOS for _ in range(N_SCENARIOS)],
            "active_experts": float(model.top_k),
        }
    model.eval()
    with torch.no_grad():
        outputs = model(tensors.features, tensors.weak_probs, tensors.hash_ids)
    router_probs = outputs["router_probs"].cpu().numpy()
    gate = outputs["gate_weights"].cpu().numpy()
    active = (gate > 0).astype(float)

    scenes = tensors.meta["weak_label_top1"].astype(str).tolist() if "weak_label_top1" in tensors.meta else []
    scene_matrix = np.zeros((N_SCENARIOS, N_SCENARIOS), dtype=float)
    scene_counts = np.zeros(N_SCENARIOS, dtype=float)
    for row_idx, scene in enumerate(scenes):
        s = scene_to_index(scene)
        scene_matrix[s] += gate[row_idx]
        scene_counts[s] += 1.0
    for s in range(N_SCENARIOS):
        if scene_counts[s] > 0:
            scene_matrix[s] /= scene_counts[s]

    return {
        "router_probs_mean": router_probs.mean(axis=0).tolist(),
        "expert_utilization": active.mean(axis=0).tolist(),
        "expert_scene_matrix": scene_matrix.tolist(),
        "active_experts": float(model.top_k),
    }


def evaluate(model: nn.Module, bundle: DatasetBundle, data_dir: str | Path) -> EvalResult:
    """Run prototype/cosine verification and return pooled scores + diagnostics.

    Args:
        model: The trained encoder.
        bundle: The loaded dataset bundle.
        data_dir: The dataset directory (source of ``impostor_pairs.parquet``).

    Returns:
        The populated :class:`EvalResult`. When the split is too small to form
        any genuine/impostor pair the score/label arrays are empty (downstream
        metrics then return ``nan`` via their guards).
    """
    # Enroll = train + val windows; query = test windows (sessions disjoint).
    train_emb, train_meta = _embed_all(model, bundle, "train")
    val_emb, val_meta = _embed_all(model, bundle, "val")
    test_emb, test_meta = _embed_all(model, bundle, "test")

    if test_emb.shape[0] == 0:
        diag = _router_diagnostics(model, bundle)
        return EvalResult(
            scores=np.empty(0),
            labels=np.empty(0),
            users=[],
            scenes=[],
            n_genuine=0,
            n_impostor=0,
            **diag,  # type: ignore[arg-type]
        )

    enroll_emb = np.concatenate([e for e in (train_emb, val_emb) if e.shape[0] > 0], axis=0)
    enroll_meta = pd.concat([m for m in (train_meta, val_meta) if not m.empty], ignore_index=True)
    prototypes = _prototypes(enroll_emb, enroll_meta)

    # Index test embeddings by window_id for the impostor-pair lookup.
    win_to_row = {str(w): i for i, w in enumerate(test_meta[WINDOW_COL].astype(str))}
    # And a global window->embedding map across ALL splits (impostor windows may
    # live in any split; we score them against the attacked user's prototype).
    all_emb = np.concatenate([e for e in (train_emb, val_emb, test_emb) if e.shape[0] > 0], axis=0)
    all_meta = pd.concat([m for m in (train_meta, val_meta, test_meta) if not m.empty], ignore_index=True)
    global_win_to_row = {str(w): i for i, w in enumerate(all_meta[WINDOW_COL].astype(str))}

    scores: list[float] = []
    labels: list[int] = []
    users: list[str] = []
    scenes: list[str] = []
    query_window_ids: list[str] = []
    impostor_user_ids: list[str] = []
    session_ids: list[str] = []

    # SRV-5: per-user evaluation coverage. A test user with no enroll prototype is
    # silently unevaluable (no genuine pair); count and warn instead of dropping
    # it invisibly.
    test_users = set(test_meta[USER_COL].astype(str)) if USER_COL in test_meta else set()
    dropped_users: set[str] = set()
    all_sessions = all_meta[SESSION_COL].astype(str).tolist() if SESSION_COL in all_meta else [""] * len(all_meta)

    # Genuine pairs: each test (query) window vs its own user's prototype.
    for row in range(test_emb.shape[0]):
        user = str(test_meta.loc[row, USER_COL])
        proto = prototypes.get(user)
        if proto is None:  # user had no enroll windows -> cannot form a genuine pair
            dropped_users.add(user)
            continue
        scene = str(test_meta.loc[row, "weak_label_top1"]) if "weak_label_top1" in test_meta else SCENARIOS[0]
        scores.append(_cosine(test_emb[row], proto))
        labels.append(1)
        users.append(user)
        scenes.append(scene)
        query_window_ids.append(str(test_meta.loc[row, WINDOW_COL]) if WINDOW_COL in test_meta else "")
        impostor_user_ids.append("")
        session_ids.append(str(test_meta.loc[row, SESSION_COL]) if SESSION_COL in test_meta else "")

    # Impostor pairs: matched impostor window vs the ATTACKED user's prototype.
    n_skipped_impostor_pairs_no_enroll = 0
    ip_path = Path(data_dir) / "impostor_pairs.parquet"
    if ip_path.exists():
        pairs = pd.read_parquet(ip_path)
        for _, pair in pairs.iterrows():
            attacked = str(pair["genuine_user_id"])
            proto = prototypes.get(attacked)
            if proto is None:
                n_skipped_impostor_pairs_no_enroll += 1
                continue
            iwin = str(pair["impostor_window_id"])
            irow = global_win_to_row.get(iwin)
            if irow is None:
                continue
            scores.append(_cosine(all_emb[irow], proto))
            labels.append(0)
            users.append(attacked)
            scenes.append(str(pair["scene"]))
            query_window_ids.append(iwin)
            impostor_user_ids.append(str(pair["impostor_user_id"]))
            session_ids.append(all_sessions[irow] if irow < len(all_sessions) else "")

    dropped_sorted = sorted(dropped_users)
    if dropped_sorted:
        LOGGER.warning(
            "evaluate: %d/%d test users have no enroll prototype and were dropped: %s",
            len(dropped_sorted), len(test_users), dropped_sorted,
        )
    if n_skipped_impostor_pairs_no_enroll:
        LOGGER.warning("evaluate: skipped %d impostor pairs (attacked user had no enroll)", n_skipped_impostor_pairs_no_enroll)

    diag = _router_diagnostics(model, bundle)
    return EvalResult(
        scores=np.asarray(scores, dtype=float),
        labels=np.asarray(labels, dtype=int),
        users=users,
        scenes=scenes,
        n_genuine=int(sum(1 for lbl in labels if lbl == 1)),
        n_impostor=int(sum(1 for lbl in labels if lbl == 0)),
        query_window_ids=query_window_ids,
        impostor_user_ids=impostor_user_ids,
        session_ids=session_ids,
        n_test_users=len(test_users),
        n_evaluated_users=len(test_users) - len(dropped_sorted),
        dropped_users_no_enroll=dropped_sorted,
        n_skipped_impostor_pairs_no_enroll=n_skipped_impostor_pairs_no_enroll,
        **diag,  # type: ignore[arg-type]
    )
