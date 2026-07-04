"""Matched-impostor sampling (build contract §11 S3, spec §5.4, HMOG §5).

For each genuine test window we sample impostor windows drawn from OTHER users
whose weak label matches (same ``weak_label_top1``; a relaxed fallback requires
only that the genuine top1 appears in the impostor's ``weak_label_topk``). This
holds the interaction *scene* fixed across the genuine/impostor comparison so
the verifier is judged on identity, not on scene confounds.

Critical leakage guard (asserted by the dataset builder): the pool of impostor
*users* is user-level DISJOINT from the genuine user being attacked — an
impostor window can never come from the genuine user's own rows. Sampling is
deterministic given ``seed`` (a per-target stable seed, HMOG idiom), never
:func:`random`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.datasets.splits import USER_COL, WINDOW_COL

# Column carrying the primary weak label used for scene matching.
TOP1_COL = "weak_label_top1"
# JSON-encoded list column carrying the top-k weak labels (for relaxed match).
TOPK_JSON_COL = "weak_label_topk_json"


@dataclass
class ImpostorPairs:
    """Matched genuine/impostor pairs for one test split.

    Attributes:
        genuine_window_ids: Genuine test ``window_id`` for each pair row.
        impostor_window_ids: The sampled impostor ``window_id`` for each pair.
        genuine_user_ids: Genuine (attacked) ``user_id`` per pair.
        impostor_user_ids: Impostor source ``user_id`` per pair.
        scene: The matched weak-label scenario per pair.
        matched_exact: Whether the pair matched on ``top1`` (True) or only via
            the relaxed ``topk`` fallback (False).
    """

    genuine_window_ids: list[str]
    impostor_window_ids: list[str]
    genuine_user_ids: list[str]
    impostor_user_ids: list[str]
    scene: list[str]
    matched_exact: list[bool]

    def __len__(self) -> int:
        """Return the number of matched pairs."""
        return len(self.genuine_window_ids)

    def to_frame(self) -> pd.DataFrame:
        """Return the pairs as a tidy DataFrame (one row per pair)."""
        return pd.DataFrame(
            {
                "genuine_window_id": self.genuine_window_ids,
                "impostor_window_id": self.impostor_window_ids,
                "genuine_user_id": self.genuine_user_ids,
                "impostor_user_id": self.impostor_user_ids,
                "scene": self.scene,
                "matched_exact": self.matched_exact,
            }
        )

    def impostor_pool_disjoint(self) -> bool:
        """Return True iff no impostor user coincides with its genuine user.

        This is the per-pair user-level disjointness the split manifest asserts
        as ``impostor_pool_user_disjoint``.
        """
        return all(g != i for g, i in zip(self.genuine_user_ids, self.impostor_user_ids))


def _stable_seed(*parts: object, base: int) -> int:
    """Derive a stable non-negative 63-bit seed from string parts.

    Mirrors the HMOG ``stable_int_seed`` idiom so every draw is reproducible and
    independent of process / hash randomisation.

    Args:
        *parts: Components identifying the draw (target user, window id, ...).
        base: An integer base salt.

    Returns:
        A deterministic seed in ``[0, 2**63)``.
    """
    key = f"{int(base)}:" + ":".join(str(p) for p in parts)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % (2**63)


def _topk_of(row: pd.Series) -> list[str]:
    """Decode a window's top-k weak-label list from its JSON column.

    Args:
        row: A window row.

    Returns:
        The list of scenario ids (empty on missing/invalid JSON).
    """
    raw = row.get(TOPK_JSON_COL)
    if isinstance(raw, str) and raw:
        try:
            value = json.loads(raw)
            if isinstance(value, list):
                return [str(v) for v in value]
        except (ValueError, TypeError):  # pragma: no cover - defensive
            return []
    return []


def sample_matched_impostors(
    windows: pd.DataFrame,
    genuine_idx: list[int],
    pool_idx: list[int],
    *,
    seed: int = 42,
    n_per_genuine: int = 1,
    relaxed_topk: bool = True,
) -> ImpostorPairs:
    """Sample scene-matched impostor windows for each genuine test window.

    For every genuine window (rows ``genuine_idx``) impostor candidates are
    taken from ``pool_idx`` restricted to (a) users other than the genuine
    window's user and (b) a matching weak label. Exact match requires the same
    ``weak_label_top1``; if none exist and ``relaxed_topk`` is set, candidates
    whose ``weak_label_topk`` contains the genuine ``top1`` are used. Draws are
    deterministic per ``(seed, genuine_window_id)``.

    Args:
        windows: The full window table (row index == row id).
        genuine_idx: Row indices of the genuine test windows to attack.
        pool_idx: Row indices forming the impostor candidate pool. The builder
            passes the HELD-OUT test split rows only (SRV-6), so impostor windows
            never come from train/val. Any pool row sharing the genuine user is
            skipped (user-level disjointness).
        seed: Deterministic sampling salt.
        n_per_genuine: Number of impostor windows to sample per genuine window.
        relaxed_topk: Whether to fall back to a top-k membership match when no
            exact top1 match exists for a scene.

    Returns:
        The populated :class:`ImpostorPairs` (may be shorter than
        ``len(genuine_idx) * n_per_genuine`` if some scenes have no valid
        cross-user candidate).
    """
    pool = windows.loc[pool_idx]
    # Pre-index candidate row-ids by (user, top1) for O(1) scene lookups.
    by_top1: dict[str, list[int]] = {}
    topk_members: dict[str, list[int]] = {}
    pool_user: dict[int, str] = {}
    for ridx, row in pool.iterrows():
        user = str(row[USER_COL])
        pool_user[int(ridx)] = user
        top1 = str(row[TOP1_COL])
        by_top1.setdefault(top1, []).append(int(ridx))
        for scene in set(_topk_of(row)):
            topk_members.setdefault(scene, []).append(int(ridx))

    pairs = ImpostorPairs([], [], [], [], [], [])
    for gidx in genuine_idx:
        grow = windows.loc[gidx]
        guser = str(grow[USER_COL])
        gscene = str(grow[TOP1_COL])
        gwin = str(grow[WINDOW_COL])

        # Candidate ids: exact top1 match from OTHER users.
        candidates = [i for i in by_top1.get(gscene, []) if pool_user.get(i) != guser]
        matched_exact = True
        if not candidates and relaxed_topk:
            candidates = [i for i in topk_members.get(gscene, []) if pool_user.get(i) != guser]
            matched_exact = False
        if not candidates:
            continue

        rng = np.random.default_rng(_stable_seed(guser, gwin, base=seed))
        order = rng.permutation(len(candidates))
        take = min(n_per_genuine, len(candidates))
        for j in range(take):
            iidx = candidates[int(order[j])]
            irow = windows.loc[iidx]
            pairs.genuine_window_ids.append(gwin)
            pairs.impostor_window_ids.append(str(irow[WINDOW_COL]))
            pairs.genuine_user_ids.append(guser)
            pairs.impostor_user_ids.append(str(irow[USER_COL]))
            pairs.scene.append(gscene)
            pairs.matched_exact.append(matched_exact)
    return pairs
