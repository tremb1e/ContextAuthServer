# ContextAuth `research/` — MoE Behavioural-Authentication Experiment Layer

This package is the **offline machine-learning research layer** for ContextAuth. It
is fully decoupled from the ingest service in `app/` (which only receives, validates
and stores batches — no ML). Everything here reads the on-disk batch tree the server
writes (or a synthetic generator that writes the *same* tree), turns it into windowed
features + weak labels, trains a **Mixture-of-Experts (MoE) authenticator** against
several baselines, and produces publication figures + a report.

Ground truth of the design (frozen): **7 interaction scenarios `I0..I6` == 7 experts**,
**top-k swept over `1..7`** (not a fixed 3), a **learned weakly-supervised router**,
`encryption:"none"` (TLS confidentiality + SHA-256 integrity over the LZ4-compressed
bytes), `device_id`-only identity. Authoritative contracts: `research/_BUILD_CONTRACT.md`
(frozen build contract), `research/_recon_spec.md` (requirements digest), and the
Chinese design doc referenced under **中文文档** below.

> **P0 honesty note.** All results ship on **synthetic data**: they validate that the
> pipeline and method are self-consistent and run end-to-end. They **cannot** replace
> real multi-user empirical conclusions — collecting real multi-user data and
> re-establishing effect sizes / significance on real devices is **P0** (see
> *Limitations*).

---

## 1. Architecture & data flow

```
                 (real device OR synthetic generator — SAME on-disk layout)
   devices/{device_id}/{date}/{batch_id}.json          (+ by_category/ symlinks, index/)
   envelopes/{batch_id}.json   (optional 8-field LZ4_FRAME+JSON, ingestable by the server)
        │
        │  preprocessing/  (loaders → align → sessionize → windowing → feature_extractors → quality)
        │  labeling/       (interaction_states: score-based 7-class weak labels)
        ▼
   data/processed/windows.parquet          + feature_manifest.json + preprocess_report.json
        │
        │  datasets/  (splits: leave_session/day/app_out + combined; impostors: matched, user-disjoint)
        ▼
   data/datasets/{name}/{train,val,test}.parquet
        + impostor_pairs.parquet + split_manifest.json (leakage_check ALL True) + feature_manifest.json
        │
        │  models/       (dense; moe E=7 top-k 1..7; 5 routers; losses)
        │  experiments/  (trainer → evaluator[prototype/cosine, enroll≠query] → metrics → bootstrap → runner)
        ▼
   data/results/{run_id}/   config.yaml, metrics.json/.csv, per_user/per_scene_metrics.csv,
        expert_utilization.csv, expert_scene_matrix.csv, model.pt, logs/train.jsonl, run_context.json
        + topk_sweep.csv, topk_kstar.json, runs_index.json
        │
        │  reporting/  (plots: matplotlib+numpy only, no titles, no CJK, PDF+PNG@300dpi; tables: LaTeX; report: 中文)
        ▼
   data/results/report.md  + plots/*.{pdf,png}  + latex_tables.tex
```

**Package layout** (every module has full type hints + docstrings; no TODO stubs in
core paths):

```
research/
  __init__.py                  frozen constants: SCENARIOS, SCENARIO_NAMES, N_SCENARIOS,
                               LEAKAGE_COLUMNS, SENSOR_TYPES (defined ONCE, imported everywhere)
  config.py                    load_config / deep_merge / config_hash
  configs/default.yaml         base config; configs/experiments/*.yaml are thin overrides
  utils/                       logging (JSONL + run_context), seed, io
  preprocessing/               loaders, align, sessionize, windowing, feature_extractors, quality
  labeling/                    interaction_states (weak_label, LABEL_FEATURE_KEYS allow-list)
  datasets/                    builders, splits, impostors
  models/                      dense, moe, routing, losses
  experiments/                 metrics, bootstrap, trainer, evaluator, runner (+ _data, _data bundle)
  reporting/                   plots, tables, report
  scripts/                     generate_synthetic_data, run_preprocess, build_datasets,
                               run_experiment, run_all_experiments, make_report, export_artifact_bundle
  tests/                       21 pytest files + conftest (tiny synthetic fixture,
                               session-scoped); 114 test cases green (conda hmog_1dcnn)
```

### Key invariants (enforced in code, checked by tests)

- **3-channel parity.** `SENSOR_TYPES = [ACCELEROMETER, GYROSCOPE, MAGNETIC_FIELD]` are
  fully symmetric in `feature_extractors`; a missing channel sets `{ch}_missing = 1.0`
  and zero-fills that channel's cells (never a silent zero).
- **No leakage.** The 4 `LEAKAGE_COLUMNS` — `estimated_context_category`,
  `game_like_score`, `viewIdResourceName`, `coarse_orientation` — are **never** computed
  into features and **never** read by the weak labeler. The IMU-derived landscape
  boolean `orient_landscape` (our own signal, not the uploaded `coarse_orientation`) is
  the one explicitly-allowed orientation feature.
- **Manifest-driven `input_dim`.** Models read `input_dim = len(feature_columns)` from
  `feature_manifest.json`; it is never hardcoded.
- **Leakage-free splits.** Whole groups (sessions / days / apps) move together — never
  individual windows — so adjacent overlapping windows can't straddle a split. The
  builder asserts every `split_manifest.leakage_check` is True (raises otherwise), and
  enroll (train+val) / query (test) sessions are disjoint for prototype/cosine eval.
- **Matched impostors, user-disjoint, test-split-only.** Each genuine test window is
  compared against impostor windows from *other* users with the same weak-label scene.
  *(2026-07-05 SRV-6: the impostor pool is drawn from the **held-out test split only** —
  never train/val — and `split_manifest` records `impostor_windows_test_only` so the
  restriction is auditable.)*
- **Frozen `k*`.** The top-k `1..7` sweep selects `k*` on **validation** (smallest-cost
  `k` whose EER is not significantly worse than the best), then the test EER is read
  once at that `k*`.

### Baselines (M0..M10) and configs

`configs/experiments/m0.yaml .. m10.yaml` are thin overrides that mirror
`research.experiments.runner.M_OVERRIDES` exactly. `top_k: "__kstar__"` is a sentinel the
runner substitutes with the frozen `k*` (from `topk_kstar.json`) at suite-build time.

| Cfg | Label | kind / router / top_k | feature mode | Purpose (RQ) |
|-----|-------|-----------------------|--------------|--------------|
| m0  | sensor_only_dense      | dense                    | sensor_only          | RQ1 lower bound |
| m1  | ui_sensor_dense        | dense                    | ui_sensor            | RQ1 mid |
| m2  | capacity_matched_dense | dense (wider)            | ui_sensor            | RQ2 capacity control |
| m3  | package_only_router    | moe / package_only / 2   | ui_sensor            | RQ6 package confound |
| m4  | fixed_rule_top1        | moe / fixed_rule / 1     | ui_sensor            | RQ3 fixed-rule anchor |
| m5  | fixed_rule_topk_star   | moe / fixed_rule / k\*   | ui_sensor            | RQ3 strong fixed baseline |
| m6  | auth_only_moe          | moe / learned / k\* (λ_scene=0) | ui_sensor     | RQ4 weak-sup vs auth-only |
| m7  | **weak_moe** (formal)  | moe / learned / k\*      | ui_sensor            | the method |
| m8  | weak_moe_no_package    | moe / learned / k\*      | ui_sensor_no_package | RQ6 no-package |
| m9  | random_moe             | moe / random / k\*       | ui_sensor            | routing control |
| m10 | hash_moe               | moe / hash / k\*         | ui_sensor            | routing control |

**Ablation configs** (each lists its swept dimension under the `ablation:` key):
`ablation_topk.yaml` (top_k `1..7`), `ablation_privacy.yaml`
(`privacy_coarse_bounds` / `no_resource_id` / `coarse_widget_category_only`),
`ablation_features.yaml` (`no_ui` / `no_sensor` / `no_package` / `no_tree_diff` /
`no_temporal_smoothness` / `no_load_balance`), `ablation_sensor_channel.yaml`
(`no_accel` / `no_gyro` / `no_magnetometer`).

---

## 2. Runtime environment

- **Python interpreter (the only supported one):**
  `/home/tremb1e/miniconda3/envs/hmog_1dcnn/bin/python` (Python 3.10, the HMOG-parity
  conda env `hmog_1dcnn`). Torch runs on **CPU**; no CUDA is assumed anywhere.
- **Dependencies** are pinned in `research/requirements.txt`
  (`numpy==1.26.4`, `torch==2.4.1`, `pandas==2.2.2`, `scikit-learn==1.5.2`,
  `scipy==1.13.1`, `matplotlib==3.9.2`, `pyarrow==17.0.0`, `pyyaml==6.0.3`,
  `pytest==9.0.3`, `lz4>=4.3`). These are RESEARCH-only deps — never add them to the
  ingest service's minimal `app/` deps.
- Run everything as a package from the repo root
  (`/data/paper/sp/app_exp/ContextAuthServer`): `python -m research.scripts.<name>` and
  `python -m pytest research/tests`. `data/` is runtime-generated and gitignored.

---

## 3. Reproducible commands (`_recon_spec.md` §16)

All commands assume `cd /data/paper/sp/app_exp/ContextAuthServer` and
`PY=/home/tremb1e/miniconda3/envs/hmog_1dcnn/bin/python`.

```bash
# 0) run the smoke test suite (tiny synthetic, finishes in a couple of minutes on CPU)
$PY -m pytest research/tests -q

# 1) synthetic data (add --emit-envelopes to also write server-ingestable LZ4 envelopes)
$PY -m research.scripts.generate_synthetic_data \
    --users 20 --days 3 --sessions-per-day 4 --out data/synthetic --seed 42

# 2) preprocess -> windows.parquet + feature_manifest.json + preprocess_report.json
$PY -m research.scripts.run_preprocess \
    --input data/synthetic --output data/processed \
    --window-size-sec 5 --stride-sec 1

# 3) build a leakage-checked dataset (leave_session_out shown; also leave_day_out,
#    leave_app_out, combined). Asserts split_manifest.leakage_check ALL True.
$PY -m research.scripts.build_datasets \
    --input data/processed --output data/datasets \
    --protocol leave_session_out

# 4) full M0..M10 suite + top-k 1..7 sweep (freezes k* on validation) -> runs_index.json
$PY -m research.scripts.run_all_experiments \
    --config research/configs/default.yaml \
    --data data/datasets --out data/results

#    (single baseline, e.g. the formal method M7:)
$PY -m research.scripts.run_experiment \
    --config research/configs/experiments/m7.yaml \
    --data data/datasets --out data/results --tag m7

# 5) report: 中文 report.md + plots/*.{pdf,png} (no titles, no CJK in figures) + latex_tables.tex
$PY -m research.scripts.make_report \
    --results data/results --out data/results/report.md \
    --data data/datasets

# 6) (optional) bundle the artifacts for sharing
$PY -m research.scripts.export_artifact_bundle --out data/artifact_bundle
```

Add `--smoke` to `run_experiment` / `run_all_experiments` to shrink nets/epochs/data for
a fast CPU dry run (the trainer caps smoke epochs at ≤2, so `pytest` stays fast). The
default config now trains for the **formal `epochs=100`** (2026-07-05 SRV-9); omit
`--smoke` only when you actually want a full-budget run.

---

## 4. Minimal-working-version limitations (`_recon_spec.md` §17, honest scope)

Everything imports and runs end-to-end on synthetic data with **no TODO stubs in core
paths**. The following are deliberate, documented reductions relative to the full spec —
each is a *representative* minimal implementation, not a placeholder:

1. **Single-user *real* data is absent → multi-user empirical is P0.** All numbers come
   from the synthetic generator. Synthetic data validates the pipeline and the method's
   internal consistency only; it **cannot** substantiate real-world effect sizes,
   significance, or per-user/per-scene conclusions. Real multi-user collection +
   re-analysis on devices is the top priority (P0).

2. **Reduced-but-representative feature families.** All families required by the contract
   are present (IMU time+freq per 3 channels/3 axes, orientation, cross-channel, motion
   bins, events, UI incl. tree-diff, package), but in reduced form:
   depth/category *histograms* are summarised to counts/ratios rather than full
   histograms; bounds-grid occupancy is a single scalar rather than a grid histogram;
   frequency features use **numpy `rfft`** (three band-energy ratios + dom-freq +
   spectral centroid/entropy) rather than a full periodogram. `input_dim` for
   `ui_sensor` is 204 (manifest-driven).
   *(2026-07-04 P0-1 unit fix: the bounds-grid occupancy / `ui_surface_like` scalars are
   now **scale-invariant**. The old `_bounds_area` normalised by 1080×1920 **pixels**
   while `bounds_grid` is pixels **÷24** (effective ~173×158), which pinned
   `ui_bounds_occupancy`/`ui_surface_like` to ≈0 on real devices; the denominator is now
   the per-snapshot valid-bounds bounding box, unit-agnostic. See
   `docs/ContextAuthServer_服务端说明.md` §8.2.)*

3. **Training scale — now formal by default (2026-07-05 SRV-9).** The default is now
   `training.epochs=100` (the formal paper magnitude), with config propagation fixed so
   the value actually reaches the trainer. `--smoke` (or `runtime.smoke=true`) shrinks
   nets/epochs/data for a fast CPU dry run (smoke epochs capped at ≤2); the test suite
   runs in smoke. On the current single-user real data absolute EERs are still not
   meaningful (see #1), but the training *budget* is no longer a documented reduction.

4. **Approximate capacity match (M2).** M2's dense width is hand-tuned to be *near* the
   M7 top-k\* MoE parameter count, not an exact FLOP/param solve. The actual parameter
   counts are recorded in each run's `metrics.json` (`param_count`,
   `active_param_count`) so the residual capacity gap is auditable.

5. **Event-level detection metrics — FIXED (2026-07-05 SRV-4).** `time_to_detect` /
   `false_alarms_per_hour` are now derived through an explicit **detection policy**
   (k-of-n / EWMA over the per-window score stream) alongside `FRR@FAR` and `FAR@FRR`
   operating points; the per-pair scores are persisted so the policy is auditable
   (`experiments/metrics.py`, `test_metrics_operating_points`). On single-user data the
   absolute values are still not deployment conclusions, but the metric machinery is no
   longer a reduction.

6. **Bootstrap protocol — FIXED (2026-07-05 SRV-3).** `pooled_bootstrap_ci` now
   implements the §18.3 primary protocol: resample **users** with replacement → rebuild
   the genuine + matched-impostor pairs → recompute the **pooled** metric each replicate;
   the M7-vs-baseline paired delta reuses the **same** user-resample index matrix
   (`user_resample_indices`) with Holm correction (`test_bootstrap_protocol`). The old
   per-user-EER-vector `bootstrap_ci` is retained only as a labelled *secondary* report.
   Statistical power is now bounded by the (here, single) real user count, not by the
   estimator.

7. **ROC curves — FIXED (2026-07-05 SRV-4).** The runner now persists the per-pair
   genuine/impostor score vectors, so `roc_curves` sweeps thresholds and draws **true ROC
   curves** per baseline instead of the former ROC-AUC bar-chart proxy
   (`reporting/plots.py`).

8. **Automatic ablation drivers.** `run_all_experiments` now writes
   `feature_ablation.csv`, `privacy_ablation.csv`, and `sensor_channel_ablation.csv`
   by default. `--skip-ablations` is only for quick debugging; paper runs should keep
   the default. (The former 8->7 mapping ablation was dropped: the scene set is now the
   identity `I0..I6`, so there is no alternative task mapping left to compare.)

9. **`k=7` == dense-all.** In the top-k sweep, `k = n_experts = 7` aggregates every
   expert (the dense-all mixture), the intended interpretation from the spec.

---

## 5. Pointers

- **Frozen build contract:** [`_BUILD_CONTRACT.md`](./_BUILD_CONTRACT.md) — package
  layout, shared schemas/interfaces, config schema, tests, build stages. Authoritative;
  implementation follows it verbatim.
- **Requirements digest / methodology:** [`_recon_spec.md`](./_recon_spec.md) (exp_prompt
  §六–§二十), [`_recon_hmog.md`](./_recon_hmog.md) (HMOG idioms mirrored for
  metrics/bootstrap), [`_recon_contract.md`](./_recon_contract.md) (exact app↔server data
  contract).
- **中文文档:** [`docs/ContextAuthServer_服务端说明.md`](../docs/ContextAuthServer_服务端说明.md)
  records the ingest contract (CANONICAL `I0..I6` + legacy `I7`/`C0..C6` compatibility)
  and its legacy task-remapping rules, strict no-text server checks, automatic ablation
  outputs, and reproducible commands.
