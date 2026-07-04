# ContextAuthServer 服务端说明

本文档说明当前服务端的摄取协议、研究实验层、自动实验产物和隐私边界。

## 1. 服务边界

`app/` 是轻量摄取服务，只负责：

- `GET /health`
- `GET /ready`
- `GET /api/v1/config`
- `GET /api/v1/rules`
- `POST /api/v1/ingest`
- `GET /metrics`

服务端不提供在线 dashboard、注册、token、用户管理或在线认证接口。论文实验在 `research/` 离线研究层完成，从摄取落盘目录读取数据。

## 2. App 交互协议

上传 envelope 固定为 8 个字段：

```json
{
  "algorithm": "LZ4_FRAME+JSON",
  "payload_base64": "...",
  "payload_sha256_hex": "...",
  "device_id": "...",
  "batch_id": "...",
  "rule_version": "1",
  "rule_hash": "...",
  "created_at_wall_millis": 0
}
```

服务端对压缩后字节计算 SHA-256，校验成功后用 LZ4 frame 解压并按 `Batch` schema 校验。身份只有 `device_id`，无 `user_id`、enrollment token、公钥或应用层加密。

### 2.1 任务/金标体系（2026-07-03 定稿：7 类 I0..I6）

自 2026-07-03 起，金标 / 场景 / 专家空间**统一为 Android app 自身的 7 个任务类 `I0..I6`（1:1，恒等，无 8→7 映射）**。此前的 `C0..C6` 论文分类与 `recommended`/`alt_c5_nav` 双映射机制已**整体废除**。研究层 `research.SCENARIOS == ["I0".."I6"]`。

| ID | 中文（任务名 / 直觉描述） | 英文（taskName / intuitiveDescription，与 App 枚举逐字一致） | research 层 SCENARIO_NAME |
|---|---|---|---|
| I0 | 静观 · 视频监看 / 静态查看类 | Quiet watching and video / Static viewing | STATIC_VIEWING |
| I1 | 文本录入与编辑 / 文本输入类 | Text entry and editing / Text entry | TEXT_ENTRY |
| I2 | 离散点击与控件操作 / 离散触控类 | Discrete taps and controls / Discrete touch | DISCRETE_TOUCH |
| I3 | 列表滚动与检索 / 列表浏览类 | List scrolling and selection / List browsing | LIST_BROWSING |
| I4 | 长文档审阅 / 长文审阅类 | Long-document review / Long-form review | LONG_FORM_REVIEW |
| I5 | 批注绘制与拖拽 / 对象操控类 | Annotate, draw, and drag / Object manipulation | OBJECT_MANIPULATION |
| I6 | 手腕转动 / 手腕转动类 | Wrist rotation / Wrist rotation | WRIST_ROTATION |

- **I0 正典文案**：`task_name == "Quiet watching and video"`。历史构建曾出现 legacy 变体 `"Quiet viewing and video"`（仅存在于 2026-07-03 在盘旧数据，24 批 I0 中占 10 批）；代码/文档一律用正典写法，legacy 变体只在历史快照文档中记载。
- **旧 I6「空间采集类 / 扫描取景与拍摄（Scan, frame, and capture）」已删除**；新的 `I6` 是**手腕转动**（由旧 `I7` 重编号而来）。

### 2.2 摄取契约（CANONICAL + LEGACY 并集）

`app/schemas.py` 把校验集合拆为两部分，用**并集**校验（行为向后兼容，绝不拒收旧 APK/旧数据）：

- `CANONICAL_TASK_CATEGORIES = {I0..I6}`（当前 App 正典）
- `LEGACY_TASK_CATEGORIES = {I7, C0..C6}`（仅为兼容旧 APK / 旧在盘数据保留）
- `TASK_CATEGORIES = CANONICAL ∪ LEGACY`

> 保留 LEGACY 的动机：2026-07-03 上午一版过严的线上镜像（只认旧集合）把 36 批合法数据打进隔离区。契约保留 legacy 即该事故的回归修复。`task_name` 内容不参与校验（保持现状）。

### 2.3 研究层 legacy 重映射（`research.canonical_scene_for_task`）

历史在盘真实数据（2026-07-03，198 批）为旧 8 类。研究层按下列规则消化，保留 `raw_task_category`，把金标 `task_category` 写为 canonical 场景：

> **历史化说明（2026-07-05，DOC-6）**：上文「旧 8 类」描述的是 2026-07-03 的历史在盘快照；下列 legacy 重映射规则作为**防御性向后兼容**保留至今。**当前在盘数据（2026-07-04 晚间复采，120 批 `app_version=1.1.1`，见 §9）已全部为 canonical `I0..I6`**，重映射对其为恒等直通、不触发任何 legacy 分支。

- `I0..I5` → 同名（恒等）。
- `I6` 且 `task_name ∈ {"Scan, frame, and capture", "扫描取景与拍摄"}` → **`None`（legacy 剔除，不作金标）**；`I6` 且为手腕转动名（或无名）→ `I6`（新任务 / 未来新 APK）。
- `I7` → **`I6`**（旧手腕转动无条件重编号）。
- `C0..C6` → `None`（废除的旧分类，防御性处理；C 系 payload 已从盘上删除）。
- `task_category == null`（第三方批）/ 未知 id（如 `I8`）→ `None`（无金标）。

## 3. 隐私边界

服务端拒绝以下内容：

- password 节点；
- 任意非空 `node.text`；
- 任意非空 `text_redacted`；
- 任意非空 `content_desc_redacted`；
- 任意非空 `window_title_redacted`；
- 通过 extra 字段夹带的 `contentDescription`、`hintText`、`paneTitle` 等文本字段。

研究层仍会剔除泄漏列：`estimated_context_category`、`game_like_score`、`viewIdResourceName`、`coarse_orientation`。IMU 派生的 `orient_landscape` 允许使用（它是我们自算的信号，不是上传的任务标签）。

> **orient_landscape 反相修复（2026-07-03）**：旧实现用 `|roll|>π/4` 判据，导致竖屏直握（重力沿 +y、roll≈±π/2）被误判为横屏≈1、真横屏反而≈0.44。已改为按平均重力向量判据：`landscape = |mean(ax)| > |mean(ay)|`（竖屏≈0、横屏≈1），并加合成重力向量单元测试。新的 I0（视频监看可横屏）/I5（画布拖拽多横屏）弱标注规则依赖该布尔正确。

### 3.1 event_detail 键控遥测拒收契约（2026-07-05，SRV-1）

除上述文本 / 密码红线外，`app/schemas.py` 自 2026-07-05 起对每个 `context_event` 的 `event_detail` 结构对象新增**键控遥测拒收** validator（`ContextEvent.reject_event_detail_text_telemetry`）：

- **6 个禁用键**（`before_text_length`、`text_total_length`、`content_description_length`、`text_entry_count`、`added_count`、`removed_count`）在任一事件上出现即判 `event_detail_forbidden_text_telemetry`、整批隔离——它们可重建"输入文本长度序列"（一种击键动力学侧信道）。
- **3 类文本事件**（`TYPE_VIEW_TEXT_CHANGED` / `TYPE_VIEW_TEXT_SELECTION_CHANGED` / `TYPE_VIEW_TEXT_TRAVERSED_AT_MOVEMENT_GRANULARITY`）上，4 个索引键（`from_index`、`to_index`、`item_count`、`current_item_index`）必须未设置（缺省或 `-1` 哨兵），否则判 `event_detail_text_index_must_be_unset`（泄漏光标位置）。非文本事件（如 `TYPE_VIEW_SCROLLED`）上这些索引键是合法的可视区间 / 列表结构，不受约束。
- `ContextEvent` 仍保持 `extra="allow"`（结构化 `event_detail` 不改 schema）；旧注释里"no text content"的历史说法已订正——该说法在 0705 在盘数据中被证伪，正是本 validator 拦截的对象。

**动因与在盘清洗**：0705 审计发现历史 `event_detail` 曾携带文本长度序列侧信道。App v1.1.2 已在端侧剥除这些键；服务端 validator 拦截任何仍发送它们的旧 APK 批次。对**已落盘**的历史数据，`tools/sanitize_event_detail.py` 就地清洗——本轮对 120 批删除 **43,362** 处禁用键、并把 **73** 处文本事件的 `event_time_wall_millis` 下取整到整秒（去除逐击键计时侧信道），每批就地重跑 `Batch.model_validate`，清洗后 **120/120 通过新契约**。被改写的批次在其 `meta.json` 盖 `sanitized_at` / `sanitizer_version` 戳（2 个本就干净、无需改写的批次不盖戳，故盘上 118/120 有戳、全部合规）。

## 4. 研究实验层

`research/` 实现以下流程：

1. `run_preprocess`：读取 `devices/`，对齐三通道 IMU、切 session、滑窗、提取 UI/事件/IMU 特征、弱标注 7 场景（I0..I6）。金标场景由 `canonical_scene_for_task(raw_task_category, raw_task_name)` 生成（见 §2.3），并保留 `raw_task_category`。
2. `build_datasets`：构建 `leave_session_out`、`leave_day_out`、`leave_app_out` 或 `combined_day_app` 数据集，检查 session/day/app 泄漏，并采样 scene-matched impostor。
3. `run_all_experiments`：先做 top-k 1..7 sweep，在 validation 上冻结 `k*`，再跑 M0-M10 和三类消融（feature / privacy / sensor-channel）。**8→7 mapping 消融已随 C0..C6 体系一并删除**（场景恒等 I0..I6，无备用映射可比）。
4. `make_report`：生成中文报告、出版级 PDF/PNG 图和 LaTeX 表。

`run_all_experiments` 默认自动产出：

- `topk_sweep.csv`
- `topk_kstar.json`
- `runs_index.json`
- `feature_ablation.csv`
- `privacy_ablation.csv`
- `sensor_channel_ablation.csv`
- 每个 run 的 `metrics.json`、`metrics.csv`、`per_user_metrics.csv`、`per_scene_metrics.csv`、`expert_utilization.csv`、`expert_scene_matrix.csv`、`model.pt`、`logs/train.jsonl`、`run_context.json`

## 5. 常用命令

```bash
cd /data/paper/sp/app_exp/ContextAuthServer
PY=/home/tremb1e/miniconda3/envs/hmog_1dcnn/bin/python

$PY -m research.scripts.generate_synthetic_data \
  --users 20 --days 3 --sessions-per-day 4 --out data/synthetic --seed 42 --emit-envelopes

$PY -m research.scripts.run_preprocess \
  --input data/synthetic --output data/processed \
  --window-size-sec 5 --stride-sec 1

$PY -m research.scripts.build_datasets \
  --input data/processed --output data/datasets \
  --protocol leave_session_out

$PY -m research.scripts.run_all_experiments \
  --config research/configs/default.yaml \
  --data data/datasets/leave_session_out__ui_sensor \
  --out data/results --smoke

$PY -m research.scripts.make_report \
  --results data/results --out data/results/report.md \
  --data data/datasets/leave_session_out__ui_sensor
```

`--skip-ablations` 可用于临时只跑 M0-M10 与 top-k；论文实验默认不要使用该开关。

## 6. 数据充分性提醒

真实论文结论必须使用多用户、多 session、多天数据。单用户数据只能验证工程链路，不能定义 impostor 对，也不能支撑 EER/FAR/FRR 结论。

## 7. 部署与契约变更须知（2026-07-03）

本轮体系变更（详见 §2.1–§2.3）：金标改为 App 原生 7 类 `I0..I6`；删除旧 I6 空间采集；`I7→I6` 重编号；`C0..C6` 金标/场景/专家地位废除（仅作 legacy 兼容标识保留于契约与历史文档）；research 层删除双映射与 `--task-mapping`；修复 `orient_landscape` 反相 bug；统一 I0 正典文案 `"Quiet watching and video"`。

摄取契约（`app/schemas.py`，尤其 `CANONICAL_TASK_CATEGORIES` / `LEGACY_TASK_CATEGORIES` / `TASK_CATEGORIES`）与**线上镜像**必须同步：**改了 schema 源码就要重建并重部署镜像——源码提交 ≠ 线上生效**。

线上部署见 `deploy/docker-compose.yml`（容器 `cca-deploy`，镜像 `contextauth/server:deploy`，数据挂载 `deploy/data/paper`）。重建 + 重部署（数据挂载不变）：

```bash
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d
```

上线自检：

```bash
# 线上 schema 应为 CANONICAL{I0..I6} ∪ LEGACY{I7,C0..C6}
docker exec cca-deploy python -c "from app.schemas import CANONICAL_TASK_CATEGORIES, LEGACY_TASK_CATEGORIES, TASK_CATEGORIES; print('canonical', sorted(CANONICAL_TASK_CATEGORIES)); print('legacy', sorted(LEGACY_TASK_CATEGORIES)); print('union', sorted(TASK_CATEGORIES))"
# 正典任务金标签冒烟：应 stored:true
python tools/send_sample_batch.py --server http://127.0.0.1:8000 --task-category I2
# legacy 兼容冒烟（旧 APK 的 I7 仍必须被接受）：应 stored:true
python tools/send_sample_batch.py --server http://127.0.0.1:8000 --task-category I7
```

> 背景：2026-07-03 一次采集出现 71 成功 / 36 隔离（`HTTP 400 schema_validation_failed`）。根因是**线上镜像陈旧**、把 App 的受控任务金标签批次全部拒绝；`THIRD_PARTY_APP`（`task_category=null`）批次不受影响。已通过重建 + 重部署修复并端到端验证。这也是契约保留 `LEGACY_TASK_CATEGORIES` 的直接动因。完整分析见 [`docs/0703/`](./0703/) 目录下当日快照分析文档。

> **2026-07-05 契约再变更须知（event_detail 红线 + 时钟运维）**：本轮 `app/schemas.py` 新增 `event_detail` 键控遥测拒收 validator（§3.1，SRV-1），**又一次改了摄取 schema 源码**——同 §2.2 的规则，须重建并重部署线上镜像。**App v1.1.2（端侧已剥除 6 个禁用键 / 文本事件索引键）必须与加了 event_detail 契约的 Server 镜像同批上线**：老 APK 若仍发送这些键，会被新契约整批隔离（`event_detail_forbidden_text_telemetry`）。另：0705 观测到 `ingested_at` 系统性偏移的根因是**部署宿主机时钟慢约 226 s**（仓外运维项、非 App 缺陷），建议在部署宿主机启用 NTP 校时后再复采，避免污染批墙钟对账。

## 8. 2026-07-04 迭代：App v1.1.x 与 research 层根因修复

本节记录 2026-07-04 一轮 App 端迭代与 research 弱标注 / 特征层根因修复。**摄取契约、envelope 8 字段、`Batch` schema、隐私边界均未变，服务端无需改动。**

### 8.1 App 端修复（v1.1.0 → v1.1.1）

- **IMU 采集线程修复（v1.1.0，已落盘验证）**：旧实现传感器回调在主线程处理、高负载下丢样，0703 快照实测有效采样率回退至 ~86 Hz/通道。改为 `HandlerThread` 独立采集线程后实测恢复名义速率 **accel/gyro 103.3 Hz、mag 100.0 Hz**（本机 python 逐批复算确认）。**历史化注（2026-07-05，DOC-11）**：此结论最初依据的是 2026-07-04 当日 **94 批 `app_version=1.1.0`** 落盘数据；该批已从盘删除、被 2026-07-04 晚间 **120 批 `app_version=1.1.1`** 复采取代（见 §9），103.3 Hz / 100.0 Hz 在新批上同样复现。
- **App v1.1.1（app 团队报告，服务端无需改动、字段名与 schema 不变）**，三项修复：
  1. **详细页实测 Hz 口径统一（`SensorRateMath`）**：详情页展示的实时 Hz 计算口径修正，与落盘统计口径一致。
  2. **`totalSamples` 清零**：会话样本计数器跨批不再累加残值，按批正确归零。
  3. **`ServerClock` 全链墙钟统一**：批墙钟出现异常负 / 超长间隔（实测 91 s / 93 s / −6.05 s 等）的根因是链路各处墙钟来源不一致；统一为单一 `ServerClock` 后 `batch_duration_seconds` 不再虚增。字段名与 schema 不变，服务端解析无需调整。

### 8.2 research 层根因修复合集（本轮，全部落地）

| 编号 | 修复 | 要点 |
|---|---|---|
| P0-1 | `ui_surface_like` / `ui_bounds_occupancy` 量纲错配 | 旧 `_bounds_area` 除以 1080×1920 像素，而 `bounds_grid` 为像素÷24 网格（有效量级 ~173×158），真机上二特征恒 0。改为**尺度无关**的每快照屏幕外接框归一 `rel_area`，清洗哨兵值（±89478485）/非正尺寸；`ui_surface_like` 候选集限定 surface/texture/video/canvas 类名以避开满屏根容器退化。真机 I0 的 `ui_surface_like>0` 占比 0.00→0.68。 |
| P0-2 | 合成器 bounds 量纲 | `generate_synthetic_data` 由像素改为 ÷24 网格尺度，与真机对齐（RNG 抽取流不变，确定性保持）。 |
| P0-3 / P2-c | 数据集 manifest 可观测性 | `split_manifest` 增 `n_users` / `has_impostor_pairs` / `impostor_pool_check_vacuous` / `warnings`（**置于 `leakage_check` 之外**，保 `all(leakage_check)` 断言）+ `logger.warning`；`leave_day_out` 单日回退 `leave_session_out` 由 `SplitResult.notes` 标记为 `leave_day_out_fell_back_to_leave_session_out`。破解单用户 / 空 impostor 时 `all([])==True` 的静默真空。 |
| P1-a | I6 缺席线索门控 | `near_zero_touch(+0.8)` / `low_event(+0.4)` 门控在正向运动证据之后——静看窗不再被误判 I6。 |
| P1-b | I3/I4 滚动权重对齐 + 容器线索修正 | I3 滚动存在权重 1.3→1.1 对齐 I4；`ScrollView` 由 list 改判 webview 文档容器（连续滚动而非条目列表）、`GridView` 并入 list。`from_index` / `item_count` 实测恒 −1（Compose 不上报指数滚动），故不造指数特征，仅权重对齐 + 局限声明。 |
| P1-c | I5 画布线索门控 | `large_canvas`（`ui_surface_like>0.5`）门控在触控证据（`touch_rate>0.5`）之后——大 surface 无触控是视频（I0）而非画布拖拽（I5）。真机 32 个 I5 金标 0 个带 surface 节点、25 个 I0 金标 17 个带（其中 14 个 touch≤0.5），未门控会把 I0 视频漏进 I5。 |
| P2-a | 泄漏列补全 | `LEAKAGE_COLUMNS` 增 `media_like_score` / `list_like_score` / `form_like_score`。 |
| P2-b | 常量化 | 硬编码 `7` → `N_SCENARIOS`（trainer）/ `SCENARIOS`（tables / plots / smoke test）。 |

### 8.3 弱标注质量（2026-07-04 金标 169 窗）

弱标 top1 与金标一致率 **46.15% → 56.21%**（+10.06pp）；低置信 23.9% → 17.4%。逐类：I1 0.953、I6 0.882、I5 0.688（保持不回退）、**I4 0.000→1.000**（ScrollView 归类修正）；I0 / I2 / I3 仍为 0.000。

**I0 / I2 / I3 残留为采集端信号缺失的固有局限（非弱标注可调，留待采集侧解决）**：

1. 所有金标批 `TYPE_VIEW_CLICKED` 计数为 0（Compose 应用不发点击无障碍事件）→ 杀 I2 点击线索与 I3 选项线索；
2. 视频播放连发 `TYPE_WINDOW_CONTENT_CHANGED`（I0 金标 `evt_rate` 均值 ≈ 11）→ 破 I0 低事件线索；
3. Compose `LazyColumn` 从不呈现为 `RecyclerView`（I3 `ui_list` 恒 0），I4 `near_zero_click` 普发 → 无容器的 I3 滚动被 I4 吸收。

### 8.4 测试与产物

- `research/tests`：**114 passed**（`conda run -n hmog_1dcnn`；本轮由 72 经数据面 84 增至训练面 114，含 `test_bootstrap_protocol` / `test_metrics_operating_points` / `test_evaluator_coverage` / `test_dataset_splits` 等新用例，目录现共 **21** 个 `test_*.py`）。server `app/` 层 `tests/`：**59 passed**（`base` 环境；较 56 增 3 例 event_detail 遥测拒收用例）。fastapi 仅在 base、torch 仅在 hmog_1dcnn，无单一环境同跑两套——既有环境漂移，非本轮引入。
- **2026-07-05 round research 修复摘要**（详见 §9 与 `docs/0705/…-2026-07-05.md` §4.2/§4.3）：**数据面** SRV-2（`package_bucket` 窗口级众数 + 前台 app 变化切会话）/ SRV-5（分层会话切分 + `user_coverage`）/ SRV-6（matched impostor 池限 test 切分、`impostor_windows_test_only`）/ SRV-12（研究时区 `Asia/Shanghai` 定 `day_id`）/ SRV-14（只发满长窗）/ SRV-16（退化分支兜底）/ APP-2-B（丢弃 `task_category` 空且落自家包的窗）/ APP-10-B（`min_session_seconds`）/ SRV-11（`--stream-by-device` 分片）；**训练面** SRV-3（§18.3 池化 bootstrap + 同索引 paired delta + Holm）/ SRV-4（FRR@FAR、FAR@FRR、检测策略、逐对分数落盘、真 ROC 曲线）/ SRV-7（temporal-smoothness 真相邻）/ SRV-8（RandomRouter 逐窗口化）/ SRV-9（epochs 正式量级 100 + 配置传播修复）/ SRV-10（隐私 / 置信消融真实化）/ SRV-15（报告数据来源条件化 + 每图中文说明）。新增 config 键：`preprocess.study_timezone` / `min_session_seconds` / `drop_self_app_windows` / `self_app_package`、`loss.weak_conf_threshold`、`stats.n_boot`。
- 修复态产物：`data/processed-0704-postfix`、`data/datasets-0704-postfix`（三协议，`leakage_all_true=true`，单用户 → manifest `warnings` 已出现）。**修复前基线 `data/processed-0704`（46.15%）保留未覆盖。**
- 复跑：`run_preprocess --input deploy/data/paper --output data/processed-0704-postfix --window-size-sec 5 --stride-sec 1`；`build_datasets --input data/processed-0704-postfix --output data/datasets-0704-postfix --protocol {leave_session_out|leave_day_out|leave_app_out}`。
- `data/results/report.md` 为**合成 run 生成物**，非本轮真实数据产物；如需真实数字须按 §5 命令重跑再生，勿手改。

### 8.5 最新数据态

多用户可行性与实验需求满足度的最新评估见 `docs/0705/数据可行性与实验需求满足度分析-2026-07-05.md`（取代 0704 / 0703 快照）。单用户 / 单日限制仍未突破（见 §6 与 §9.1）。

## 9. 2026-07-05 迭代：event_detail 隐私红线根治 + research 论文级流水线补强 + 120 批 v1.1.1 复采

本节记录 2026-07-05 一轮**服务端摄取契约变更**（有别于 §8 的 0704 轮——该轮服务端未改）、research 数据面 / 训练面系统性补强，以及 2026-07-04 晚间的 120 批复采数据态。完整数据可行性分析见 `docs/0705/…-2026-07-05.md`。

### 9.1 当前在盘数据态（120 批 v1.1.1）

- **规模**：`deploy/data/paper/devices/36905bde…638/2026-07-04/` 现为 **120 批 `app_version=1.1.1`**（**61 金标 I0..I6 + 59 第三方**；金标逐类 I0=13 / I1=9 / I2=5 / I3=5 / I4=15 / I5=8 / I6=6，七类全覆盖）。此前 docs/0704 分析的 **94 批 v1.1.0 已从盘删除、被本批取代**。
- **采集形态**：2026-07-04 晚间单人 · 单日复采；金标墙钟合计 158 s；175,349 条三通道传感样本、116,415 个 UI 节点、582 条触控起止事件。**仍为单用户 / 单日**，层 B（科学结论）三条 P0（单用户无冒充对 / 单日 `leave_day_out` 回退 / 金标单包）未破（见 §6 与 docs/0705 §9）。
- **IMU 采样率**：`HandlerThread` 修复在本批复现 **accel/gyro 103.3 Hz、mag 100.0 Hz、零丢样**。
- **墙钟**：v1.1.1 的 `ServerClock` 统一使混时钟负间隔签名消失；**残留封套级负间隔**（`batch_duration` 封套两端取自不同来源）不影响窗边界 / 特征（研究层用单调 `elapsed`）。另 **部署宿主机时钟慢约 226 s** 污染了 `ingested_at`（仓外运维项、非 App 缺陷）——见 §7 的 NTP 提示。

### 9.2 摄取契约变更（SRV-1）

`app/schemas.py` 新增 `event_detail` 键控遥测拒收 validator、并订正 `ContextEvent` 旧注释——契约细节见 **§3.1**。在盘历史数据由 `tools/sanitize_event_detail.py` 就地清洗（120/120 过新契约）。**这是本轮唯一的服务端摄取源码变更**：改了 schema 就必须重建并重部署镜像（见 §7），旧镜像会漏放禁用键。

### 9.3 research 数据面 / 训练面补强与修复前后复测

- 修复清单见 **§8.4** 摘要；测试 research **114** / ingest **59** 全绿。
- **修复前后复测**（真实 120 批，基线 `data/processed-0705` → 最终 `data/processed-0705-postfix`）：窗口 584→**469**；包桶宇宙 2（错）→**5（对）**（com.contextauth / xhs / wechat / bili / miui.home，0 误标窗）；金标窗 296→260；金标 top1 一致率 57.77%→**63.85%（166/260）**；残留自家非金标窗 33→**0**；低置信窗 74→49。逐类：I1=1.000、I4=0.972、I6=0.923、I5=0.641；**I0=0.143、I2/I3=0** 系采集端 Compose 语义缺失（金标批 `TYPE_VIEW_CLICKED=0`、`LazyColumn` 无 `RecyclerView` 类名、视频高频 `WINDOW_CONTENT_CHANGED`）的固有局限，非弱标注器可调。
- 产物均保留（`data/processed-0705` 基线 / `data/processed-0705-postfix` 最终 / `data/datasets-0705-postfix` 三协议）。真实数字须按 §5 命令重跑再生；`data/results/report.md` 仍为合成 run 生成物，勿手改。
