"""Weak labeling: score-based 7-class interaction-state labeling functions.

Exposes :func:`research.labeling.interaction_states.weak_label` and the small
helpers used by the pipeline / tests. All labeling uses ONLY non-leakage
features (the IMU-derived ``orient_landscape`` boolean is allowed; the uploaded
``coarse_orientation`` / ``game_like_score`` / ``estimated_context_category`` /
``viewIdResourceName`` are never touched).
"""

from __future__ import annotations

from .interaction_states import (
    LABEL_FEATURE_KEYS,
    softmax,
    topk as topk_scenarios,
    weak_label,
)

__all__ = [
    "weak_label",
    "softmax",
    "topk_scenarios",
    "LABEL_FEATURE_KEYS",
]
