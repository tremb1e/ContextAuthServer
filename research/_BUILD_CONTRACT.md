# research/ FROZEN BUILD CONTRACT (authoritative — implementation agents follow VERBATIM)

> **[2026-07-03 taxonomy-evolution note]** This contract was frozen under the earlier
> `C0..C6` taxonomy. The scenario/task taxonomy has since evolved to the canonical
> **7 classes `I0..I6`** (the 8->7 task-mapping and the `recommended` vs `alt_c5_nav`
> dual-mapping mechanism were removed; old `I7` wrist -> new `I6`, old spatial-capture
> `I6` deleted). **The body below is unchanged and reflects the contract as written at
> the time.** For the current state see `docs/ContextAuthServer_服务端说明.md` and
> `research/README.md`.

Companion docs (authoritative for requirements/contract/methodology; READ them):
`_recon_spec.md` (exp_prompt §六–§二十 digest), `_recon_contract.md` (exact app↔server data contract), `_recon_hmog.md` (HMOG methodology idioms to mirror, with copy-able code).

This contract fixes the package layout, the SHARED interfaces/schemas, config, tests, runtime, and build stages so 5 sequential single-writer stages produce a COHERENT, RUNNABLE package. Do not contradict the recon files or the C0..C6 taxonomy or the leakage-column exclusions.

## 0. Runtime & dependencies
- RUNTIME python = `/home/tremb1e/miniconda3/envs/hmog_1dcnn/bin/python` (Python 3.10; the HMOG-parity env). All test/run commands use this interpreter.
- `research/requirements.txt` (pin to the env): `numpy==1.26.4`, `torch==2.4.1`, `pandas==2.2.2`, `scikit-learn==1.5.2`, `scipy==1.13.1`, `matplotlib==3.9.2`, `pyarrow==17.0.0`, `pyyaml==6.0.3`, `pytest==9.0.3`, `lz4>=4.3`. These are the RESEARCH deps ONLY — never add them to the ingest service's `app/` minimal deps.
- All code Python 3.10 compatible, full type hints, `from __future__ import annotations` where helpful. Torch runs on CPU (no CUDA assumption in code/tests).

## 1. Package layout (every file; assigned to a build Stage S1..S5)
```
research/
  __init__.py                         (S1)
  config.py                           (S1)  load_config/merge/config_hash
  requirements.txt                    (S1)
  README.md                           (S5)  short EN pointer to the中文 doc
  utils/{__init__,logging,seed,io}.py (S1)
  configs/default.yaml                (S1)
  configs/experiments/                (S5)  m0.yaml..m10.yaml + ablation_topk.yaml + ablation_privacy.yaml + ablation_features.yaml + ablation_mapping.yaml + ablation_sensor_channel.yaml
  preprocessing/{__init__,loaders,align,sessionize,windowing,feature_extractors,quality}.py  (S2)
  labeling/{__init__,interaction_states}.py    (S2)
  datasets/{__init__,builders,splits,impostors}.py  (S3)
  models/{__init__,dense,moe,routing,losses}.py     (S3)
  experiments/{__init__,metrics,bootstrap,trainer,evaluator,runner}.py  (S4)
  reporting/{__init__,plots,tables,report}.py       (S4)
  scripts/{__init__,generate_synthetic_data,run_preprocess,build_datasets,run_experiment,run_all_experiments,make_report,export_artifact_bundle}.py  (S5)
  tests/{__init__,conftest,test_loaders_ingest_roundtrip,test_preprocessing_alignment,test_sensor_features_three_channel,test_labeling_functions,test_dataset_splits,test_models_moe_topk,test_training_smoke,test_topk_sweep_smoke,test_report_generation,test_no_leakage_columns,test_privacy_sanity}.py  (S5)
```
Data dirs (created at runtime, gitignored): `data/synthetic/`, `data/processed/`, `data/datasets/`, `data/results/{run_id}/`. All research code is a package under `ContextAuthServer/`; run as `python -m research.scripts.<name>` and `pytest research/tests`.

## 2. Shared constants (define ONCE in `research/__init__.py`, import everywhere)
```python
SCENARIOS = ["C0","C1","C2","C3","C4","C5","C6"]           # 7 experts, ordinal index = position
SCENARIO_NAMES = {"C0":"QUIESCENT_VIEWING","C1":"KEYBOARD_TEXT_ENTRY","C2":"CONTINUOUS_SCROLLING",
  "C3":"DISCRETE_NAVIGATION","C4":"STRUCTURED_CONTROL","C5":"MEDIA_PLAYBACK","C6":"CANVAS_HIGH_MOTION"}
N_SCENARIOS = 7
LEAKAGE_COLUMNS = {"estimated_context_category","game_like_score","viewIdResourceName","coarse_orientation"}
SENSOR_TYPES = ["ACCELEROMETER","GYROSCOPE","MAGNETIC_FIELD"]   # 3-channel, fully equal
```
LEAKAGE_COLUMNS are NEVER computed into features and NEVER used by weak labels. The IMU-derived landscape boolean (from accel(+mag) in feature_extractors) IS allowed and is named `orient_landscape` (NOT `coarse_orientation`).

## 3. Canonical schemas (the cross-layer contract)
### 3a. Window record (dict; one per window) — produced by windowing+features+labeling, stored to parquet
Keys: `device_id:str, session_id:str, day_id:str, window_id:str, user_id:str, package_bucket:str,
start_elapsed_ns:int, end_elapsed_ns:int, start_wall_ms:int, end_wall_ms:int,
<feature columns...> (flat float columns, names from feature_extractors),
weak_label_probs:list[float](len7), weak_label_top1:str, weak_label_topk:list[str],
weak_label_confidence:float, weak_label_entropy:float, quality_flags:list[str], task_category:str|None (gold label if BUILTIN_TASK else None)`.
Stored as parquet via pandas; list/dict-valued columns JSON-encoded into `*_json` string columns when needed for parquet friendliness (`weak_label_probs_json`, `quality_flags_json`). Keep a parallel flat `weak_label_top1` etc.

### 3b. Feature columns — decoupled via manifest (CRITICAL anti-drift rule)
`feature_extractors.build_feature_columns(feature_mode:str) -> list[str]` returns the ORDERED feature column names for a mode. `feature_extractors.extract_window_features(imu_df, events, nodes_snapshots, prev_snapshot, package_bucket) -> dict[str,float]` returns a dict keyed by those columns. Families (3-channel parity required):
- IMU per channel∈{acc,gyro,mag} per axis∈{x,y,z}: `{ch}_{ax}_mean/std/min/max/rms/energy/zcr/jerk/skew/kurt` and freq `{ch}_{ax}_domfreq/speccentroid/specentropy/band0_3/band3_8/band8_15`; magnitude `{ch}_mag_mean/std/energy`. Orientation: `orient_pitch_mean/pitch_std/roll_mean/roll_std/heading_stability/landscape` (landscape = IMU-derived bool, ALLOWED). Cross-channel corr `corr_acc_gyro/acc_mag/gyro_mag`. `motion_energy_low/mid/high`, `gyro_burst_count`, per-channel `{ch}_sample_count/{ch}_missing`.
- Events: `evt_click/longclick/scroll/textchanged/focus/windowstate/windowcontent_count`, `evt_rate`, `evt_entropy`.
- UI: `ui_node_count_mean/max`, `ui_max_depth`, `ui_clickable/editable/scrollable/focusable_count`, `ui_editable_ratio/scrollable_ratio`, `ui_checked/selected_count`, `ui_surface_like`, `ui_webview/list/scroll_indicator`, `ui_form_like_control_count`, `ui_bounds_occupancy`, `ui_stable_ms`, `ui_treediff_nodedelta/categoryl1/boundsl1/hashchanged`.
- Package (ONLY in modes that include package): `pkg_bucket_hash` (a small integer hash of package_bucket, float-encoded). `feature_mode` in {`sensor_only`,`ui_sensor`,`ui_sensor_no_package`,`package_only`,`ui_only`,`privacy_coarse_ui`}; `no_package`/`_no_package` modes MUST exclude all `pkg_*` columns; `sensor_only` = only IMU cols; `ui_only` = UI+event; `package_only` = only `pkg_*`. Missing channel → its `{ch}_missing`=1.0 and its feature cells filled 0.0 (with the missing flag set, never silent).
Minimal-working-version: a REDUCED but representative feature set is acceptable as long as (i) 3-channel parity holds, (ii) all families above are present at least in reduced form, (iii) NO leakage column appears. Document any reduction in `research/README.md`.
`build_feature_manifest(feature_mode) -> dict{feature_columns:list[str], package_columns:list[str], leakage_free:True}`. Datasets write this to `feature_manifest.json`; models read `input_dim = len(feature_columns)` from it — NEVER hardcode input_dim.

### 3c. Weak-label output — `labeling.interaction_states.weak_label(features:dict, temperature:float=1.0) -> dict`
Returns `{"probs":np.ndarray(7), "scores":np.ndarray(7), "confidence":float, "entropy":float, "fired_rules":list[str], "top1":str, "topk":list[str]}`. Multiple additive/subtractive score-based LFs per class (NOT single if-else) using ONLY non-leakage features (§4 of _recon_spec). `confidence = clip(top1_prob - top4_prob, 0, 1)`; low-confidence if max prob<0.35 or confidence<0.10. `topk(k)` helper returns top-k scenario ids.

### 3d. split_manifest.json (datasets) — must include
`{protocol, feature_mode, users, devices, sessions, days, package_buckets, n_windows_{train,val,test}, weak_label_distribution, n_genuine_pairs, n_impostor_pairs, leakage_check:{no_session_leak:bool,no_day_leak:bool,no_app_leak:bool,enroll_query_sessions_disjoint:bool,impostor_pool_user_disjoint:bool}, kstar_selection_split:"val"}`. Build asserts every leakage_check True (raise if not).

## 4. Model interfaces (`models/`, torch.nn.Module)
- `DenseAuthenticator(input_dim:int, hidden_dims:list[int]=(256,128), embedding_dim:int=128, n_users:int=..., dropout:float=0.1, layer_norm:bool=False)`; `forward(x:Tensor[B,input_dim]) -> {"embedding":Tensor[B,emb], "user_logits":Tensor[B,n_users]}`.
- `MoEAuthenticator(input_dim, n_experts:int=7, top_k:int=2, expert_hidden:list[int]=(128,), embedding_dim:int=128, n_users:int=..., router:str="learned", dropout:float=0.1)`; `forward(x, weak_probs:Tensor[B,7]|None=None, ids:Tensor[B]|None=None) -> {"embedding","user_logits","router_logits":[B,7],"router_probs":[B,7],"topk_indices":[B,k],"gate_weights":[B,7],"active_experts":float}`. top-k gating: keep top_k experts, renormalize their weights, zero others; k∈{1..7} all valid (k=7 = dense-all).
- `routing.py`: `build_router(kind, input_dim, n_experts)` → module with `forward(x, weak_probs, ids)->router_logits[B,7]`. kinds: `learned` (MLP on x), `fixed_rule` (log of weak_probs, no grad), `random` (fixed-seed random logits), `hash` (hash ids→one-hot), `package_only` (learned on pkg cols only — caller passes sliced x or pkg feature). Keep simple.
- `losses.py`: `auth_loss(embeddings, user_labels, kind="triplet"|"ce_proto", margin=0.3)`; `kl_weak(router_logprobs:Tensor[B,7], weak_probs:Tensor[B,7], confidence:Tensor[B])`; `load_balance(router_probs:Tensor[B,7])`; `temporal_smoothness(router_probs, session_ids)`; `total_loss(outputs, batch, cfg)->(loss, parts:dict)`. Small default lambda_balance (0.005). Minimal-working: `auth_loss` may use user-classification cross-entropy + prototype-cosine at eval; document choice.

## 5. Metrics & stats (`experiments/`, MIRROR HMOG `_recon_hmog.md` verbatim where noted)
- `metrics.compute_eer_threshold(labels, scores) -> (eer:float, thr:float)` — copy _recon_hmog §1b (roc_curve+brentq, argmin|fpr-fnr| fallback, nan-guard). label 1=genuine.
- `metrics.far_frr_at_threshold(labels, scores, thr)`, `metrics.compute_eer_auc(labels, scores)->{eer,roc_auc,pr_auc}`.
- `metrics.per_user_eer(...)`, `metrics.per_scene_eer(...)`, `metrics.time_to_detect(...)`, `metrics.false_alarms_per_hour(...)` (event-level; may be minimal, documented).
- `bootstrap.bootstrap_ci(values, n_boot=1000, seed=0)->(mean,lo,hi)` — copy _recon_hmog §2 (resample per-user EER vector). `bootstrap.holm_correction(pvals)->adjusted` — copy §3a. `bootstrap.paired_delta(a,b,seed)->{delta_mean,ci,p_wilcoxon,cohend,win_rate}` — §4 (wilcoxon + sign-test fallback).
- Bootstrap protocol: resample USERS → rebuild genuine/impostor pairs → recompute POOLED metric; impostors stay matched. Minimal-working may resample per-user EER vectors (documented) but MUST be by-user, never by-window.

## 6. Experiment runner (`experiments/runner.py`) & outputs
- `run_experiment(cfg:dict, data_dir:str, out_dir:str) -> run_dir`: trains (deterministic seed, early stop, best ckpt), evaluates with prototype/cosine under enroll/query-session-disjoint, writes `data/results/{run_id}/`: `config.yaml, metrics.json, metrics.csv, per_user_metrics.csv, per_scene_metrics.csv, expert_utilization.csv, expert_scene_matrix.csv, model.pt, logs/train.jsonl, run_context.json`.
- `run_topk_sweep(cfg, data_dir, out_dir) -> topk_sweep.csv` (k∈1..7, each row: k, eer, roc_auc, per_scene_eer, matched_impostor_eer, avg_active_experts, latency_ms, param_count). `select_kstar_pareto(topk_sweep, on="val") -> k*` (smallest-cost k whose EER not sig. worse than best; frozen; test once). Emit Pareto data.
- `run_all_experiments(cfg, data_dir, out_dir)`: M0..M10 + topk sweep; each writes its run dir; a manifest `data/results/runs_index.json`.
- Training SMOKE-fast: default `epochs=1..2`, tiny nets, tiny synthetic — the whole `pytest research/tests` must finish in a couple minutes on CPU. A `smoke:true` config flag shrinks everything.

## 7. Config schema (`configs/default.yaml` keys) & experiment configs
default.yaml (top-level keys): `seed:42`, `runtime:{smoke:false, device:"cpu"}`, `preprocess:{window_size_sec:5, stride_sec:1, gap_min:10}`, `features:{mode:"ui_sensor"}`, `labeling:{temperature:1.0, low_conf_prob:0.35, low_conf_margin:0.10}`, `dataset:{protocol:"leave_session_out", enroll_query_disjoint:true}`, `model:{kind:"moe", top_k:2, n_experts:7, embedding_dim:128, expert_hidden:[128], router:"learned"}`, `train:{epochs:2, lr:1e-3, batch_size:64, early_stop_patience:3}`, `loss:{lambda_scene:1.0, lambda_balance:0.005, lambda_smooth:0.1, auth_kind:"ce_proto"}`, `topk:{sweep:[1,2,3,4,5,6,7], select_on:"val"}`, `report:{outdir:"data/results"}`.
`configs/experiments/mN.yaml` each is a thin override of default.yaml matching M0..M10 in _recon_spec §7 (e.g. m0: model.kind=dense, features.mode=sensor_only; m7: moe learned weak, top_k=k*; m8: features.mode=ui_sensor_no_package; m3: router=package_only; m4: router=fixed_rule + top_k=1; m5: router=fixed_rule top_k=k*; m6: moe no KL (loss.lambda_scene=0); m9: router=random; m10: router=hash; m2: dense capacity-matched). Ablation yamls override the swept dimension.

## 8. Scripts (each: argparse, --help, `python -m research.scripts.X`)
`generate_synthetic_data.py --users --days --sessions-per-day --out --seed [--emit-envelopes]` (produces raw batch JSONs under out/, and with --emit-envelopes writes LZ4_FRAME+JSON 8-field envelopes that pass `/api/v1/ingest`; records satisfy the real schema incl C0..C6 + drop-all-text + encryption none). `run_preprocess.py --input --output --window-size-sec --stride-sec`. `build_datasets.py --input --output --protocol [--feature-mode]`. `run_experiment.py --config --data --out`. `run_all_experiments.py --config --data --out`. `make_report.py --results --out`. `export_artifact_bundle.py --out`.

## 9. Tests (`research/tests/`, pytest; run with the hmog_1dcnn python). conftest builds a TINY synthetic dataset fixture (few users/days/sessions) once.
Files & key tests (map to _recon_spec §15): `test_loaders_ingest_roundtrip` (read raw+envelope), `test_preprocessing_alignment` (elapsed sort/gap sessionize/window counts), `test_sensor_features_three_channel` (acc/gyro/mag columns present & symmetric; missing channel→flag), `test_labeling_functions` (per-class synthetic window top1 correct; mixed topk contains expected; asserts no leakage feature used), `test_dataset_splits` (no session/day/app leakage; matched impostor; split_manifest leakage_check all True; enroll/query disjoint), `test_models_moe_topk` (forward for all k∈1..7; gate weights sum→1 over active; param/active counts), `test_training_smoke` (train M0 & M7 1 epoch → metrics.json exists), `test_topk_sweep_smoke` (k∈1..7 → topk_sweep.csv + k* selected), `test_report_generation` (report.md + a plot PDF+PNG produced; assert figure has no title & no CJK chars in text), `test_no_leakage_columns` (dataset feature columns ∩ LEAKAGE_COLUMNS == ∅), `test_privacy_sanity` (no on-disk artifact contains raw text/placeholder; synthetic records satisfy redaction_applied:true/encryption:none/compression:lz4_frame/task∈C0..C6). All must pass; keep fast (smoke config).

## 10. Minimal-working-version scope (per §十七 — document each in research/README.md)
Full: loaders, preprocessing (align/sessionize/windowing), 3-channel IMU + UI features, 7-class weak labeling, 4 split protocols + combined + matched-impostor, Dense + MoE(E=7,top-k 1..7) + 5 routers + losses, runner + metrics(EER/AUC/per-user/per-scene) + by-user bootstrap + Holm, topk sweep + Pareto k*, synthetic generator (+envelopes), reporting (中文 report + ≥ the listed plots + latex tables), all tests. Minimal (documented limits): reduced-but-representative feature families; event-level TTD/false-alarms-per-hour basic; capacity-match (M2) approximate (record param counts); frequency features via numpy rfft; plots cover the required list but may share styling helper. NO TODO stubs in core paths — everything imports and runs end-to-end on synthetic.

## 11. Build stages (SEQUENTIAL, single-writer, DISJOINT files)
- S1 foundation+IO: `__init__.py, config.py, requirements.txt, utils/*, configs/default.yaml, scripts/generate_synthetic_data.py, preprocessing/loaders.py`. Deliverable: `python -m research.scripts.generate_synthetic_data --users 3 --days 2 --sessions-per-day 2 --out data/synthetic --seed 42` runs; loaders read it back.
- S2 preprocessing+labeling: `preprocessing/{align,sessionize,windowing,feature_extractors,quality}.py, labeling/interaction_states.py, scripts/run_preprocess.py`. Deliverable: run_preprocess produces windows parquet with feature columns + weak labels.
- S3 datasets+models: `datasets/{builders,splits,impostors}.py, models/{dense,moe,routing,losses}.py, scripts/build_datasets.py`. Deliverable: build_datasets produces train/val/test parquet + split_manifest + feature_manifest; models forward on a batch.
- S4 experiments+reporting: `experiments/{metrics,bootstrap,trainer,evaluator,runner}.py, reporting/{plots,tables,report}.py, scripts/{run_experiment,run_all_experiments,make_report,export_artifact_bundle}.py`. Deliverable: run_experiment (smoke) → run dir; make_report → report.md + a plot.
- S5 configs+tests+readme: `configs/experiments/*.yaml, research/README.md, tests/*`. Deliverable: `pytest research/tests` green.
Each stage: READ `_BUILD_CONTRACT.md` + `_recon_*.md` + the ACTUAL files from prior stages; WRITE only your stage's files; write each file with its own Write call (incremental, avoid long no-tool gaps); keep files focused; add type hints + docstrings; do a quick self-smoke of your deliverable with the hmog_1dcnn python before finishing and report results.
