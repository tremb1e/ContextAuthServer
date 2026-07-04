"""Evaluation coverage counting (SRV-5) + scene-matched val impostors (SRV-6).

Builds tiny hand-crafted dataset dirs so a test user with NO enroll prototype is
counted + dropped (not silently), impostor pairs against an un-enrollable user
are counted as skipped, and the val evaluation reports its impostor provenance.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.experiments._data import DatasetBundle
from research.experiments.evaluator import evaluate
from research.experiments.runner import _evaluate_val
from research.experiments.trainer import build_model
from research.utils.seed import set_seed

_FEATS = ["f0", "f1", "f2"]


def _row(win: str, user: str, sess: str, scene: str = "I0") -> dict:
    probs = [1.0 if s == scene else 0.0 for s in ["I0", "I1", "I2", "I3", "I4", "I5", "I6"]]
    rng = np.random.default_rng(abs(hash(win)) % (2**32))
    return {
        "window_id": win, "user_id": user, "session_id": sess, "day_id": "d0",
        "start_elapsed_ns": 0.0, "weak_label_top1": scene,
        "weak_label_probs_json": json.dumps(probs), "weak_label_confidence": 0.9,
        **{f: float(rng.random()) for f in _FEATS},
    }


def _write_dataset(root: Path, train, val, test, pairs) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(train).to_parquet(root / "train.parquet", index=False)
    pd.DataFrame(val).to_parquet(root / "val.parquet", index=False)
    pd.DataFrame(test).to_parquet(root / "test.parquet", index=False)
    pd.DataFrame(pairs).to_parquet(root / "impostor_pairs.parquet", index=False)
    (root / "feature_manifest.json").write_text(
        json.dumps({"feature_columns": _FEATS, "input_dim": len(_FEATS), "package_columns": []}), encoding="utf-8"
    )
    return root


def _tiny_model(bundle: DatasetBundle):
    set_seed(0)
    return build_model({"model": {"kind": "dense", "hidden_dims": [8]}, "runtime": {"smoke": True}, "seed": 0}, bundle)


def test_evaluate_counts_dropped_users(tmp_path: Path) -> None:
    """A test user with no enroll is counted + dropped; skipped impostor pairs counted."""
    # u0 has enroll (train); u1 is TEST-ONLY (no prototype).
    train = [_row("w_u0_tr0", "u0", "s0"), _row("w_u0_tr1", "u0", "s0")]
    val: list = []
    test = [_row("w_u0_te", "u0", "s1"), _row("w_u1_te", "u1", "s2")]
    pairs = [
        # attacked u0 (enrolled) vs u1's test window -> scored.
        {"genuine_window_id": "w_u0_te", "impostor_window_id": "w_u1_te", "genuine_user_id": "u0", "impostor_user_id": "u1", "scene": "I0", "matched_exact": True},
        # attacked u1 (NOT enrolled) -> skipped.
        {"genuine_window_id": "w_u1_te", "impostor_window_id": "w_u0_te", "genuine_user_id": "u1", "impostor_user_id": "u0", "scene": "I0", "matched_exact": True},
    ]
    ds = _write_dataset(tmp_path / "cov", train, val, test, pairs)
    bundle = DatasetBundle(ds)
    result = evaluate(_tiny_model(bundle), bundle, ds)
    assert result.n_test_users == 2
    assert result.dropped_users_no_enroll == ["u1"]
    assert result.n_evaluated_users == 1
    assert result.n_skipped_impostor_pairs_no_enroll == 1
    # Per-pair metadata is populated for the persisted scores frame.
    assert len(result.query_window_ids) == len(result.scores)
    assert "w_u1_te" in result.query_window_ids  # the impostor query window


def test_val_matched_impostor_provenance(tmp_path: Path) -> None:
    """Two-user val with a shared scene yields SCENE-MATCHED val impostors."""
    train = [_row("w_u0_tr", "u0", "s0"), _row("w_u1_tr", "u1", "s1")]
    val = [_row("w_u0_va", "u0", "s2"), _row("w_u1_va", "u1", "s3")]
    test = [_row("w_u0_te", "u0", "s4"), _row("w_u1_te", "u1", "s5")]
    ds = _write_dataset(tmp_path / "val", train, val, test, [])
    bundle = DatasetBundle(ds)
    result, matching = _evaluate_val(_tiny_model(bundle), bundle, seed=0)
    assert matching == "matched"
    assert result.n_genuine == 2 and result.n_impostor >= 1
    # Single-user val -> loose fallback provenance.
    ds2 = _write_dataset(tmp_path / "val1", [_row("w_a_tr", "u0", "s0")], [_row("w_a_va", "u0", "s1")], [_row("w_a_te", "u0", "s2")], [])
    _, matching2 = _evaluate_val(_tiny_model(DatasetBundle(ds2)), DatasetBundle(ds2), seed=0)
    assert matching2 == "loose_fallback"
