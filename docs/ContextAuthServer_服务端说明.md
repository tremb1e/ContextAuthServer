# ContextAuthServer 服务端说明（摄取服务 + 研究实验层）

> 面向对象：**服务端维护者** 与 **论文 artifact 审阅者**。
> 定位：这是一份 USENIX Security 论文的**研究型 artifact**，不是商业产品。全文风格“先结论后细节”，诚实标注 minimal-working-version 的边界。
> 一句话总览：`ContextAuthServer` 由两层组成——(A) **摄取服务 `app/`**（FastAPI，只“接收 / 校验 / 落盘”，**不鉴权、不解密、无 dashboard**）；(B) **研究实验层 `research/`**（隐私保护、UI 上下文感知的移动端持续认证 **MoE** 系统的 ML / 实验 / 评估 / 报告层）。两层**依赖解耦、运行环境不同**。
>
> **本文档为“总览并引用”性质**：它汇总两层全貌，并把已有细分文档（`docs/data_schema.md`、`docs/server_api.md`、`docs/privacy_model.md`、`docs/redaction_rules.md`、`docs/storage_layout.md`、`research/README.md`、`research/_BUILD_CONTRACT.md` 等）作为权威出处引用，不逐字复制。**若本文与代码冲突，一律以代码为准。**

---

## 目录（TOC）

1. [概述与定位（两层架构 · 边界 · 两套 Python 运行环境）](#一概述与定位)
2. [快速开始（如何各自跑起来）](#二快速开始)
3. [摄取服务 `app/`](#三摄取服务-app)
   - 3.1 [六个端点](#31-六个端点)
   - 3.2 [ingest 流程（不解密）](#32-ingest-流程不解密)
   - 3.3 [8 字段信封 Envelope](#33-8-字段信封-envelope)
   - 3.4 [schema 契约与隐私红线](#34-schema-契约与隐私红线)
   - 3.5 [存储 · 索引 · 软链 · quarantine](#35-存储--索引--软链--quarantine)
   - 3.6 [幂等与错误码](#36-幂等与错误码)
   - 3.7 [本次 8→7 类改动](#37-本次-8→7-类改动)
4. [与 App 的交互协议对齐](#四与-app-的交互协议对齐)
5. [研究实验层 `research/`（核心）](#五研究实验层-research核心)
   - 5.1 [数据流与目录](#51-数据流与目录)
   - 5.2 [预处理与 204 维特征（三通道对等）](#52-预处理与-204-维特征三通道对等)
   - 5.3 [七类弱标注与泄漏列剔除](#53-七类弱标注与泄漏列剔除)
   - 5.4 [数据集与评估协议](#54-数据集与评估协议)
   - 5.5 [模型：Dense / MoE / 路由 / 损失](#55-模型dense--moe--路由--损失)
   - 5.6 [基线 M0..M10 与消融](#56-基线-m0m10-与消融)
   - 5.7 [top-k 的实验性选择（Pareto k\*）](#57-top-k-的实验性选择pareto-k)
   - 5.8 [指标与统计（EER / bootstrap / Holm，对齐 HMOG）](#58-指标与统计)
   - 5.9 [报告与出版级绘图](#59-报告与出版级绘图)
   - 5.10 [合成数据生成器](#510-合成数据生成器)
6. [运行命令速查（§十六端到端链路）](#六运行命令速查)
7. [测试（研究 44 + 摄取 53 = 97）](#七测试)
8. [依赖与环境](#八依赖与环境)
9. [目录结构与关键文件清单](#九目录结构与关键文件清单)
10. [已知限制与后续（多用户 P0）](#十已知限制与后续)
11. [隐私、安全与合规](#十一隐私安全与合规)

---

## 一、概述与定位

**结论先行：** 服务端严格分为两层，彼此之间只通过“**磁盘上的 batch 目录树**”耦合，不共享进程、不共享依赖：

| 维度 | (A) 摄取服务 `app/` | (B) 研究实验层 `research/` |
| --- | --- | --- |
| 职责 | 接收 App 上传的信封、校验完整性、按 schema 校验、落盘 + 索引 | 读磁盘 batch 树 → 特征/弱标注 → 数据集 → 训练 MoE/基线 → 评估 → 出版图 + 报告 |
| 框架 | FastAPI + uvicorn | torch / numpy / pandas / sklearn / scipy / matplotlib |
| 是否鉴权 | **否**（`INGEST_REQUIRE_AUTH=true` 直接拒绝启动） | 不适用（离线批处理，无服务） |
| 是否解密 | **否**（应用层 `encryption:"none"`，机密性靠 TLS） | 不适用 |
| Python | **base conda（Python 3.13）**，最小依赖 | **conda env `hmog_1dcnn`（Python 3.10，CPU torch）** |
| 解释器 | 系统 `python` / `python3` | `/home/tremb1e/miniconda3/envs/hmog_1dcnn/bin/python` |
| 依赖清单 | `requirements.txt`（fastapi/uvicorn/pydantic/lz4/prometheus-client） | `research/requirements.txt`（numpy/torch/pandas/sklearn/scipy/matplotlib/pyarrow/pyyaml/pytest/lz4） |
| 依赖约束 | 研究重依赖**不得**污染摄取最小依赖 | 研究依赖仅 `research/` 使用 |

**边界与非目标（诚实声明）：**
- 摄取服务只做“摄取 / 校验 / 落盘”。它**不**训练、不推理、不出报告、不提供查询面板、不解密内容。
- 研究层的所有数值结论（EER/FAR/FRR、k\*、per-scene）目前均来自**合成数据**，仅用于验证“流水线 + 方法自洽性 + 工程正确性”，**不能**替代真实多用户实证结论。**采集真实多用户数据（≥20–40 人）并在真机上重估效应量/显著性是论文 P0**（详见 §十）。

---

## 二、快速开始

### 2.1 跑摄取服务（base python）

来源：仓库 `README.md`、`Makefile`。

```bash
cd /data/paper/sp/app_exp/ContextAuthServer
python3 -m pip install -r requirements-dev.txt      # 含 pytest/httpx/pytest-asyncio
PYTHONPATH=. pytest -q tests                          # 摄取测试（53 passed）
PYTHONPATH=. uvicorn app.main:app --host 0.0.0.0 --port 8000
curl http://127.0.0.1:8000/health                     # {"status":"ok"}
curl http://127.0.0.1:8000/ready                      # {"status":"ready"} 或 503
```

Docker（可选，见 `docs/deployment.md`）：`cp .env.example .env && docker compose up -d --build`。

### 2.2 跑研究实验层（hmog_1dcnn python）

来源：`research/README.md`、`research/_BUILD_CONTRACT.md`。**必须**用 hmog_1dcnn 解释器、**从仓库根**以包方式运行：

```bash
cd /data/paper/sp/app_exp/ContextAuthServer
PY=/home/tremb1e/miniconda3/envs/hmog_1dcnn/bin/python
$PY -m pytest research/tests -q                        # 研究测试（44 passed，CPU 上约 30s）
```

端到端可复现链路见 [§六](#六运行命令速查)。`data/` 为运行期生成、默认 gitignore（`data/paper/` 被忽略）。

---

## 三、摄取服务 `app/`

> 权威出处：`app/main.py`、`app/schemas.py`、`app/storage.py`、`app/integrity.py`、`app/config.py`、`app/rules.py`、`app/errors.py`；细分文档 `docs/server_api.md`、`docs/data_schema.md`、`docs/storage_layout.md`、`docs/privacy_model.md`、`docs/redaction_rules.md`。

### 3.1 六个端点

`app/main.py` 定义**恰好 6 个** HTTP 端点，无其它路由：

| 方法 · 路径 | 处理函数 | 说明 |
| --- | --- | --- |
| `GET /health` | `health()` | 恒返回 `{"status":"ok"}`（存活探针）。 |
| `GET /ready` | `ready()` | 调 `STORE.assert_ready()` 检查数据目录/索引文件可写 + 剩余磁盘 ≥ 阈值；失败 `503`。 |
| `GET /api/v1/config` | `config()` | 返回 `serverStudySalt` / `rulesVersion` / `serverTimeMillis` / `timeSync`（`HTTP_MIDPOINT` 时钟同步 + 中国区 NTP 建议）。 |
| `GET /api/v1/rules` | `rules()` | 返回脱敏规则载荷 + 运行时计算的 `rule_hash`。**App 已不再消费此端点**（drop-all-text，见 §四/§十一），保留仅为服务端完备。 |
| `POST /api/v1/ingest` | `ingest()` | **核心**：收信封 → 校验 → 落盘（见 3.2）。 |
| `GET /metrics` | `metrics()` | Prometheus 文本格式，**不含** `device_id`/`batch_id`。 |

**启动即拒绝鉴权：** `app/config.py` 的 `Settings.from_env()` 中，若环境变量 `INGEST_REQUIRE_AUTH` 取真，直接 `raise ValueError("INGEST_REQUIRE_AUTH_unsupported")`——该原型不支持鉴权，配置成 `true` 会导致进程无法启动。

**Prometheus 指标**（`METRICS_REGISTRY`，自定义 registry）：`ingest_total{result}`、`ingest_errors_total{type}`、`ingest_decompress_seconds`、`ingest_payload_bytes_in/out`、`ingest_decrypt_seconds`（**兼容性 no-op**，原型不解密）、`server_up`。

### 3.2 ingest 流程（不解密）

`POST /api/v1/ingest` 的处理次序（`app/main.py::ingest`，**全程不解密**）：

1. 生成 `request_id`（uuid4）；读原始 body。
2. `Envelope.model_validate_json(raw_body)` 解析 8 字段信封；失败 → `400 invalid_envelope`。
3. `decode_base64(payload_base64)`（`app/integrity.py`，`validate=True` 严格 base64）；失败 → `400 invalid_base64`。
4. `verify_sha256(compressed_bytes, payload_sha256_hex)`：对**压缩字节**用 `hmac.compare_digest` 常数时间比对 SHA-256；不符 → `400 payload_hash_mismatch`。
5. `lz4.frame.decompress(compressed_bytes)` 解压；失败 → `400 corrupted_lz4_payload`。
6. `json.loads(...)`（且必须是 JSON object）；失败 → `400 invalid_json`。
7. `Batch.model_validate(obj)` 走 pydantic + 隐私红线契约（见 3.4）；失败 → **入 quarantine** 并 `400 schema_validation_failed`。
8. 一致性：`batch.device_id == envelope.device_id`（否则 quarantine + `400 envelope_batch_device_id_mismatch`）；`batch.batch_id == envelope.batch_id`（否则 quarantine + `400 envelope_batch_id_mismatch`）。
9. `STORE.store(...)` 落盘 + 索引 + 软链（见 3.5）。重复冲突 → `409 duplicate_batch_id_conflict`；磁盘不足/写失败 → `507`。
10. 成功返回 `{"status":"ok","device_id_prefix": <前8位>,"batch_id":...,"stored":true}`——**只回显 `device_id` 前 8 位**，不暴露完整设备 id 或服务端路径。

全链路以结构化 JSON 记日志（`ingest_received/decompressed/stored/rejected/quarantined`，`app/logging_config.py`）；`algorithm` 恒为 `"LZ4_FRAME+JSON"`。

### 3.3 8 字段信封 Envelope

`app/schemas.py::Envelope`（`extra="forbid"`，**恰好 8 键**）：

| 字段 | 约束（pydantic 校验器） |
| --- | --- |
| `algorithm` | `Literal["LZ4_FRAME+JSON"]` |
| `payload_base64` | `min_length=1` |
| `payload_sha256_hex` | 64-hex（`SHA256_RE`） |
| `device_id` | 64-hex 小写（`DEVICE_ID_RE = ^[a-f0-9]{64}$`） |
| `batch_id` | 合法 UUID（`uuid.UUID(value)`，规范化后存储） |
| `rule_version` | 字符串（App 侧恒 `"1"`） |
| `rule_hash` | 64-hex（App 侧恒 64 个 0） |
| `created_at_wall_millis` | `ge=0` |

### 3.4 schema 契约与隐私红线

`app/schemas.py::Batch`（及嵌套 `NodeSnapshot`/`SensorSample`/`TouchEvent`/`ContextEvent`/`ContextFeature`/`BatchDiagnostics`）在 pydantic 层强制以下**隐私红线**（`model_validator`）：

- `diagnostics.redaction_applied == True`、`compression == "lz4_frame"`、`encryption == "none"`（`BatchDiagnostics`，均为 `Literal`）。
- **password 节点必须已丢弃**：`NodeSnapshot.reject_password_nodes` 若 `password==True` → `password_node_must_be_dropped`。
- **editable text 恒 null**：可编辑节点 `text` 只允许 `{None, "", "<EDITABLE_TEXT_DROPPED>"}`，否则 `editable_text_must_be_dropped`。
- **BUILTIN_TASK 任务字段齐全**：`task_id/task_sequence/task_name/task_intuitive_description/task_category/task_session_id/task_started_at_wall_millis/task_elapsed_seconds_at_batch_end` 均非空；`task_category ∈ C0..C6`；`task_id == task_category`；`task_sequence == int(task_id[1:])`。
- **THIRD_PARTY_APP 所有 task 字段为 null**（否则 `third_party_task_fields_must_be_null`）。
- `context_feature.event_id` 必须能在本批 `context_events` 内找到（否则 `context_feature_event_id_not_found`）；且 `context_features` 的 source/task 元数据须与 batch 一致。
- **diagnostics 计数与数组长度一致**：`sensor_sample_count/context_event_count/touch_event_count` 分别等于对应数组长度；`sampling_rate_hz`（若给）须与 batch 一致。
- 时间健全：`started_at_wall_millis <= ended_at_wall_millis`。

> `TASK_CATEGORIES = {C0,C1,C2,C3,C4,C5,C6}`（`app/schemas.py` 顶部集合，见 3.7 改动）。

### 3.5 存储 · 索引 · 软链 · quarantine

`app/storage.py::DiskStore`（默认根 `data/paper/`，可用 `SERVER_DATA_DIR` 覆盖）。落盘布局（另见 `docs/storage_layout.md`）：

```
data/paper/
  server_study_salt.txt                        # 0600；稳定盐（get_server_study_salt）
  devices/{device_id}/{date}/{batch_id}.json           # 解压后、去文本的 batch JSON
                                {batch_id}.meta.json    # 信封元数据（不含 payload_base64）+ 摄取时间/大小/校验结果
  devices/{device_id}/by_category/{task_category}/{date}/{batch_id}.json  # 相对软链 → 上面的 batch（仅 BUILTIN_TASK）
  index/{devices,batches,errors}.jsonl                 # 追加式索引
  quarantine/{device_id}/{date}/{batch_id}.json        # 违规批仅存“摘要”
```

关键点：
- `{date}` 由 `batch.started_at_wall_millis` 的 UTC 日期推出（`_date_dir`）。
- **软链**：`by_category/` 用相对 `os.symlink`；不支持软链的文件系统回退为一个指向目标的小 pointer JSON。
- **quarantine 不存原文**：仅记 `reason` + payload 的 `payload_sha256` + `payload_type` + 顶层 keys（前 50 个），避免通过错误路径泄漏可疑文本。
- **路径安全**：`_safe_join` 做 `resolve()` 并校验根前缀，拒绝路径穿越；配合 `device_id` 的 64-hex 正则。
- `index/errors.jsonl` 只记 `device_id` 前 8 位。

### 3.6 幂等与错误码

- **幂等**：`store()` 比对已存在 `{batch_id}.json` 的内容——**字节相同**则幂等接受（不重复追加索引）；**同 `batch_id` 但内容不同** → `raise DuplicateBatchConflict` → 端点 `409 duplicate_batch_id_conflict`，且**不覆盖**原批。

- **拒绝/隔离原因表**（`app/errors.py::RejectReason` + `main.py` 内联串）：

| HTTP | reason | 触发点 |
| --- | --- | --- |
| 400 | `invalid_envelope` | 信封解析失败 |
| 400 | `invalid_base64` | base64 解码失败 |
| 400 | `payload_hash_mismatch` | 压缩字节 SHA-256 不符 |
| 400 | `corrupted_lz4_payload` | LZ4 解压失败 |
| 400 | `invalid_json` | JSON 解析失败/非 object |
| 400 | `schema_validation_failed` | pydantic/契约失败（**入 quarantine**） |
| 400 | `envelope_batch_device_id_mismatch` / `envelope_batch_id_mismatch` | 信封与 batch id 不一致（**入 quarantine**） |
| 409 | `duplicate_batch_id_conflict` | 同 id 不同内容 |
| 507 | `disk_space_below_threshold` / `storage_write_failed` | 磁盘不足/写失败 |
| 500 | `internal_error` | 兜底异常 |

### 3.7 本次 8→7 类改动

**唯一一处摄取侧改动**：任务场景从 8 类（曾 `C0..C7`）改为 **7 类 `C0..C6`**，与 App 端 8→7 场景重构对齐。改动落点：`app/schemas.py`（`TASK_CATEGORIES`）、`tests/helpers.py`、`tests/test_ingest.py`、`docs/data_schema.md`。**其余摄取逻辑不变**；摄取测试 `tests/` 仍 **53 passed**（base python）。

---

## 四、与 App 的交互协议对齐

> App 端（只读参考）：`/data/paper/sp/app_exp/ContextAuthlab/docs/ContextAuthLab_应用说明.md`。以下为服务端视角的对齐要点，细节以该文档与 `docs/data_schema.md` 为准。

- **数据链路（App→Server）**：明文 JSON → **LZ4 frame** 压缩 → 对**压缩字节** SHA-256 → Base64 → 组装成**恰好 8 字段** `PayloadEnvelope` → `POST /api/v1/ingest`。**应用层不做内容加密**（`encryption:"none"`），机密性依赖部署期 **TLS**。服务端做的正是这条链路的逆校验（见 3.2），二者字段/算法/摘要口径一一对应。
- **8 字段信封**：与 §3.3 完全一致（`algorithm/payload_base64/payload_sha256_hex/device_id/batch_id/rule_version/rule_hash/created_at_wall_millis`）；App 侧 `rule_version="1"`、`rule_hash=64×0` 为满足服务端 schema 的固定基线常量。
- **batch schema**：与 §3.4 一致，含 `record_type:"collection"`、`collection_source∈{BUILTIN_TASK,THIRD_PARTY_APP}`、七类 `task_category` 金标签、`sensor_samples/touch_events/context_events/context_features/diagnostics`。
- **三通道 `sensor_samples`**：`sensor_type ∈ {ACCELEROMETER, GYROSCOPE, MAGNETIC_FIELD}`，每样本 `timestamp_elapsed_nanos / wall_time_estimated_millis / x / y / z / accuracy`。默认 100 Hz。
- **drop-all-text**：所有显示/输入文本在端侧丢弃；节点 `text/text_redacted/content_desc_redacted`、事件 `window_title_redacted` **恒 `null`**；仅保留 `has_text/has_content_description` 存在性布尔。服务端 schema 把这些 `text*` 建成 `str|None` 且期望 `null`，形成**端侧丢弃 + 服务端契约**的双重保证（见 §十一）。
- **身份模型**：仅 `device_id = 小写hex(HMAC-SHA256(key=serverStudySalt, msg=ANDROID_ID))`（64-hex）。**无 user_id、无 enrollment token、无服务器公钥、无用户输入密钥**。研究中“同设备 = 同被试”。
- **幂等**：`batch_id`（UUID）是幂等键，与服务端 §3.6 语义一致。

---

## 五、研究实验层 `research/`（核心）

> 权威出处：`research/README.md`（含架构图与 minimal-version 清单）、`research/_BUILD_CONTRACT.md`（冻结构建契约）、`research/_recon_spec.md`（需求摘要）、`research/_recon_hmog.md`（HMOG 方法学镜像）、`research/_recon_contract.md`（App↔Server 精确契约）。

### 5.1 数据流与目录

**结论：** 研究层从磁盘 batch 树出发，经四段落盘中间产物，最终产出 run 结果 + 报告：

```
真实设备 devices/  或  合成 data/synthetic/（同一磁盘布局）
        │  preprocessing/  (loaders → align → sessionize → windowing → feature_extractors → quality)
        │  labeling/       (interaction_states：score-based 七类弱标注)
        ▼
data/processed/  windows.parquet + feature_manifest.json + preprocess_report.json
        │  datasets/  (splits：leave_session/day/app_out + combined；impostors：matched、用户级不相交)
        ▼
data/datasets/{name}/  {train,val,test}.parquet + impostor_pairs.parquet
                       + split_manifest.json（leakage_check 全 True）+ feature_manifest.json
        │  models/       (dense；moe E=7 top-k 1..7；5 路由；4 损失)
        │  experiments/  (trainer → evaluator[原型/余弦，enroll≠query] → metrics → bootstrap → runner)
        ▼
data/results/{run_id}/  config.yaml, metrics.json/.csv, per_user/per_scene_metrics.csv,
                        expert_utilization.csv, expert_scene_matrix.csv, model.pt,
                        logs/train.jsonl, run_context.json
        + topk_sweep.csv, topk_kstar.json, runs_index.json
        │  reporting/  (plots：matplotlib+numpy、无标题、无中文、PDF+PNG@300dpi；tables：LaTeX；report：中文)
        ▼
data/results/report.md + plots/*.{pdf,png} + latex_tables.tex
```

**冻结共享常量**（`research/__init__.py`，**全包只定义一次**）：
- `SCENARIOS = ["C0".."C6"]`，`N_SCENARIOS = 7`；`SCENARIO_NAMES`：C0 QUIESCENT_VIEWING / C1 KEYBOARD_TEXT_ENTRY / C2 CONTINUOUS_SCROLLING / C3 DISCRETE_NAVIGATION / C4 STRUCTURED_CONTROL / C5 MEDIA_PLAYBACK / C6 CANVAS_HIGH_MOTION。**7 场景 == 7 专家**，序号即列表下标。
- `LEAKAGE_COLUMNS = {estimated_context_category, game_like_score, viewIdResourceName, coarse_orientation}`（**四个泄漏列**）。
- `SENSOR_TYPES = [ACCELEROMETER, GYROSCOPE, MAGNETIC_FIELD]`（三通道**完全对等**）。

**配置系统**（`research/config.py`）：`load_config(path)` 把 `configs/experiments/*.yaml` **深合并**到 `configs/default.yaml` 之上；`config_hash(cfg)` 给出稳定 SHA-256。默认配置见 `configs/default.yaml`（seed=42、窗口 5s/步长 1s、`features.mode=ui_sensor`、`model.kind=moe`、`top_k=2`、`n_experts=7`、`router=learned`、`epochs=2`、损失 `lambda_scene=1.0/lambda_balance=0.005/lambda_smooth=0.1`、`topk.sweep=[1..7]`、`topk.select_on=val`）。

### 5.2 预处理与 204 维特征（三通道对等）

**流水线模块**（`research/preprocessing/`）：

| 模块 | 关键函数（真实签名） | 作用 |
| --- | --- | --- |
| `loaders.py` | `load_batches(input_dir, *, strict=True)`、`load_envelope(...)`、`load_envelopes(...)`、`iter_windows(...)` | 读 `devices/` 树/信封（`REQUIRED_BATCH_KEYS` 13 键、`ENVELOPE_KEYS` 8 键）。 |
| `align.py` | `align_batches(batches) -> pd.DataFrame`、`detect_clock_jumps(frame, *, max_gap_sec=600.0)`、`channel_presence(frame)` | 把所有样本按 `(device_id, timestamp_elapsed_nanos)` 排序摊平；检测 **clock-jump**（后退/远跳，标记重启）。 |
| `sessionize.py` | `sessionize(frame, *, gap_min=10.0)`、`session_summary(frame)` | 三条切 session 规则：**inter-sample gap > gap_min（默认 10min）** / **UTC 日界** / **服务重启（elapsed 后退）**；`session_id = "{device}:{day}:{seg}"`。 |
| `windowing.py` | `make_windows(session_stream, batch_index, *, window_size_sec=5.0, stride_sec=1.0)` | 每 session 切**滑窗（默认 5s/1s）**；窗口上下文含 IMU、事件、节点快照、**前一窗末快照（供 tree-diff）**；`user_id==device_id`，`package_bucket==前台包名`。 |
| `feature_extractors.py` | `build_feature_columns(mode)`、`build_feature_manifest(mode)`、`extract_window_features(ctx, *, feature_mode="ui_sensor")` | 见下。 |
| `quality.py` | `quality_flags(window_ctx) -> list[str]` | 8 个质量旗标 `QUALITY_FLAG_VOCAB`：`missing_sensor/missing_ui/low_record_count/service_restart/app_transition_window/time_gap/privacy_violation/low_confidence_label`（后者由弱标注器后置）；`_LOW_RECORD_THRESHOLD=30`。 |

**204 维特征（manifest 驱动、三通道对等、leakage-free）：**
- `ui_sensor` 模式下 `feature_manifest.json` 记 `input_dim=204`、`leakage_free=True`（已实测：`data/processed/feature_manifest.json` 与报告中的 input_dim 均为 204）。**模型从 manifest 读 `input_dim`，绝不硬编码**。
- 三通道 `_CHANNELS = {acc:ACCELEROMETER, gyro:GYROSCOPE, mag:MAGNETIC_FIELD}` × 三轴 `_AXES=(x,y,z)` 完全对等：
  - **时域** `_TIME_FEATS`（10 个）：`mean/std/min/max/rms/energy/zcr/jerk/skew/kurt`。
  - **频域** `_FREQ_FEATS`（6 个，**numpy `rfft`**）：`domfreq/speccentroid/specentropy/band0_3/band3_8/band8_15`。
  - 每通道幅值 `mag_mean/mag_std/mag_energy`、`sample_count`、**`{ch}_missing` 缺失旗标**。
- **姿态**（accel+mag 派生，**允许**）：`orient_pitch_mean/pitch_std/roll_mean/roll_std/heading_stability/landscape`——其中 **`orient_landscape` 是我们自算的 IMU 派生布尔量（合法）**，与上传的 `coarse_orientation`（泄漏列）无关。
- **跨通道**相关 `corr_acc_gyro/acc_mag/gyro_mag`；**运动能量分箱** `motion_energy_low/mid/high` + `gyro_burst_count`。
- **事件族**（9 维）：7 个事件计数（`evt_click/longclick/scroll/textchanged/focus/windowstate/windowcontent_count`）+ `evt_rate` + `evt_entropy`。
- **UI 族**（22 维）：节点计数/深度、clickable/editable/scrollable/focusable 计数与比例、checked/selected、surface-like、webview/list/scroll 指示、form-like 控件数、bounds 占用、`ui_stable_ms`、**tree-diff** `ui_treediff_nodedelta/categoryl1/boundsl1/hashchanged`。
- **包名族**（1 维，仅含包名模式）：`pkg_bucket_hash`（`package_bucket` 的小整数哈希，float 编码）。
- **缺通道处理**：缺失通道 `{ch}_missing=1.0` 且把该通道所有特征格**零填**（绝不静默置零）。
- **六种特征模式** `_FEATURE_MODES`：`sensor_only / ui_sensor / ui_sensor_no_package / package_only / ui_only / privacy_coarse_ui`（决定 imu/ui/event/package 各族是否纳入）。
- `feature_extractors.py` 内含对 `LEAKAGE_COLUMNS` 的防御断言，确保四个泄漏列**从不进入**特征列。

### 5.3 七类弱标注与泄漏列剔除

`research/labeling/interaction_states.py`：**score-based 多打分函数**（每类多条加/减 LF，非单一 if/else）：

- 每类一个打分器 `_score_c0.._score_c6`（`_SCORERS` 与 `SCENARIOS` 对齐）→ 原始分向量 → **temperature `softmax`** → 概率(7)。
- `confidence = clip(top1_prob − top4_prob, 0, 1)`；`entropy` 为香农熵；`low_confidence = (max_prob < low_conf_prob) or (confidence < low_conf_margin)`。
- `weak_label(features, temperature=1.0, *, low_conf_prob=0.35, low_conf_margin=0.10, topk_k=3)` 返回 `{probs(7), scores(7), confidence, entropy, fired_rules, top1, topk, low_confidence}`。
- **严禁读泄漏列**：LF **只允许**读白名单 `LABEL_FEATURE_KEYS`（事件/UI/tree-diff/运动/`orient_landscape`/`touch_rate` 等）。模块**导入期**即 `assert not (set(LABEL_FEATURE_KEYS) & LEAKAGE_COLUMNS)`，且 `_prepare_features` 只投影到白名单键，即使调用方传入超集也读不到泄漏列。

**四个泄漏列的处理原则**：`estimated_context_category / game_like_score / viewIdResourceName / coarse_orientation` **永不进入特征/标注/训练**。合成数据仍会在原始 batch 中产出这些列（真实数据也存在），以证明“剔除”是非空操作；下游一律排除。`test_no_leakage_columns.py` 对**每种特征模式**、`windows.parquet`、以及 train/val/test 三个 split parquet 逐一断言与泄漏列不相交，并断言 `orient_landscape` 存在。

### 5.4 数据集与评估协议

`research/datasets/`：

- **划分协议**（`splits.py::PROTOCOLS`，**整组移动、绝不切单窗**）：
  - `leave_session_out`（会话级留出）
  - `leave_day_out`（按天，早训/中验/晚测，含时间漂移）
  - `leave_app_out`（val/test 的包名桶与 train 不相交，证明不靠“记住 app”）
  - `combined_day_app`（**leave_day∩app**：test 同时是留出天且留出 app；别名 `combined`）
  - 每个协议整组（session/day/app）迁移，避免相邻重叠窗跨越边界。
- **matched_impostor**（`impostors.py::sample_matched_impostors`）：为每个 genuine test 窗，从**其他用户**中抽取**同弱标注场景**（精确同 `weak_label_top1`，或放松：genuine top1 落在冒充者 `topk` 内）的冒充窗；**冒充者池与被测用户用户级不相交**（`impostor_pool_disjoint()` 逐对校验）。
- **enroll/query 不相交**：enroll = train∪val，query = test；由 leave-session-out 天然保证 enroll∩query 会话为空，**防 EER 虚低**。
- **`split_manifest.json`**（`builders.py::build_dataset`）：记录 `protocol/feature_mode/dataset_name/seed/input_dim/users/…/n_windows_*` 与 `leakage_check` 字典（`no_session_leak / no_day_leak / no_app_leak / enroll_query_sessions_disjoint / impostor_pool_user_disjoint`），并 `kstar_selection_split="val"`。**构建期断言全部为 True，否则 `assert` 失败并报出失败项**。
- **feature modes** 通过 `_project_columns` 在建集时按 `build_feature_columns(mode)` 选列（缺列填 0.0），六种模式同 §5.2。
- 产物文件：`{train,val,test}.parquet` + `impostor_pairs.parquet` + `split_manifest.json` + `feature_manifest.json`。

### 5.5 模型：Dense / MoE / 路由 / 损失

`research/models/`（全 CPU、全类型注解、`input_dim` 从 feature_manifest 读）：

- **`DenseAuthenticator`**（`dense.py`）：MLP 编码器 + 辅助分类头（评估时丢弃分类头，用嵌入做原型/余弦验证）。
- **`MoEAuthenticator`**（`moe.py`）：**E=7 专家**（每场景一个 MLP encoder），**top-k 稀疏门控**（`top_k ∈ 1..7`，`k==n_experts==7` 即 dense-all）。`forward` 返回 `embedding / user_logits / router_logits[B,7] / router_probs[B,7] / topk_indices[B,k] / gate_weights[B,7]（激活专家重归一化、其余置 0）/ active_experts`。含 `param_count()` 与 `active_param_count()`（router+classifier+ `top_k`×单专家，作为开销代理）。
- **5 种路由**（`routing.py::ROUTER_KINDS = (learned, fixed_rule, random, hash, package_only)`，均 `forward(x, weak_probs, ids)->logits[B,7]`）：
  - `learned`：小 MLP，端到端训练。
  - `fixed_rule`：`log(weak_probs)`，**无梯度**（M4/M5）。
  - `random`：定种子随机常量 logits（M9）。
  - `hash`：按 `id % n_experts` one-hot（M10）。
  - `package_only`：只看**包名特征切片**的学习式 MLP（M3）。
- **4 个损失**（`losses.py::total_loss`，权重来自 `cfg["loss"]`）：
  - `auth_loss`（`ce_proto`=交叉熵+原型余弦，或 `triplet`）——**认证损失**；
  - `kl_weak`——router 与弱标签的 **KL 弱监督**（**按 confidence 逐样本加权**，低置信窗软跳过）；
  - `load_balance`——**负载均衡**（小权重，把平均专家使用推向均匀）；
  - `temporal_smoothness`——**时间平滑**（惩罚同 session 相邻窗的路由分布跳变）。
  - `total_loss` 返回 `(loss, parts)`，`parts` 含 `auth/kl/balance/smooth/total`；空/退化 batch 返回可微零而非 NaN。

### 5.6 基线 M0..M10 与消融

**基线套件**（`research/experiments/runner.py::M_OVERRIDES`，与 `configs/experiments/m0.yaml..m10.yaml` 精确镜像；`"__kstar__"` 是运行器在建套件时用冻结 k\* 替换的哨兵）：

| Cfg | 标签 | kind / router / top_k | 特征模式 | 目的（RQ） |
| --- | --- | --- | --- | --- |
| m0 | sensor_only_dense | dense（hidden [128,64]） | sensor_only | RQ1 下界 |
| m1 | ui_sensor_dense | dense（hidden [128,64]） | ui_sensor | RQ1 中段 |
| m2 | capacity_matched_dense | dense（更宽 [256,128,128]） | ui_sensor | RQ2 容量对照 |
| m3 | package_only_router | moe / package_only / 2 | ui_sensor | RQ6 包名混淆 |
| m4 | fixed_rule_top1 | moe / fixed_rule / 1 | ui_sensor | RQ3 固定规则锚点 |
| m5 | fixed_rule_topk_star | moe / fixed_rule / k\* | ui_sensor | RQ3 强固定基线 |
| m6 | auth_only_moe | moe / learned / k\*（`lambda_scene=0`） | ui_sensor | RQ4 弱监督 vs 仅认证 |
| **m7** | **weak_moe（正式方法）** | moe / learned / k\* | ui_sensor | **the method** |
| m8 | weak_moe_no_package | moe / learned / k\* | ui_sensor_no_package | RQ6 去包名 |
| m9 | random_moe | moe / random / k\* | ui_sensor | 路由对照 |
| m10 | hash_moe | moe / hash / k\* | ui_sensor | 路由对照 |

**消融配置**（`configs/experiments/ablation_*.yaml`，每个在 `ablation:` 键下列出所扫维度）：
- `ablation_topk.yaml`：`top_k ∈ {1..7}`。
- `ablation_privacy.yaml`：`privacy_coarse_bounds / no_resource_id / coarse_widget_category_only`（仅粗化**允许**的 UI 结构，四个泄漏列始终不是特征）。
- `ablation_features.yaml`：`no_ui / no_sensor / no_package / no_tree_diff / no_temporal_smoothness / no_load_balance`。
- `ablation_mapping.yaml`：`recommended` vs `alt_c5_nav`（7 专家 taxonomy 已冻结，故 alt 为**可选** S5 artifact）。
- `ablation_sensor_channel.yaml`：`no_accel / no_gyro / no_magnetometer`（验证磁力计贡献）。

### 5.7 top-k 的实验性选择（Pareto k\*）

`runner.py::run_topk_sweep` + `select_kstar_pareto`：
- **全扫 `k ∈ {1..7}`**（`topk.sweep`），每 k 记准确率（EER/AUC/per-scene/matched-impostor）与开销（`avg_active_experts/latency_ms/param_count/active_param_count`）→ `topk_sweep.csv`。
- **仅在 validation 选 k\* 并冻结、test 只评一次**（`select_on: val`）：规则 `smallest_k_not_sig_worse_than_best`——取“EER 不显著劣于最佳”的**最小 k**（容差 0.02 或配对 Wilcoxon p≥0.05）。冻结结果写 `topk_kstar.json`。
- Pareto 图 `topk_eer_latency_pareto`（EER-vs-开销散点，标注 k\*）。
- **诚实声明**：合成数据上的 k\*、EER 仅验证流水线，**非科学结论**。（例如当前合成产物 `topk_kstar.json` 冻结出 `kstar=1`、`best_k=7`，纯属流水线自洽性演示。）

### 5.8 指标与统计

`research/experiments/`（**镜像 HMOG** `_recon_hmog.md`；分数约定：分越大越 genuine，label 1=genuine）：
- `metrics.py`：`compute_eer_threshold`（**`sklearn.roc_curve` + `scipy.brentq` 求根**，argmin|fpr−fnr| 兜底）、`compute_eer_auc`（EER/ROC-AUC/PR-AUC/阈值）、`per_user_eer`、`per_scene_eer`、`time_to_detect` / `false_alarms_per_hour`（**window/event 级**，minimal，见 §十）。
- `bootstrap.py`：`bootstrap_ci(values, n_boot=1000, seed=0, *, alpha=0.05)` —— **按用户 bootstrap 95% CI**（重采样单位是**per-user EER 向量**）；`holm_correction`（**Holm** step-down）；`paired_delta`（**paired delta** + Wilcoxon/符号检验兜底 + Cohen's d + win-rate）；`paired_permutation_p`。
- `trainer.py`：确定性训练循环（Adam、`epochs` 默认 2、`early_stop_patience` 默认 3、按 val loss 保存最优 checkpoint、逐 epoch 写 `logs/train.jsonl`）。
- `evaluator.py`：**原型/余弦验证，enroll≠query**——prototype=用户 enroll(train+val) 窗的单位均值嵌入；query=test 窗；genuine=同用户跨会话余弦，impostor=场景匹配的用户不相交对（读 `impostor_pairs.parquet`）；MoE 额外产出 `router_probs_mean / expert_utilization / expert_scene_matrix[7×7]`。
- **主结论只看 heldout users**。

### 5.9 报告与出版级绘图

`research/reporting/` + `research/scripts/make_report.py`：
- `plots.py`：matplotlib+numpy、**Times New Roman**、**STIX mathtext**、`figure.dpi=savefig.dpi=300`、`bbox=tight`；**图内无标题、无中文（CJK）**。`save()` 对每图同时写 **`.pdf` + `.png`**。
- **实际渲染 10 张图**（`PLOT_FUNCTIONS` 中的前 10 个总会产出，已实测 `data/results/plots/` 恰有 10 个 PDF + 10 个 PNG）：`eer_bar`、`roc_curves`、`topk_ablation`、`topk_eer_latency_pareto`、`per_scene_eer`、`expert_utilization`、`expert_scene_heatmap`、`weak_label_distribution`、`package_ablation`、`privacy_ablation`。另有 `mapping_ablation`、`sensor_channel_ablation` 两个**可选**函数——仅当对应 `mapping_ablation.csv`/`sensor_channel_ablation.csv` 存在时渲染，否则 skip-with-message（因 7 专家 taxonomy 与单通道消融驱动为可选 S5）。
- `tables.py`：`write_latex_tables(...)` 产出 `latex_tables.tex`（booktabs 风格，三张表：主结果 `main_results_table`、top-k `topk_table`、per-scene `per_scene_table`；`nan` 渲染为 `--`）。
- `report.py::make_report`：产 `data/results/report.md`（**中文、先结论**），章节：一、执行摘要（结论先行，含 M7/k\*/EER+CI 与 P0 声明）；二、数据集概况（协议/特征模式/input_dim/泄漏自检/已排除四列）；三、研究问题 RQ1–RQ7；四、专家专化分析；五、局限性；六、可复现性；七、图表索引。图内不含中文，中文叙述仅在 `report.md`。

### 5.10 合成数据生成器

`research/scripts/generate_synthetic_data.py`：确定性生成多用户/多天/多会话、7 场景（C0..C6）的合成数据，写入与真实一致的 `devices/{device_id}/{date}/{batch_id}.json`；每个 batch 满足 `app/schemas.py`（BUILTIN_TASK、`encryption:"none"`、`compression:"lz4_frame"`、`redaction_applied:true`、计数一致）。CLI：

```bash
$PY -m research.scripts.generate_synthetic_data \
    --users N --days D --sessions-per-day S --out data/synthetic --seed 42 [--emit-envelopes]
```

- `--emit-envelopes` 会额外写 `data/synthetic/envelopes/{batch_id}.json`——**可被 `/api/v1/ingest` 接收的 8 字段 LZ4 信封**（供 loader 往返测试 / 打通 App↔Server 链路）。
- 合成 batch **仍会产出四个泄漏列**（`game_like_score / coarse_orientation / estimated_context_category` 等，以及 `node_class_histogram/event_type`），因为真实数据也有——用于证明下游剔除是实打实的操作。
- **声明**：合成数据只验证流水线，**不能替代多用户真实结论**（P0 声明在 `report.py` 执行摘要中给出）。

---

## 六、运行命令速查

> `research/_recon_spec.md §16` 端到端链路。均设 `cd /data/paper/sp/app_exp/ContextAuthServer`、`PY=/home/tremb1e/miniconda3/envs/hmog_1dcnn/bin/python`。已实测跑通。

```bash
# 0) 冒烟测试（tiny 合成，CPU 上约 30s）
$PY -m pytest research/tests -q

# 1) 合成数据（--emit-envelopes 另产服务端可摄取的 LZ4 信封）
$PY -m research.scripts.generate_synthetic_data \
    --users 20 --days 3 --sessions-per-day 4 --out data/synthetic --seed 42

# 2) 预处理 → windows.parquet + feature_manifest.json + preprocess_report.json
$PY -m research.scripts.run_preprocess \
    --input data/synthetic --output data/processed \
    --window-size-sec 5 --stride-sec 1

# 3) 构建 leakage-checked 数据集（此处 leave_session_out；另有 leave_day_out/leave_app_out/combined）
#    构建期断言 split_manifest.leakage_check 全 True
$PY -m research.scripts.build_datasets \
    --input data/processed --output data/datasets \
    --protocol leave_session_out

# 4) 全 M0..M10 套件 + top-k 1..7 扫描（在 validation 冻结 k*）→ runs_index.json
$PY -m research.scripts.run_all_experiments \
    --config research/configs/default.yaml \
    --data data/datasets --out data/results
#    单基线（如正式方法 M7）：
$PY -m research.scripts.run_experiment \
    --config research/configs/experiments/m7.yaml \
    --data data/datasets --out data/results --tag m7

# 5) 报告：中文 report.md + plots/*.{pdf,png}（无标题/无中文）+ latex_tables.tex
$PY -m research.scripts.make_report \
    --results data/results --out data/results/report.md \
    --data data/datasets

# 6)（可选）打包 artifact
$PY -m research.scripts.export_artifact_bundle --out data/artifact_bundle
```

`run_experiment`/`run_all_experiments` 加 `--smoke` 可缩小网络/epoch 做快速空跑（默认配置本就只训 2 epoch）。

---

## 七、测试

| 套件 | 数量 | 解释器 | 运行命令 |
| --- | --- | --- | --- |
| 摄取 `tests/` | **53 passed** | base python | `PYTHONPATH=. pytest -q tests`（或 `make test-server`） |
| 研究 `research/tests/` | **44 passed**（约 30s） | hmog_1dcnn python | `$PY -m pytest research/tests -q` |
| **合计** | **97** | 两套 env 分别运行 | —— |

- 数量已实测：`pytest --collect-only` 分别收集到 **53** 与 **44**。
- 研究测试共 **11 个测试文件 + conftest**：`conftest.py` 用 session 级 fixture 跑一次 tiny 合成流水线（5 用户/2 天/2 会话/seed 42：generate→run_preprocess→build_dataset）；文件包括 `test_loaders_ingest_roundtrip / test_preprocessing_alignment / test_sensor_features_three_channel / test_labeling_functions / test_dataset_splits / test_models_moe_topk / test_training_smoke / test_topk_sweep_smoke / test_report_generation / test_no_leakage_columns / test_privacy_sanity`。
- **§十六 端到端链路**（generate→run_preprocess→build_datasets→run_all_experiments→make_report）已跑通（`data/results/` 内可见 `runs_index.json`、`report.md`、`topk_*` 与 10+10 张图）。

---

## 八、依赖与环境

- **摄取（最小依赖，base python 3.13）**：`requirements.txt` = `fastapi / uvicorn / pydantic / lz4 / prometheus-client`；开发再加 `requirements-dev.txt`（`pytest / httpx / pytest-asyncio`）。
- **研究（锁定 hmog_1dcnn，Python 3.10，CPU torch）**：`research/requirements.txt` 固定版本 `numpy==1.26.4 / torch==2.4.1 / pandas==2.2.2 / scikit-learn==1.5.2 / scipy==1.13.1 / matplotlib==3.9.2 / pyarrow==17.0.0 / pyyaml==6.0.3 / pytest==9.0.3 / lz4>=4.3`。**研究依赖不得加入摄取最小依赖**；两层解耦。研究层与 HMOG 方法学同环境，torch 仅 CPU、不假设 CUDA。

---

## 九、目录结构与关键文件清单

| 路径 | 内容 |
| --- | --- |
| `app/main.py` | 6 端点 + ingest 流程（不解密） |
| `app/schemas.py` | pydantic 契约；`TASK_CATEGORIES = C0..C6`；Envelope/Batch/隐私红线 |
| `app/storage.py` | `DiskStore`：落盘/索引/软链/quarantine；幂等与路径安全 |
| `app/integrity.py` | base64 解码 + 压缩字节 SHA-256（常数时间比对） |
| `app/config.py` | `Settings`；`INGEST_REQUIRE_AUTH=true` 拒启动；study salt |
| `app/rules.py` · `app/default_rules.json` | 脱敏规则载荷 + 运行时 `rule_hash`（App 已不消费） |
| `app/errors.py` | `RejectReason` 枚举 |
| `research/__init__.py` | 冻结常量 `SCENARIOS / N_SCENARIOS / LEAKAGE_COLUMNS / SENSOR_TYPES` |
| `research/config.py` · `configs/default.yaml` · `configs/experiments/` | 配置合并/哈希；默认配置；m0..m10 + 5 个 ablation_* |
| `research/preprocessing/` | loaders/align/sessionize/windowing/feature_extractors/quality |
| `research/labeling/interaction_states.py` | 七类 score-based 弱标注 + 白名单 |
| `research/datasets/` | builders/splits/impostors（含 split_manifest 断言） |
| `research/models/` | dense/moe/routing/losses |
| `research/experiments/` | metrics/bootstrap/trainer/evaluator/runner(+_data) |
| `research/reporting/` | plots(10 图)/tables/report |
| `research/scripts/` | generate_synthetic_data/run_preprocess/build_datasets/run_experiment/run_all_experiments/make_report/export_artifact_bundle |
| `research/tests/` | 11 测试文件 + conftest（44 passed） |
| `research/_BUILD_CONTRACT.md` · `_recon_*.md` · `README.md` | 冻结契约 / 侦察 / 研究层 README |
| `docs/` | 本文件 + data_schema/server_api/privacy_model/redaction_rules/storage_layout/deployment 等 |
| `data/`（gitignore） | synthetic/processed/datasets/results 运行期产物 |

---

## 十、已知限制与后续

> 权威出处：`research/README.md §4`、`research/_recon_spec.md §17`。如实转述，**均为刻意、有记录的最小化**（核心路径**无 TODO 桩**）：

1. **无真实单/多用户数据 → 多用户实证是 P0。** 所有数值来自合成生成器，只验证流水线与方法自洽性；**不能**支撑真实效应量、显著性或 per-user/per-scene 结论。**采集真实多用户数据（≥20–40 人）并在真机重估是最高优先级（P0）。**
2. **特征族“缩减但有代表性”**：深度/类别用计数/比例概括而非完整直方图；bounds 占用为单标量；频域用 numpy `rfft`（三段带能量比 + 主频 + 谱质心/谱熵）而非完整周期图。`ui_sensor` 的 `input_dim=204`（manifest 驱动）。
3. **冒烟规模训练**：默认 `epochs=2`、小网络/小 batch，仅打通代码路径、产出良构 artifact，**模型未收敛**，绝对 EER 无意义，只看流程与相对结构。
4. **M2 近似容量匹配**：M2 dense 宽度手调至“接近”M7 top-k\* 的参数量，非精确 FLOP/param 求解；实际 `param_count/active_param_count` 记于各 run 的 `metrics.json` 供审计。
5. **事件级指标最小**：`time_to_detect`/`false_alarms_per_hour` 在 window/event 级、汇总分数流上计算，非完整 k-of-n / EWMA 检测策略；方向性有用、非部署级。
6. **by-user bootstrap 作用于 per-user EER 向量**（单位即用户）；在此 tiny 合成用户数下，配对显著性（`paired_delta`、Holm）功效有限。冒充者始终场景/用户匹配。
7. **ROC “曲线”图**：runner 只持久化汇总 EER/AUC，故 `roc_curves` 以每基线 ROC-AUC 柱状代理，非重绘真 ROC。
8. **可选消融图**：`mapping_ablation`/`sensor_channel_ablation` 仅在有对应 CSV 时渲染，否则 skip-with-message（taxonomy 已冻结、单通道驱动为可选）。
9. **`k=7` == dense-all**：top-k 扫描中 `k=n_experts=7` 聚合全部专家（dense-all 混合），即 spec 的既定语义。

---

## 十一、隐私、安全与合规

- **drop-all-text 双重保证**：端侧丢弃所有显示/输入文本（`docs/redaction_rules.md`——`RedactionEngine` 现为结构化 sanitizer，节点 `text/text_redacted/content_desc_redacted`、`window_title_redacted` 恒 `null`，只留 `has_text/has_content_description` 存在性布尔，password 子树整体丢弃）；服务端 schema 又把 `text*` 建成 `str|None` 且期望 `null`，并在契约层强制 password 缺席、`redaction_applied:true`、计数一致、context_feature 引用同批事件——**即便端侧出 bug，服务端契约也不存文本**。服务端**已移除**旧的二次“敏感串/原始 UI 扫描”。
- **泄漏列剔除**：四个泄漏列 `estimated_context_category / game_like_score / viewIdResourceName / coarse_orientation` **永不进入特征/标注/训练**；`test_no_leakage_columns.py` 与 `test_privacy_sanity.py` 对每种模式、windows/split parquet、原始 batch 与信封逐一断言（含 `<EDITABLE_TEXT_DROPPED>` 占位符不得出现在任何落盘产物）。IMU 自算的 `orient_landscape` 是唯一显式允许的姿态布尔量。
- **无真实密钥 / 不解密**：应用层 `encryption:"none"`，机密性依赖部署期 **TLS**；`device_id` 由固定共享盐 HMAC 派生，属**可复现伪匿名标识**（研究内“同设备”关联，非不可逆匿名化）。服务端**不鉴权、不解密**。
- **`data/` gitignore**：运行期数据 `data/paper/`、`logs/` 等默认忽略（见 `.gitignore`）。quarantine 只存摘要不存原文；`/metrics` 与 `index/errors.jsonl` 不暴露完整 `device_id`/`batch_id`。
- **IRB / 合规占位**：batch 携带 `consent_version`；App 侧采集需**明示同意** + 无障碍/电池白名单/通知/亮屏未锁屏等门控。**真实数据采集前需补齐 IRB / 知情同意等合规审批（占位，尚未在本 artifact 内落实）**。

---

*本说明基于对 `app/` 与 `research/` 真实代码逐条核实撰写；每个模块名/函数名/常量/字段/路径/端点/命令均与实际代码一致。若发现与代码不符，请以代码为准并据实修订本文档。*
