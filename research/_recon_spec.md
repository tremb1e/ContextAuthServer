# research/ Engineering Recon Spec (derived from exp_prompt.md §六–§二十)

> **[2026-07-03/07-04 taxonomy-evolution note]** The scenario/task taxonomy has since evolved
> to the canonical **7 classes `I0..I6`** (the former `C0..C6` scene ids, the 8->7
> task-mapping, and the `recommended` vs `alt_c5_nav` dual-mapping mechanism were all
> removed; old `I7` wrist -> new `I6`, and the old spatial-capture `I6` was deleted).
> **The body below is unchanged and reflects the contract as written at the time.** For
> the current state see `docs/ContextAuthServer_服务端说明.md` and `research/README.md`;
> §8 of that doc records the 2026-07-04 research-layer weak-labeling / feature root-cause
> fixes (surface-unit fix, I5/I6 motion+touch gating, ScrollView container category).

> READ-ONLY recon. Ground-truth: 7 scenarios = 7 experts (C0..C6), top-k swept 1..7 (NOT fixed 3),
> learned weakly-supervised router, `encryption:"none"` (LZ4+SHA256+TLS), device_id-only, ingest-only
> server (no ML yet), 9-dim IMU base like HMOG. All ML built new under `ContextAuthServer/research/`.
> HMOG (`/data/paper/sp/hmog_exp`) is the methodology gold standard.

---

## 0. Scenario taxonomy (7 classes = 7 experts)
- C0 QUIESCENT_VIEWING, C1 KEYBOARD_TEXT_ENTRY, C2 CONTINUOUS_SCROLLING, C3 DISCRETE_NAVIGATION,
  C4 STRUCTURED_CONTROL, C5 MEDIA_PLAYBACK, C6 CANVAS_HIGH_MOTION.

## 1. Module list + responsibilities
- **preprocessing/** (`loaders, align, sessionize, windowing, feature_extractors, quality`): read ingest/synthetic data → parquet + reports.
- **labeling/** (`interaction_states.py`): score-based weak LFs → 7-class probs/scores/confidence/entropy/fired_rules.
- **datasets/** (`builders, splits, impostors`): 4 split protocols + combined + matched-impostor; emit train/val/test parquet + split_manifest.json + feature_manifest.json.
- **models/** (`dense, moe, routing, losses`): DenseAuthenticator; MoEAuthenticator (E=7, top-k 1..7); 5 router variants; L_auth + KL + load-balance + temporal-smoothness.
- **experiments/** (`trainer, evaluator, runner, metrics, bootstrap`): deterministic training, metrics, by-user bootstrap+Holm, topk sweep + Pareto k*.
- **reporting/** (`plots.py`): matplotlib+numpy ONLY publication figs; Chinese report + LaTeX tables via make_report.
- **utils/** (`logging.py` JSONL, run_context env snapshot, config hashing, seeds).
- **configs/**: default.yaml + experiments/m0..m10 + ablation_topk/ablation_privacy/ablation_features.
- **scripts/**: generate_synthetic_data, run_preprocess, build_datasets, run_experiment, run_all_experiments, make_report, export_artifact_bundle.
- **tests/**: 10 pytest items + privacy sanity + no-leakage (§15).
- Outputs tree: `data/processed/`, `data/datasets/`, `data/results/{run_id}/`.

## 2. Preprocessing (§六)
- **Timestamps/sessionize (6.1):** sort by `elapsed` (timestamp_elapsed_nanos/base_elapsed_nanos) within device; wall_time for day/session grouping; detect clock jump; log time gaps + service restarts. Session cut on gap>10min OR day boundary OR service restart.
- **Windowing (6.2):** default window_size_sec=5, stride_sec=1 (4s overlap); window_size hyper-grid {0.5,1,2,3,5}s searched on **tuning subset only** (no test leakage, HMOG-style).
- Each window aggregates: event + UI-tree + UI-stability/diff + 3-ch IMU + package + quality flags + weak labels.

## 3. Window feature schema (§6.3–6.5)
- **IMU base 9-dim** `[acc_x,y,z, gyro_x,y,z, mag_x,y,z]` (accel/gyro/mag FULLY equal); per channel/axis in window:
  - **Time:** mean, std, min, max, RMS, energy, ZCR, jerk (diff energy), skewness, kurtosis.
  - **Freq:** dominant freq, spectral centroid, spectral entropy, band-energy ratios (0–3/3–8/8–15 Hz).
  - **Orientation:** pitch/roll from accel(+mag) + their std, magnetometer heading stability; **IMU-derived landscape/portrait boolean (LEGAL feature)**.
  - Cross-axis/cross-channel correlation; motion-energy bins low/mid/high (per-device robust percentiles); gyro burst count; sample_count; missingness. Any missing channel → `missing` flag (never silent zero-fill).
- **Events:** per-type counts, event_rate, click/long_click/scroll/text_changed/focus/window_state/window_content counts, event entropy.
- **UI:** node_count mean/max, max_depth, depth histogram, class_category histogram, clickable/editable/scrollable/focusable counts+ratios, checked/selected counts, surface_like_large_region, webview/list/scroll_container indicators, form_like_control_count, editable_node_count, bounds_grid occupancy histogram, UI tree hash, stable duration; **tree diff** between snapshots: node_count_delta, category_hist_l1, bounds_grid_l1, structural_hash_changed.
- **Package:** package bucket / package_category (**no-package mode must FULLY remove**).
- **Window row (6.5):** device_id, session_id, day_id, window_id, start/end_elapsed_ns, start/end_wall_time_ms, feature_vector/columns, feature_json, weak_label_probs_json, weak_label_top1, weak_label_topk, weak_label_confidence, weak_label_entropy, quality_flags_json.
- **quality_flags:** missing_sensor, missing_ui, low_record_count, service_restart, app_transition_window, time_gap, privacy_violation, low_confidence_label.

## 4. Weak labeling (§七) — 7-class score-based
- Output `{probs(7), scores(7), confidence, entropy, fired_rules}`. Multiple additive/subtractive LFs per class (**NOT single if-else**) → **temperature-scaled softmax** (default T=1.0).
- `confidence = clip(top1_prob − top4_prob, 0, 1)`. If max prob<0.35 OR confidence<0.10 → **low-confidence**: still counts in L_auth but router weak-supervision reduced/skipped.
- **Per-class scoring cues:**
  - C1 KEYBOARD: + text_changed>0, + focused editable, + editable_node_count>0, + IME visible; − no editable node.
  - C2 SCROLLING: + scroll_count>0, + scrollable_node_count>0, + list/webview/scroll container, + consistent bounds shift; − text_changed>0, − large nav diff.
  - C3 NAVIGATION: + click/long_click>0, + window_state_changed>0, + large UI-tree diff; − text_changed>0, − sustained high scroll.
  - C4 STRUCTURED_CONTROL: + checkbox/radio/switch/spinner/seekbar/date-picker, + editable_node_count≥2 with focus switching, + small/medium diff after click, + checked/selected changes; − pure scroll, − pure media/canvas.
  - C5 MEDIA_PLAYBACK: + large surface-like region, + UI stable>8s, + low event rate, + low/mid motion, + **landscape (IMU-derived bool)**; − high motion/gyro, − text_changed>0, − high scroll.
  - C6 CANVAS_HIGH_MOTION: + large surface-like region, + low UI node count, + high accel/gyro/mag energy, + orientation/motion burst, + high touch density, + low semantic event rate; − low motion & stable UI.
  - C0 QUIESCENT: + low event rate, + stable UI tree, + low/mid motion, + no large surface; − text_changed>0, − scroll>0, − click>0, − high motion.
- **LEAKAGE COLUMNS THAT MUST BE EXCLUDED (from features AND scoring):** `estimated_context_category`, `game_like_score`, `viewIdResourceName` (package-fingerprintable), **raw client-uploaded `coarse_orientation`**. ALLOWED: IMU-derived landscape bool from accel(+mag) (legal, our own signal, not task-bound label).
- Test: per class a synthetic window → top1 correct; mixed window → top-k contains expected class.

## 5. Dataset splits & protocols (§八)
- **NO random window split; adjacent overlapping windows must not split across train/test.**
1. **leave_session_out** — split by session; same session never in both train & test.
2. **leave_day_out** — early days train, later days test (temporal drift).
3. **leave_app_out** — hold out package bucket/category (prove not memorizing app).
4. **matched_impostor** — per genuine test window sample impostor windows from OTHER users with matching weak_label_top1/top-k (and package_category if available). **Impostor-pool users must be user-level DISJOINT from the tested genuine user on the training identity set** (aligns one-class); asserted in split_manifest leakage check.
5. **Combined / strictest (USENIX main col):** at least `leave_day_out ∩ leave_app_out` (cross-time AND cross-app; or nested leave_user_and_app_out). Main conclusions must hold here. report.md orders easy→hardest (guard against single-axis pass but combined collapse).
- **Auth task (8.2):** encoder→embedding; enrollment windows→user prototype; query vs claimed prototype cosine similarity; genuine=same user, impostor=other users. Train may use user-classification head; eval uses prototype/cosine (+optional Mahalanobis reject threshold).
- **ENROLL/QUERY SESSIONS MUST BE DISJOINT (critical, prevents EER虚低):** per tested user, prototype enrollment windows and query windows come from disjoint sessions (prefer cross-day); a genuine pair valid only if enroll-session ≠ query-session — else 5s/1s overlapping windows create near-duplicates that inflate genuine scores. split_manifest must record+assert; all metrics/bootstrap computed under this constraint.
- **one-class protocol:** train only target genuine; scaler/stats fit on train only; impostor pool user-level disjoint.
- **feature modes (8.3):** sensor_only / ui_sensor / ui_sensor_no_package / package_only / ui_only / privacy_coarse_ui.
- **Outputs (8.4):** `data/datasets/{name}/{train,val,test}.parquet` + split_manifest.json (users, devices, sessions, days, package buckets, window counts, weak-label dist, genuine/impostor pair counts, leakage-check results incl. no session/day/app leakage assertions) + feature_manifest.json.

## 6. Models (§九)
- **DenseAuthenticator (9.1):** MLP encoder; input_dim configurable; hidden dims configurable; embedding dim default 128; dropout; optional layer norm; user-classification head at train; embedding for prototype verification at eval.
- **MoEAuthenticator E=7 (9.2):** 7 MLP-encoder experts; router → 7 expert logits; **top-k sparse gating, k∈{1..7} configurable**; top-k weights normalized; weighted fusion of expert embeddings; classification head on fused embedding.
  - **Router variants:** learned / fixed-rule (uses weak_label_probs) / random / hash (window_id/session_id) / package-only.
  - **M7 formal (weak_supervised_moe):** input UI+IMU(+optional package); learned router; top-k* (from §十); router KL-supervised by weak-label probs; loss = L_auth + KL-weak + load-balance(small wt) + temporal-smoothness.
- **L_auth (9.3):** metric/contrastive/one-class (triplet w/ configurable margin | NT-Xent | InfoNCE) + user-classification aux head; eval cosine+prototype; reject via Mahalanobis threshold. All hyperparams configurable + swept in ablation.

## 7. Baselines M0–M10 (§9.4, one line each)
- **M0** sensor_only_dense — 3-ch IMU only, dense.
- **M1** ui_sensor_dense — UI+IMU, dense.
- **M2** capacity_matched_dense — dense with params/FLOPs ≈ M7 top-k* MoE (record param count; controls capacity confound).
- **M3** package_only_router — router uses ONLY package features (tests package-name confound).
- **M4** fixed_rule_top1 — fixed router = weak_label_probs top1, router untrained.
- **M5** fixed_rule_topk* — fixed router = weak_label_probs top-k*, untrained (STRONG baseline, don't cripple; k* = formal method's).
- **M6** auth_only_moe — same arch/top-k* as M7 but NO weak-label KL; router learned from auth loss + load-balance + temporal-smoothness only.
- **M7** weak_moe — formal method (top-k*).
- **M8** weak_moe_no_package — M7 minus package features (proves no package dependence).
- **M9** random_moe — random top-k* routing, fixed seed.
- **M10** hash_moe — hash window_id/session_id → experts.

## 8. Ablations (§9.5)
- top_k ∈ {1..7} (full sweep, §十); no_ui/no_sensor/no_package/no_tree_diff/no_temporal_smoothness/no_load_balance; weak_label_confidence_threshold ∈ {0.0,0.2,0.4,0.6}; privacy_coarse_bounds/no_resource_id/coarse_widget_category_only; `--mapping ∈ {recommended, alt_c5_nav}` (§三 8→7 mapping); single-channel no_accel/no_gyro/no_magnetometer (validate magnetometer contribution).

## 9. top-k selection (§十, RQ5)
1. Fix formal-method hyperparams; train/eval once per k∈{1..7}; **k=7 = dense-all (aggregate all experts)**.
2. Per k record accuracy (EER, ROC-AUC, per-scene EER, matched-impostor EER) + cost (avg active experts, per-window latency ms, FLOPs, param count, energy proxy).
3. **k* chosen ON VALIDATION/tuning subset ONLY, then FROZEN** (same discipline as 6.2 window search): plot EER-vs-latency/active-experts Pareto (`topk_eer_latency_pareto`); pick smallest-cost k whose EER not significantly worse than best (bootstrap CI); test evaluated with frozen k* **ONCE**. k* selection split recorded in split_manifest + run_context.
4. Formal method + M5/M6 (anything using k) all use k*; report k* provenance + ±1 sensitivity + why not preset.

## 10. RQ map (§十一)
RQ1 UI structure helps? M0/M1/M7. RQ2 MoE vs dense (capacity-controlled)? M1/M2/M7. RQ3 learned weak routing vs fixed rule? M4/M5/M7. RQ4 weak-supervision vs auth-only MoE? M6/M7. RQ5 top-k / k*? sweep 1..7 + Pareto. RQ6 app/package dependence? M3/M7/M8 + leave_app_out. RQ7 privacy/cost/deploy? record rate, batch size, LZ4 ratio, upload freq, server time, inference latency, no-text/privacy test, per-redaction-level perf.

## 11. Metrics (§9.7)
- **Main:** EER, ROC-AUC, PR-AUC, FRR@FAR=1%, FRR@FAR=5%, FAR@FRR=5%, time-to-detect, per-user EER, per-scene EER, leave-app-out EER, matched-impostor EER.
- **Event-level (HMOG):** restricted-mean time-to-detect, false-alarms-per-hour, attack-detection-rate; detection策略 raw / k-of-n / EWMA chosen on validation, fixed on test.
- **Router/expert analysis:** weak-label top1/top-k dist, router entropy, expert-utilization entropy, per-scene expert-activation matrix, KL(router‖weak label), expert specialization score. **Stratified by protocol:** additionally under leave_app_out report weak-label dist drift, KL, per-scene activation (OOD routing robustness; avoid misattributing leave-app-out drop to encoder vs router weak-supervision failure).
- **Statistics:** by-user bootstrap 95% CI; M7-vs-each-baseline paired delta; **Holm** multiple-comparison correction; scipy p-value if available else bootstrap delta CI; effect size + per-user/per-scene win rate; **main conclusions heldout-users only**.
- **Bootstrap protocol (§18.3, fixed):** resample USERS with replacement → rebuild genuine/impostor pairs → recompute POOLED metrics (NOT mean of per-user EER); paired delta on same resample index; impostors stay matched (scene/package) within each bootstrap.
  - _[2026-07-05 实现注记 · 不改契约原文]_ 本条已由 `experiments/bootstrap.py::pooled_bootstrap_ci` 落地（SRV-3）；README 里 minimal-working「per-user EER vector」向量法 documented-deviation 已退役、仅作 labelled secondary report 保留。RandomRouter「fixed-seed random logits」在 SRV-8 逐窗口实现下仍满足本规约，无需改。
- **Per-run outputs (9.8):** config.yaml, metrics.json, metrics.csv, per_user_metrics.csv, per_scene_metrics.csv, expert_utilization.csv, expert_scene_matrix.csv, topk_sweep.csv, model.pt, logs/train.jsonl, run_context.json.

## 12. Reporting & plots (§十二)
- JSONL structured logging (UTC per-line) across all stages; run_context env snapshot (python/torch/cuda ver, git commit, config hash, seed, hardware). Log any budget-driven coverage cuts explicitly (no silent truncation).
- Chinese markdown per experiment (conclusion-first). Global `make_report.py` → `data/results/report.md` (中文) + `latex_tables.tex` + `plots/`, organized RQ1–RQ7 with Executive Summary, Dataset Summary, Expert Specialization, Limitations, Reproducibility.
- **Publication plots (`reporting/plots.py`): matplotlib + numpy ONLY (NO seaborn / NO pandas plotting). Times New Roman, large font, LaTeX mathtext for symbols ($k$, $\text{EER}$, $\mathrm{FAR}$, $p(\text{scene}\mid x)$, $\lambda_{\text{scene}}$). NO Chinese in figures. NO titles. No overlap. Export PDF + PNG @300dpi. Each fig gets a Chinese caption doc.**
- **Required plot list (≥):** eer_bar, roc_curves, topk_ablation, topk_eer_latency_pareto, per_scene_eer, expert_utilization, expert_scene_heatmap, weak_label_distribution, package_ablation, privacy_ablation, mapping_ablation (recommended vs alt_c5_nav), sensor_channel_ablation (accel/gyro/mag).

## 13. Synthetic data generator (§十三)
- `scripts/generate_synthetic_data.py --users --days --sessions-per-day --out --seed [--emit-envelopes]`. Simulates: multi-user/day/session, 7 interaction states, multi package buckets, UI snapshots, AccessibilityEvents, RAW 3-ch sensor_samples, user-specific behavior, impostors, label noise, mixed states, missing streams, app transitions. Records MUST satisfy real §零 schema (LZ4 envelope, 8 fields, drop-all-text, C0..C6). `--emit-envelopes` → LZ4 envelopes ingestable by `/api/v1/ingest` → land in `data/paper/devices/`. **Declared: synthetic validates pipeline ONLY, cannot replace real multi-user conclusions.**

## 14. Privacy/ethics (§十四) — for docs + sanity tests
- Consent; visible collection status; pause/stop/delete-local. App collects NO text/screenshot/OCR/notification text/clipboard/contentDescription/hintText/raw input/keystroke timing/touch coords/trajectories (drop-all-text). App redaction + server structural contract (§5.4). HTTPS/TLS; `encryption:"none"`; SHA-256 over compressed bytes; device_id = HMAC(fixed salt, ANDROID_ID) (no IMEI/serial/MAC). No real keys in repo; .env.example placeholders; data/ gitignored; IRB placeholder. Threat model: network attacker, honest-but-curious operator, app-log leak, accidental text leak, app/package confound, weak-label noise. **Forbidden:** stealth mode, hidden collection, bypassing consent, covert persistence, RCE, collecting text/screenshot/clipboard, malware-like behavior.

## 15. Tests (§十五) — enumerate ALL
**15.1 experiment pytest (10):**
1. `test_loaders_ingest_roundtrip` — correctly reads ingest JSON (incl. by_category symlinks) + synthetic envelopes.
2. `test_preprocessing_alignment` — elapsed sort/clock-jump, gap sessionization, windowing correct.
3. `test_sensor_features_three_channel` — accel/gyro/magnetometer features complete & equal; missing channel → missing flag.
4. `test_labeling_functions` — 7 classes each: synthetic window top1 correct; mixed window top-k contains expected; NO leakage cols used.
5. `test_dataset_splits` — leave_session_out no session leakage; leave_day_out/leave_app_out valid; matched_impostor matches weak labels; split_manifest leakage check passes.
6. `test_models_moe_topk` — MoE forward for all k∈{1..7}; top-k weights normalized; param count / active-expert count recorded correctly.
7. `test_training_smoke` — train M0 & M7 1 epoch on synthetic → produce metrics.json.
8. `test_topk_sweep_smoke` — run all k∈{1..7} → produce topk_sweep.csv + Pareto data.
9. `test_report_generation` — sample results → report.md + figs (plot script runs, produces PDF+PNG, NO Chinese, NO titles).
10. `test_no_leakage_columns` — assert training features exclude estimated_context_category / game_like_score / viewIdResourceName / coarse_orientation.
**15.2 privacy sanity assertions (one test per constraint):**
- No text raw / placeholder sentinel in ANY pipeline on-disk artifact.
- Forbidden-field presence → test FAILS.
- Synthetic/real records satisfy server schema contract (redaction_applied:true, encryption:none, compression:lz4_frame, 7-class task contract).
**15.3 Android:** reuse §4.6, adapt to 7 scenarios incl. "task count = 7" assertion. All must be green (pytest + `:android-app:testDebugUnitTest`).

## 16. Reproducible commands (§十六)
`generate_synthetic_data --users 20 --days 3 --sessions-per-day 4 --out data/synthetic --seed 42` (+`--emit-envelopes` for full-chain) → `run_preprocess --input data/synthetic --output data/processed --window-size-sec 5 --stride-sec 1` → `build_datasets --input data/processed --output data/datasets --protocol leave_session_out` → `run_all_experiments --config research/configs/default.yaml --data data/datasets --out data/results` (M0..M10 + topk 1..7) → `make_report --results data/results --out data/results/report.md`; `pytest tests`.

## 17. Implementation order (§十七) + minimal-working-version guidance
Order: (1) research/ skeleton + deps; (2) loaders + synthetic gen; (3) preprocessing (align/sessionize/window/3-ch IMU+UI/quality); (4) 7-class weak labeling (drop leakage); (5) datasets + 4 protocols + matched-impostor; (6) Dense + MoE (E=7, top-k 1..7) + 4 routers + losses; (7) runner/metrics/bootstrap; (8) topk sweep + Pareto k*; (9) reporting + pub plots; (10) all functional tests + synthetic e2e; (11) fix failures, update README/docs; (12) App 8→7 adaptation + Android tests. **NO TODO stubs for core functionality; complex features may ship as minimal working version but MUST run and document limits.**

## 18. Acceptance criteria (§十九)
1. **Privacy:** neither App nor server collects/serializes/uploads/stores/logs text; forbidden field → test fails; server rejects violating batch; pipeline on-disk has no text raw.
2. **E2E:** synthetic passes ingest/preprocess/dataset/train/report; ≥ M0,M5,M6,M7,M8 smoke-run; M0–M10 + topk(1..7) configs all exist.
3. **Science:** no random window split; leave-session/day/app-out + matched-impostor implemented; M7 vs fixed-rule/auth-only-MoE/dense fair; top-k 1..7 sweep + Pareto k*; features leakage-free; honestly label "multi-user empirical = P0".
4. **Repro:** save config/seed/metrics.json/metrics.csv/split_manifest/report.md/run_context; Docker + tests run; logs no sensitive content.
5. **Artifact quality:** clear README; complete docs; schema matches real code; synthetic gen works; full type hints; 3-sensor parity; pub figs conform (Times New Roman, LaTeX, no Chinese, no titles, no overlap, PDF+PNG) each with Chinese caption.
6. **Consistency:** NO residual RSA/AES-GCM/gzip/user_id/enrollment token/server-public-key-input/ui-auth-moe monorepo/fixed top-3/accel+gyro-only; 7 scenarios / 7 experts / top-k* consistent throughout.

## 19. Non-core carry-over (§二十, do NOT rewrite — research adapts to these)
device_id = HMAC-SHA256(fixed serverStudySalt, ANDROID_ID) 64-hex, stable reinstall, no user_id/enrollment/public-key/user-input keys. `encryption:"none"` (TLS confidentiality, SHA-256 integrity over compressed bytes; no RSA/AES-GCM/PBKDF2). LZ4 frame; PayloadEnvelope EXACTLY 8 fields (algorithm/payload_base64/payload_sha256_hex/device_id/batch_id/rule_version/rule_hash/created_at_wall_millis). POST /api/v1/ingest, 5s/batch, idempotent by batch_id (409 on conflict). Endpoints only: /health,/ready,/api/v1/config,/api/v1/rules,/api/v1/ingest,/metrics; no auth (INGEST_REQUIRE_AUTH=true refuses start); no dashboard. Storage: devices/{device_id}/{date}/{batch_id}.json(+.meta.json), by_category symlinks, index/*.jsonl, quarantine/. 3-ch IMU 100Hz raw per-sample; touch = start/end timestamps only; drop-all-text. Schema contract: redaction_applied:true, encryption:none, compression:lz4_frame, 7-class task, diagnostics counts consistent, feature↔event refs consistent.
