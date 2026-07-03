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

在盘真实数据（2026-07-03，198 批）为旧 8 类。研究层按下列规则消化，保留 `raw_task_category`，把金标 `task_category` 写为 canonical 场景：

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
