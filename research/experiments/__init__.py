"""Experiment layer (build contract §11 S4).

Metrics (EER/AUC + per-user/per-scene, HMOG-parity), by-user bootstrap + Holm +
paired-delta statistics, a deterministic trainer, a prototype/cosine evaluator
(enroll/query sessions disjoint), and the experiment runner (single run, top-k
sweep + Pareto k*, and the M0..M10 suite). All modules mirror the HMOG
methodology of ``_recon_hmog.md`` where noted and read ``input_dim`` from the
dataset feature manifest — never hardcoded.
"""

from __future__ import annotations

__all__: list[str] = []
