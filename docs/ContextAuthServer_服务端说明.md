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

当前 Android app 实际上传的受控任务是 `I0..I7` 八任务；论文实验层的 canonical 场景是 `C0..C6` 七专家。服务端摄取层兼容 `I0..I7` 与 `C0..C6`，研究层会保留 `raw_task_category`，并把 `task_category` 映射为 canonical `C0..C6`。

默认映射：

| 原始任务 | canonical 场景 |
|---|---|
| I0 | C0 |
| I1 | C1 |
| I2 | C3 |
| I3 | C2 |
| I4 | C2 |
| I5 | C6 |
| I6 | C6 |
| I7 | C6 |

备用映射 `alt_c5_nav` 用于 mapping 消融。

## 3. 隐私边界

服务端拒绝以下内容：

- password 节点；
- 任意非空 `node.text`；
- 任意非空 `text_redacted`；
- 任意非空 `content_desc_redacted`；
- 任意非空 `window_title_redacted`；
- 通过 extra 字段夹带的 `contentDescription`、`hintText`、`paneTitle` 等文本字段。

研究层仍会剔除泄漏列：`estimated_context_category`、`game_like_score`、`viewIdResourceName`、`coarse_orientation`。IMU 派生的 `orient_landscape` 允许使用。

## 4. 研究实验层

`research/` 实现以下流程：

1. `run_preprocess`：读取 `devices/`，对齐三通道 IMU、切 session、滑窗、提取 UI/事件/IMU 特征、弱标注七场景。
2. `build_datasets`：构建 `leave_session_out`、`leave_day_out`、`leave_app_out` 或 `combined_day_app` 数据集，检查 session/day/app 泄漏，并采样 scene-matched impostor。
3. `run_all_experiments`：先做 top-k 1..7 sweep，在 validation 上冻结 `k*`，再跑 M0-M10 和四类消融。
4. `make_report`：生成中文报告、出版级 PDF/PNG 图和 LaTeX 表。

`run_all_experiments` 默认自动产出：

- `topk_sweep.csv`
- `topk_kstar.json`
- `runs_index.json`
- `feature_ablation.csv`
- `privacy_ablation.csv`
- `mapping_ablation.csv`
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
  --window-size-sec 5 --stride-sec 1 --task-mapping recommended

$PY -m research.scripts.build_datasets \
  --input data/processed --output data/datasets \
  --protocol leave_session_out --task-mapping recommended

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

摄取契约（`app/schemas.py`，尤其 `TASK_CATEGORIES`）与**线上镜像**必须同步：**改了 schema 源码就要重建并重部署镜像——源码提交 ≠ 线上生效**。

线上部署见 `deploy/docker-compose.yml`（容器 `cca-deploy`，镜像 `contextauth/server:deploy`，数据挂载 `deploy/data/paper`）。重建 + 重部署（数据挂载不变）：

```bash
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d
```

上线自检：

```bash
# 线上 schema 应含 I0..I7
docker exec cca-deploy python -c "from app.schemas import TASK_CATEGORIES; print(sorted(TASK_CATEGORIES))"
# 受控任务金标签冒烟：应 stored:true
python tools/send_sample_batch.py --server http://127.0.0.1:8000 --task-category I7
```

> 背景：2026-07-03 一次采集出现 71 成功 / 36 隔离（`HTTP 400 schema_validation_failed`）。根因是**线上镜像陈旧（只认 `C0..C6`）**，把 App 的 `I0..I7` 受控任务金标签批次全部拒绝；`THIRD_PARTY_APP`（`task_category=null`）批次不受影响。已通过重建 + 重部署修复并端到端验证。完整分析见 [`docs/0703/`](./0703/00-索引.md)。
