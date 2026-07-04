# MoE 自动路由（门控/上下文分类器）与专家模型训练方案

> **[2026-07-03 体系演进批注]** 本文按撰写当时的任务/金标体系描述（旧 8 类 `I0..I7` 与 `C0..C6` 论文分类及其 8→7 映射/消融）。项目已于 2026-07-03 统一为 App 原生 **7 类 `I0..I6`**（1:1 恒等，无 8→7 映射；`recommended`/`alt_c5_nav` 双映射与 mapping 消融均已删除）：删除旧 I6「空间采集 / 扫描取景与拍摄」；旧 I7「手腕转动」→ 新 `I6`；`C0..C6` 金标/场景/专家地位废除（仅作 legacy 兼容标识）。**下方正文保持原样、未随体系更新**；当前态以 [`docs/ContextAuthServer_服务端说明.md`](../../docs/ContextAuthServer_服务端说明.md) 为准。
>
> **【2026-07-04 时效补注】** 任务体系现状仍为正典 7 类 `I0..I6`（上述指针不变）；另补两条 0704 演进：① IMU 有效采样率 旧 ~103 Hz → 0703 ~86 Hz（主线程回调丢样）→ App v1.1.0 `HandlerThread` 修复 → 0704 在盘实测 accel/gyro 103.3 Hz、mag 100.0 Hz；② `ui_surface_like≡0` 的特征抽取量纲根因（旧 `_bounds_area` 按 `1080×1920` px 归一、与 `bounds_grid`÷24 粗网格量级错配，相对面积被压到 ≈0）已于 2026-07-04 修复（P0-1，改尺度无关归一）。现状以 [`docs/ContextAuthServer_服务端说明.md`](../../docs/ContextAuthServer_服务端说明.md) §8 与 `docs/0704/` 文档为准。

> 文档编号：03 ｜ 主题：ContextAuthLab 持续认证系统中 **MoE 的 gating/router（上下文分类器）** 的完整落地方案
> 适用项目：ContextAuthLab（安卓无障碍采 UI 上下文 → 端侧脱敏 → LZ4 压缩上传 → server 按「app 包名 + 脱敏 UI 信息」MoE 自动路由 → 8 专家持续认证）
> 撰写视角：移动感知建模 / 行为生物特征 / MoE 路由 / 端侧部署
> 状态：**专家认证模型与路由均未实现**；本文聚焦 **router（gating）** 的数据→特征→模型→训练→推理→评估→落地全流程，专家模型仅作衔接说明。
> 编写日期：2026-06-16

---

## 0. 写在前面：本文档的事实基线与对既有简报的修正

本文所有结论建立在**真实读码 + 全量 42 批 testdata 重新统计**之上（脚本见 §12，统计可复现）。下面先给出对既有任务简报的**核实结论与修正**，因为其中两条直接改变了标签策略与评估设计：

**核实无误的事实**

- 数据集：`data/testdata/2026-06-16/` 下 **42 个明文 batch `.json` + 42 个 `.meta.json`**；`meta.json` 内 `compressed_payload_omitted=true`，但同名 `.json` 已是解压明文，可直接读。
- **1 个 device_id**（`36905bde…f573638`）、**18 个 session**、**20 个 BUILTIN_TASK + 22 个 THIRD_PARTY_APP**。
- 包名：`com.contextauth` 37 / `com.xingin.xhs` 3 / `com.miui.home` 1 / `com.miui.personalassistant` 1。
- 模态量级：`sensor_samples` 合计 **50347** 条（3 通道 ACCEL/GYRO/MAGNETIC，~100Hz，5s/批，每批 13–1852、均值 1199）；`context_events` **670**；`context_features` **670**（与 event 一一对应）；`root_nodes` 节点 **33490**；`touch_events` **0**。
- UI 纯结构、**0 文本**：全集 `text / text_redacted / content_desc_redacted` 全为 `null`（drop-all-text 迁移已生效，见 `Models.kt:134-137` 注释）。
- 事件类型分布：`TYPE_WINDOW_CONTENT_CHANGED` 444 / `TYPE_VIEW_SCROLLED` 60 / `TYPE_WINDOWS_CHANGED` 47 / `FOREGROUND_SNAPSHOT` 42 / `TYPE_WINDOW_STATE_CHANGED` 36 / `TYPE_VIEW_SELECTED` 30 / `TYPE_VIEW_CLICKED` 7 / `TYPE_VIEW_FOCUSED` 4。
- 方向（事件级）：`portrait` 619 / `landscape` 51。
- `touch_events` schema 仅时间戳、无坐标/压力（`schemas.py:176-191`），且**全集为空**。

**关键修正（务必采纳）**

1. **【修正 A — 启发式弱标签覆盖比简报更窄】** 简报称「无引导时启发式只产 C2/C3/C4/C6/UNKNOWN」。实测：把 `estimated_context_category` 按 `collection_source` 拆开后，**THIRD_PARTY_APP 的启发式输出仅为 `{C2:256, C4:50, UNKNOWN:14, C3:2}`——根本没有 C6**。简报里看到的 C0/C1/C5/C6/C7 全部来自 20 个 BUILTIN 批的**金标签直通**（`ContextFeatureExtractor.kt:31-32`：`taskCategory != null` 时 `estimated = taskCategory.name`，金标签短路了启发式）。换言之：
   - `estimated_context_category` 字段在 BUILTIN 批 = 金标签；在 THIRD_PARTY 批 = 真·启发式。**做 baseline 对比时只能用 THIRD_PARTY 段或重算启发式**，否则会把金标签误当启发式而虚高。
   - 真·启发式只覆盖 **{C2, C3, C4, UNKNOWN}** 四类（且 C3 仅 2 条），对 **C0/C1/C5/C6/C7 零召回**。半监督伪标签若直接用它会有严重覆盖偏置（见 §6.4）。

2. **【修正 B — 每个金标签类只来自唯一 session，这是全案最强约束】** 实测金标签类↔session 为**严格一对一**：

   | 类 | session 前缀 | 金标签批数 | context_event 数 | sensor 量级 |
   |---|---|---|---|---|
   | C0 | a31502d3 | 1 | **8** | 最少 |
   | C1 | 9c346767 | 4 | 65 | 多 |
   | C2 | a45c9b6d | 2 | 34 | 中 |
   | C3 | 7c806161 | 1 | 22 | 中 |
   | C4 | 47ca1854 | 4 | **102** | 多 |
   | C5 | dd9e343f | 2 | 26 | 中 |
   | C6 | cb5fc238 | 3 | 47 | 中 |
   | C7 | 3ba22765 | 3 | 44 | 多 |

   合计 **348** 个带金标签 context_event。**后果**：按 session 分组的 GroupKFold 等价于 **leave-one-class-out**——任何「按 session 留出」的折，留出集要么缺类、要么训练集缺类，**无法在「session 不泄漏」与「每折覆盖 8 类」之间两全**。这一条直接决定 §3.4 / §6 / §9 的评估必须采用**双轨制**，并把单用户单 session/类视为头号数据缺口（§10）。

3. **【补充 C — `has_text` / `has_content_description` 是强可用特征】** 虽然 0 文本内容，但**节点是否携带文本/内容描述的布尔位**保留：全集 `has_text=true` 占 **15361/33490 = 45.9%**，`has_content_description=true` 占 **4467/33490 = 13.3%**。它们能区分「文本密集（阅读/输入）」与「图形/媒体」界面，是 UI 分支的核心特征之一。

4. **【补充 D — 节点几何/类目空间小且干净，利于表格化】** `bounds_grid` 量化上界 **right≤173, bottom≤158**；`depth` max=12 / mean=6.81；`child_count` max=150 / mean=0.99；**简单类名仅 22 种**（TextView 15846 / View 8609 / Button 2442 / FrameLayout 1723 / …）；`actions_summary` 仅 5 种（CLICK 7574 / LONG_CLICK 2222 / SCROLL 717 / CHECK 439 / EDIT 62）；`viewIdResourceName` 159 种（含 `com.xingin.xhs:id/...` 等**会泄漏包名/语义**，慎用）。这些都极利于做**固定维度的表格特征**。

5. **【补充 E — 派生特征里有可直接用的信号】** `keyboard_visible_estimated=true` 62 条、`input_type_category='text'` 62 条、`input_method_visible=true` 仅 9 个事件（C3 session 小所致）；`*_like_score` 由 `ContextFeatureExtractor.kt:19-30` 启发式给出（注意 `gameLikeScore` 强依赖 `taskCategory==C5`，对 THIRD_PARTY 几乎恒为 0.1，**该派生特征在无标签数据上几乎无信息**，§4.3 会标注）。

6. **【补充 F — 约 1.5% 事件 root_nodes 为空】** 670 事件中 10 个 `root_nodes=[]`（多为 `WINDOWS_CHANGED` / 切换瞬间），需在清洗阶段处理（§3.3）。

> 一句话结论：数据够跑通 **baseline router**，但**单用户 + 每类单 session** 让「严谨的泛化评估」在当前数据上不可得；本文把这点贯穿到标签、划分、评估与补采清单，避免给出虚高指标。

---

## 1. 问题形式化：把「MoE 自动路由」定义为上下文门控（gating）分类问题

### 1.1 MoE 在本系统中的角色

整体 MoE = **1 个 gating/router + 8 个专家认证模型**。专家 `E_k`（k∈{0..7}）各自是「在第 k 个上下文场景下判别『当前持机者是否本人』」的持续认证模型（未实现）。Router `g(·)` 的职责：给定一个**上下文样本**，决定该样本（及其后续认证打分）应交给哪个/哪些专家。本文只做 router。

形式化为带覆盖兜底的多类分类 + 软门控：

- **输入** `x`：一个上下文样本的特征向量（§1.3 给粒度），由三部分拼接：
  - `x_meta`：包名 `app_package_name` + 前台 Activity（类别编码，§4.2）；
  - `x_ui`：UI 结构特征（节点统计/状态占比/类名直方图/几何布局/has_text 等，§4.1）；
  - `x_mot`（可选，进阶）：与该样本时间窗对齐的短窗运动特征（§4.4）。
- **输出**：8 个专家的路由决策。两种形态：
  - **hard routing**：`ŷ = argmax_k p_k`，`p = softmax(f(x))`，单一专家；
  - **soft top-k gating**：取 top-2 专家及归一化权重 `{(k1,w1),(k2,w2)}`，对其认证打分加权融合（标准 MoE top-k 门控，Shazeer et al. 2017 思想）。
- **覆盖兜底**：`UNKNOWN / 低置信` 不是第 9 类专家，而是**路由策略层**的回退动作（默认专家 / 多专家平均 / 暂缓认证，§8.3）。

### 1.2 本阶段推荐：先 hard routing，预留 soft top-2 接口

**推荐先做 hard routing**，理由：

1. **专家未实现**：soft 门控的价值（加权融合多专家分数）只有在有专家分数时才成立；现在无可加权对象。
2. **标签是单标签金标签**（每 BUILTIN 批一个 `task_category`），监督 hard 分类最直接、可与现有启发式逐类对比。
3. **可解释、少样本友好**：hard 分类 + GBDT 能给特征重要度，便于研究者验证「路由是否抓住了上下文语义而非用户/设备指纹」。

但**模型输出层与推理接口必须从第一天就预留 soft top-k**：分类头输出 8 维 logits/概率，hard 取 argmax，soft 取 top-2 归一化权重——同一模型两种用法，避免日后重构（§7、§8）。

### 1.3 样本粒度：以 `context_event` 为基本样本，对齐短窗运动（推荐）

候选与裁决：

- **方案 A（推荐）：1 个 `context_event` = 1 个样本**。理由：(i) UI 特征天然以事件/快照为单位；(ii) 金标签是批级、批内事件同标签，事件级可把 348 个金标签 event 全用上（比 20 批多一个量级）；(iii) `context_feature` 已与 event 一一对应（670↔670），现成对齐。运动特征：取该事件 `event_time_wall_millis` 前后各 `W/2`（建议 W=2s，可调 1–3s）的 sensor 切窗算统计量挂到该样本（§4.4）。
- **方案 B：固定时间窗滑窗（如 2s/步 1s）**，UI 取窗内最近一次 snapshot。理由是与传感器认证常用窗一致；缺点：UI 事件稀疏（5s 批内常 1–40 事件），滑窗会大量重复同一 UI 快照、且金标签批太短切不出几窗。**仅在以运动为主、UI 为辅时作为备选**。

> 采用 A。下文「样本」默认指一个 `context_event`。**注意聚合时的泄漏控制**：同一 batch/同一 session 的事件高度相关，所有划分以 session 为组（§3.4）。

---

## 2. 标签体系与 C0–C7 ↔ 8 专家映射

### 2.1 推荐映射（hard 一对一）与备选

8 任务（C0–C7）与 8 专家（场景）**并非干净双射**，分歧在 C4/C5/C7。下表给出推荐 + 备选 + 处理：

| 任务 | 中文 | 专家（推荐） | 备选/歧义 | 处理建议 |
|---|---|---|---|---|
| C0 | 持机静止 | **IDLE_HOLDING** | — | 直接映射 |
| C1 | 静态阅读 | **STATIC_READING** | 与 C2 边界（轻滑动）模糊 | 直接映射；C1↔C2 易混（§9 混淆矩阵重点） |
| C2 | 单指滑动信息流 | **SCROLL_BROWSE** | — | 直接映射 |
| C3 | 文本输入 | **TYPING** | — | 直接映射（键盘特征强判别） |
| C4 | 多控件操作 | **FORM_FILLING** | 也可 TAP_NAVIGATION | **推荐 FORM_FILLING**：C4 含输入框+滑块+复选框，editable≥1 占比高；纯点击导航无输入，更像 TAP_NAVIGATION。本数据 C4 = 模拟设置页，更贴 FORM_FILLING |
| C5 | 横屏点击小球 | **TAP_NAVIGATION** | 也可 GAME_OR_TILT | **推荐 TAP_NAVIGATION**：C5 本质是「目标触控/点击」，与导航点击运动学相近；GAME_OR_TILT 更应保留给「倾斜控制」类。**备选**：若研究者把「横屏 + 游戏化交互」视作 game，则归 GAME_OR_TILT（见 §2.2 歧义） |
| C6 | 视频观看 | **VIDEO_WATCHING** | — | 直接映射 |
| C7 | 显式转腕挑战 | **GAME_OR_TILT** | — | 直接映射：转腕 = 姿态/倾斜主导，与「倾斜类游戏」运动学同源 |

→ **推荐映射结论（一句话）**：C0→IDLE_HOLDING、C1→STATIC_READING、C2→SCROLL_BROWSE、C3→TYPING、C4→FORM_FILLING、C5→TAP_NAVIGATION、C6→VIDEO_WATCHING、C7→GAME_OR_TILT。即把分歧解为 **C4=表单、C5=点击导航、C7=倾斜**，从而 8↔8 严格双射。

### 2.2 歧义与对训练标签的影响

- **C5 的二义**是最大风险：C5↔TAP_NAVIGATION（推荐）还是 ↔GAME_OR_TILT？若改判 C5→GAME_OR_TILT，则 C7 与 C5 都进 GAME_OR_TILT，TAP_NAVIGATION 将**无任何金标签训练样本**——专家与 router 该类都成「空类」。**因此推荐映射的真正动机是避免空专家**：让 8 个专家在金标签集里都至少有一个 session 的样本。
- **C4=FORM_FILLING vs TAP_NAVIGATION**：影响 C4 与（未来）TAP_NAVIGATION 的可分性。本数据无独立「纯导航点击」类，故 TAP_NAVIGATION 在金标签集里**仅靠 C5 填充**；这意味着「点击导航 vs 倾斜游戏」的边界本质由 C5 一个 session 撑起，泛化弱，需补采（§10）。
- **建议在代码与文档中固化为一个显式映射表常量**（`CATEGORY_TO_EXPERT`），并保留 `--mapping {recommended,alt_c5_tilt}` 开关做消融（§9），让映射本身成为可评估的设计选择，而非隐式约定。

### 2.3 标签来源、可信度与 UNKNOWN 处理

- **金标签（强）**：20 个 BUILTIN 批 `task_category`，server 强制 `context_feature.task_category == batch.task_category`（`schemas.py:348-356`），可信度最高。事件级展开得 348 个金标签样本。
- **弱标签（启发式）**：THIRD_PARTY 的 `estimated_context_category`（真·启发式，仅 C2/C3/C4/UNKNOWN，见修正 A）。仅用于：(i) baseline 对照基准；(ii) 半监督自训练的**初始化候选**（须置信筛选 + 偏置告警，§6.4）。**绝不**作为监督训练的金标签。
- **UNKNOWN / 低置信处置**：
  - 训练阶段：UNKNOWN 样本**不进监督训练集**（无可靠标签）；可进半监督未标注池。
  - 推理阶段：UNKNOWN 是**路由策略动作**（§8.3），不是类。
  - 评估阶段：报告时单列「弃判率（abstain rate）」，不混进 8 类指标。

---

## 3. 数据集构建流程

### 3.1 样本粒度落定与产出物

- 基本样本 = `context_event`（§1.3）。
- 产出三张表（同 `feature_id`/`event_id` 主键对齐）：
  - `X_ui`：UI 结构 + 派生特征（§4.1/4.3），来自该 event 的 `root_nodes` 与对应 `context_feature`。
  - `X_meta`：包名/Activity 编码（§4.2）。
  - `X_mot`：以 `event_time_wall_millis` 为中心、窗宽 W 的 sensor 统计（§4.4）。运动可缺失（如该事件附近无 sensor）→ 用掩码列标注。
- 标签 `y`：
  - 金标签样本：`y = CATEGORY_TO_EXPERT[task_category]`（0..7）；
  - 无标签样本：`y = -1`（半监督池）。
- 分组键 `group = session_id`（划分用）；附 `batch_id`、`collection_source`、`coarse_orientation`、`event_type` 作分层/消融维度。

### 3.2 读取与解析（脚本骨架；生产需先 LZ4 解压）

**testdata（已是明文）直接读**；**生产/server 原始 envelope 需先解压**。两者解析后结构一致。

```python
# ---- 读 testdata 明文（本文一切实验用这个）----
import json, glob, os
def load_testdata(dir_="data/testdata/2026-06-16"):
    files = sorted(f for f in glob.glob(os.path.join(dir_, "*.json"))
                   if not f.endswith(".meta.json"))
    return [json.load(open(f)) for f in files]   # 每个元素 = 一个 batch dict

# ---- 生产：从 envelope 还原明文（与 server/app/main.py:180-212 一致）----
# server 实际流程：decode_base64(payload_base64) -> lz4.frame.decompress -> json.loads(utf-8)
import base64, lz4.frame
def decode_envelope(envelope: dict) -> dict:
    assert envelope["algorithm"] == "LZ4_FRAME+JSON"
    comp = base64.b64decode(envelope["payload_base64"])
    plain = lz4.frame.decompress(comp)            # bytes
    return json.loads(plain.decode("utf-8"))      # batch dict
```

> 字段名以**真实 JSON（snake_case）**为准：`collection_source`、`app_package_name`、`task_category`、`context_events[].root_nodes[].{class_name,bounds_grid,has_text,...}`、`sensor_samples[].{sensor_type,timestamp_elapsed_nanos,wall_time_estimated_millis,x,y,z}`。Kotlin 侧 camelCase（`Models.kt`）经 `JsonCodec` 转 snake_case 落盘。

### 3.3 数据清洗

| 现象 | 实测 | 处理 |
|---|---|---|
| `root_nodes=[]` 空事件 | 10/670（1.5%），多为 `WINDOWS_CHANGED`/切换瞬间 | UI 特征置零并打 `ui_empty=1` 掩码；**不删**（运动仍可用）。若同时无运动窗→丢弃 |
| `estimated=UNKNOWN` | THIRD_PARTY 14 条 | 进半监督池，不进监督训练 |
| 方向异常 | 仅 `portrait`/`landscape`，无 reverse；landscape 51 = **C6 32 / C5 17 / 其它(第三方) 2** | 保留 `coarse_orientation` 为类别特征；注意横屏方向与 **{C6 视频, C5 横屏触控}** 全屏场景纠缠（且 **C6 才是多数**），会与「横屏」强相关→§9 需做去方向消融，防 router 用方向「作弊」识别 C5/C6 |
| `FOREGROUND_SNAPSHOT`（42，每批 1 个） | 批首全量快照 | 作为该批最完整的 UI 视图，**保留**且可作「批级代表样本」备选 |
| `TYPE_VIEW_SCROLLED` 等无 root_nodes 增量 | 部分增量事件节点少 | 节点少不等于无效，靠 `ui_empty` 与节点计数特征区分 |
| 极短批（sensor=13） | 个别批 | 运动窗不足→该批运动特征打缺失掩码 |

### 3.4 类不平衡与按 session 分组划分（**全案核心**）

- **不平衡**：事件级金标签 {C0:8, C1:65, C2:34, C3:22, C4:102, C5:26, C6:47, C7:44}，最大/最小≈12.75:1（C4 vs C0）。
- **强约束（修正 B）**：**每类金标签只来自 1 个 session**。因此：
  - **GroupKFold(by session) 在金标签集上 = leave-one-class-out**：留出某 session 即留出整类，训练集就缺该类，**无法学到也无法在该折评估该类**。这不是实现问题，是数据结构问题。
- **因此采用双轨评估（详见 §9.1），数据划分相应双轨**：
  1. **轨道 1（泛化上界估计，会话隔离但有偏）**：`GroupKFold(by session)` 仅用于报告「**已见类**在**未见 session/批**上的稳定性」——具体做法：对**样本充足且有 ≥2 batch 的类**（C1/C4/C6/C7/C2/C5）做 **leave-one-batch-out within class**（同类不同批留出），评估「跨批不跨类」泛化；C0/C3 仅 1 批，无法留出，仅作训练侧覆盖。
  2. **轨道 2（分类能力诊断，有泄漏，需明示）**：分层 K 折（StratifiedKFold，事件级，**不按 session**），覆盖 8 类，用于诊断「在 8 类都出现时模型能否分开」。**必须在文档/报告里红字标注：此轨存在 session/batch 内泄漏，指标偏乐观，仅用于诊断与消融趋势，不作为泛化结论。**
- **半监督池**：22 个 THIRD_PARTY 批（206+74+… 共约 322 个 event）全部 `y=-1`，按 session 分组参与自训练（§6.4），评估时**永不**进入有标签折。

> 这一节是本方案与「天真做法」最大的区别：**我们不假装能在单用户单 session/类上做出可信的泛化评估**，而是把可做的（跨批诊断、分类诊断、与启发式对比）和不可做的（跨用户、跨 session 泛化）分清楚，并把后者列入补采（§10）。

---

## 4. 特征工程（分模态，给出具体清单）

> 设计目标：**固定维度、低维、可解释、可端侧**。当前数据维度小（22 类名、5 actions、几何 173×158），适合手工特征 + GBDT；进阶再上集合/序列网络（§5）。

### 4.1 UI 结构特征（核心分支，约 60–90 维）

对一个事件的 `root_nodes`（N 个节点）计算：

- **规模/结构**：`node_count=N`；`depth_max/mean/std`；`child_count_max/mean`；`ui_empty=(N==0)`。
- **状态布尔占比（每项 = 该状态节点数 / N）**：`clickable`、`editable`、`scrollable`、`checkable`、`checked`、`focused`、`selected`、`enabled`、`visible_to_user`、`long_clickable`。同时保留**计数**版（`*_count`）——占比对界面规模归一，计数保留绝对信息。
- **文本承载位（修正 C，强特征）**：`has_text_ratio`、`has_content_description_ratio`、`has_text_count`、`has_cd_count`。区分文本密集 vs 图形界面。
- **类名直方图（22 维封闭集，建议全保留为「计数 + 占比」）**：对 22 个简单类名（`TextView/View/Button/FrameLayout/LinearLayout/ImageView/ViewGroup/RelativeLayout/ProgressBar/RecyclerView/CheckBox/ActionBar$Tab/ScrollView/ViewPager/AppWidgetHostView/SeekBar/EditText/RadioButton/DrawerLayout/HorizontalScrollView/ViewFactoryHolder/VideoView`）。**强信号示例**：`VideoView/Surface→C6`；`RecyclerView/ScrollView→C2`；`EditText/RadioButton/CheckBox/SeekBar→C4`；`AppWidgetHostView→桌面(THIRD_PARTY)`。封闭集→稳定 one-hot 计数，无 OOV 风险（生产遇新类名→归 `OTHER` 桶）。
- **actions 统计（5 维封闭集）**：`{CLICK,LONG_CLICK,SCROLL,CHECK,EDIT}` 的总计数与「含该 action 的节点占比」。`SCROLL→C2`、`EDIT/CHECK→C3/C4`。
- **`bounds_grid` 布局特征（量化网格 173×158）**：
  - `screen_fill_ratio`：所有可见节点 bbox 面积并集 / (173×158)（近似用面积和裁剪）。
  - `largest_node_area_ratio`：最大单节点面积占比（大 `VideoView/Surface` → 视频/全屏）。
  - **空间分布直方图**：把网格按 3×3 或 4×4 分块，统计各块落入的节点中心数 → 9 或 16 维（捕捉「顶部 tab、底部导航、中部列表」布局）。
  - `vertical_spread / horizontal_spread`：节点中心 y/x 的 std（列表纵向铺开 vs 视频集中）。
  - `aspect_landscape=(coarse_orientation=='landscape')`（但见 §3.3 去方向消融警告）。
- **派生（直接取 `context_feature`，§4.3）**：`editable_count/scrollable_count/clickable_count` 已有，可直接用或用本节重算版（重算更可控）。

> 这些特征**无一来自文本内容**，符合「端侧丢全部文本」与 server 隐私约束。`viewIdResourceName`（159 种，含包名）**不建议进 router 特征**——它会泄漏 app 身份并导致 router 学成「按 app 记忆」而非按上下文，与 MoE「按场景路由」初衷相悖（可作单独消融观察其增益与过拟合）。

### 4.2 包名 / Activity 编码（meta 分支，注意冷启动）

- `app_package_name`：当前仅 4 个包，**目标分类编码（target/James-Stein encoding，按 session 折内 fit）** 或低维 embedding；生产需 **OOV/冷启动桶**（未见包名→`<UNK_PKG>`）。
- `foreground_activity_class_name` / `foreground_component_name`：高基数且泄漏语义，**默认不入 router**（同 viewId 理由）；如入，仅取「是否系统包」「是否输入法包」等粗粒度二值。
- **关键设计取舍**：MoE 路由应**以行为/UI 上下文为主、包名为辅或不用**。原因：(i) 包名→场景非满射（同 app 多场景：小红书可滑动可视频可输入）；(ii) 过度依赖包名会让 router 退化成「app 查表」，无法泛化到未见 app，也无法在同 app 内切换专家。**建议把包名作为弱先验特征或纯做对照消融**，让 router 主要从 UI/运动学习。

### 4.3 派生特征（直接复用 `context_feature`）

可直接取用（已在数据里）：`input_method_visible`、`keyboard_visible_estimated`、`coarse_orientation`、`event_type`（one-hot，8 种）、`editable/scrollable/clickable_count`、`media/list/form_like_score`。

- **`keyboard_visible_estimated`**：62 条 true，与 `input_type_category='text'` 一致——**对 C3(TYPING) 是强特征**，保留。
- **`event_type`**：`TYPE_VIEW_SCROLLED→C2`、`TYPE_VIEW_TEXT_CHANGED→C3`（注：本数据 text_changed 未出现，但生产会有）、`FOREGROUND_SNAPSHOT` 标识批首。
- **警告 `game_like_score`**：`ContextFeatureExtractor.kt:26-30` 中 `gameLikeScore` 在 `taskCategory==C5` 时=0.8，否则基本 0.1——它**用了标签**，对 THIRD_PARTY/推理时无信息，且会造成**标签泄漏**（训练时模型可直接读出 C5）。→ **router 训练时 `game_like_score` 必须剔除或重算**（用不含标签的纯结构启发式重算），否则金标签集上 C5 会被它泄漏，指标虚高。同理 `estimated_context_category` 本身**绝不能进特征**（它在 BUILTIN=金标签）。

### 4.4 运动传感器特征（进阶分支，每事件短窗）

切窗：对事件时间戳 `t = event_time_wall_millis`，取 sensor 中 `wall_time_estimated_millis ∈ [t-W/2, t+W/2]`（W=2s，约 200 样本/通道）。三通道（ACCEL/GYRO/MAG）各算：

- **时域（每轴 x/y/z + 合幅值 mag=√(x²+y²+z²)）**：mean、std、var、RMS、min、max、range、median、IQR、过零率(ZCR)、峰度、偏度；一阶差分得 **jerk** 的 mean/std（抖动/急动）。
- **频域（对 mag 或各轴做 FFT）**：主频（peak frequency）、谱质心、谱能量、低/中/高频带能量比（如 0–3 / 3–8 / 8–15 Hz）、谱熵。
- **姿态**：用 ACCEL 估重力分量 → pitch/roll 倾角（`atan2`）；倾角 mean/std（**C7 转腕 / C0 静止**强判别——C0 倾角 std 极小，C7 极大）。
- **跨轴/跨通道**：三轴协方差/相关；ACCEL-GYRO 互相关（转动伴随角速度）。
- **缺失掩码**：窗内样本不足阈值→该分支全置 0 + `mot_missing=1`。

> 运动分支对 **C0(静止) / C7(转腕) / C5(横屏触控) / C2(滑动节奏)** 增益最大，对 C1/C4 增益小。**但单用户下要警惕**：运动统计也强烈编码「该用户的握持习惯」，模型可能借运动记住「这是 a31502d3 这次会话」而非「这是静止场景」——这正是 §3.4 泄漏与 §10 多用户补采要解决的。建议运动分支先做**消融对照**（§9），确认它带来的是「场景」增益而非「会话指纹」增益。

---

## 5. 模型选择（分层方案 + 取舍）

### 5.1 Baseline（强烈推荐先做，M1）：表格特征 + GBDT

- **首选 LightGBM**（或 XGBoost）多分类（`objective=multiclass, num_class=8`）。备选：随机森林、逻辑回归（线性基线）。
- 输入：§4.1+§4.2+§4.3（剔除泄漏项）拼成的固定维向量（约 80–130 维）；运动特征可先不加，做纯 UI baseline。
- **为何先 baseline**：
  1. **样本极少**（348 金标签 event、8 类、单用户）——深模型必然过拟合到「该用户该 session」；GBDT 在百级样本上稳健。
  2. **可解释**：`feature_importance`/SHAP 能验证「router 是否抓上下文（has_text/scrollable/VideoView/键盘）而非用户指纹」，这对研究叙事至关重要。
  3. **可直接对照启发式**：与 `estimated_context_category` 比 macro-F1，量化「学习版 router」相对「手工规则」的增益。
  4. **端侧友好**：LightGBM 可转 ONNX 或用轻量 GBDT 推理；树模型 KB 级。

### 5.2 进阶 UI 建模（M3）：把「节点集合/树」结构化

UI 是**变长节点集合（带树结构）**，表格化丢了结构。进阶可：

- **DeepSets / Set-Transformer**：每节点编码为小特征向量（类名 emb + 状态位 + 归一 bbox + depth + has_text），对集合做 permutation-invariant 池化（sum/mean/attention）→ UI embedding。适合「节点无序集合」。
- **小型 GNN**：用 parent-child 边构图（depth/child 关系可重建层级），GraphSAGE/GCN 2–3 层 + readout。能利用树结构，但样本少时易过拟合，**优先 DeepSets**（更少参数）。
- **节点序列化 + 1D-CNN/Tiny-Transformer**：按 DFS/BFS 序列化节点流，建模顺序——但顺序对 UI 语义不稳定，**不推荐优先**。

> 进阶 UI 模型**只有在多用户/更多样本到位后**才值得（否则参数 >> 样本）。当前阶段它们的价值是「架构预研 + 在合成增强数据上验证可行性」，不是上线。

### 5.3 多模态融合（M3）：UI 分支 + 运动分支 → gating

- 运动分支：**1D-CNN 或 TCN**（时序卷积），输入对齐窗的多通道原始/低级特征（或直接喂 §4.4 统计量给 MLP）。
- 融合：**中间融合（feature-level concat 后接 MLP 分类头）** 优于 late（决策级平均），因 UI 与运动互补且需联合判别（如「横屏+点击节奏」=C5）。
- 输出 8 维 logits → softmax，hard=argmax，soft=top-2（§1.2）。

### 5.4 端侧约束与导出

- **导出**：GBDT→ONNX（onnxmltools / Hummingbird）；PyTorch 多模态→TFLite（动态范围量化）或 ONNX。
- **预算建议**：router 应 ≤ 数百 KB、单次推理 ≤ 10ms（中端机），远小于专家。GBDT baseline 天然满足；多模态网络控制在 ≤ 50K–200K 参数。
- **部署位置权衡见 §8.1**（推荐先云侧，因 server 已收全量明文，迭代快）。

---

## 6. 训练流程

### 6.1 损失与不平衡处理

- 多分类交叉熵；**类不平衡**（C0 仅 8）用：
  - GBDT：`class_weight` 反频率 或 `sample_weight`；
  - 神经网络：加权 CE 或 **focal loss**（`γ=1.5~2`）压低易类（C4）权重。
- **谨慎**：C0 样本极少且单 session，加权会放大「C0=该 session 指纹」的过拟合；建议 C0 **配合强数据增强**（§6.3）而非单纯调高权重。

### 6.2 优化、正则、早停（针对神经网络分支）

- AdamW + 余弦退火；强 dropout（0.3–0.5）、weight decay、label smoothing(0.05)；**早停以轨道 1 的跨批验证 macro-F1 为准**（不以轨道 2 泄漏指标早停）。
- GBDT：浅树（`max_depth 3-5`）、`min_child_samples` 调大、`feature_fraction/bagging_fraction<1`、早停轮数小——少样本必须强正则。

### 6.3 少样本数据增强

- **运动（时序）**：jitter（高斯噪声）、scaling（幅值缩放）、time-warp（时间扭曲）、小角度 rotation（三轴坐标系旋转，模拟握持角差异）、permutation/window-slicing。
- **UI（结构）**：节点 dropout（随机删 5–15% 子节点，模拟增量/遮挡）、bbox 抖动（±1 网格）、类名直方图扰动（小幅重采样）、状态位随机翻转极小比例（噪声鲁棒）。
- **原则**：增强**只在训练折内**做，且**保持标签语义不变**（如不要把 C0 的运动 time-warp 到出现明显转动）。增强主要救 C0/C3/C2/C5 这些单批/少批类。

### 6.4 半监督 / 自训练（用 22 个 THIRD_PARTY 批，**带强警示**）

- 思路：用金标签训出 baseline router → 对 22 批 THIRD_PARTY event 预测 → 取**高置信（如 max softmax ≥ 0.9 且与启发式不冲突）** 的伪标签加入训练，迭代（self-training）。
- **强警示（来自修正 A/B）**：
  1. **启发式只覆盖 C2/C3/C4/UNKNOWN**，THIRD_PARTY 真实分布大概率偏「滑动/桌面/列表」（小红书、桌面、个人助理）——**几乎不含 C0/C5/C6/C7**。自训练会**强化已多的类（C2/C4）**、对稀类无补充，加剧不平衡。
  2. **伪标签的「上下文」与金标签「引导任务」存在域偏移**（真实 app vs 引导 UI），直接混训可能污染。
  3. 故自训练**仅作辅助、需消融验证其净收益**（§9），且**伪标签样本权重下调**（如 0.3）、**评估永不含伪标签**。若净收益为负或不稳定，**宁可不用**，把 THIRD_PARTY 仅用于「无监督预训练 UI/运动表征」（如自监督对比学习）这一更安全的用法。

### 6.5 交叉验证协议（落地到 §3.4 双轨）

- **轨道 1（主，泛化诊断）**：对有 ≥2 batch 的类做 **leave-one-batch-out（同类内）**，报告跨批 macro-F1 的均值±std；C0/C3 单批不参与留出（仅训练）。
- **轨道 2（辅，分类能力诊断）**：事件级 **StratifiedKFold(k=5)**，覆盖 8 类——**报告时明确标注「含批/会话内泄漏，指标偏乐观」**。
- 两轨都报，差距本身就是「过拟合到会话」的度量。

---

## 7. 与 MoE 整体的衔接

### 7.1 router 输出如何驱动专家

- **hard**：`k* = argmax p` → 仅激活专家 `E_{k*}`，其认证分数 `s = E_{k*}(behavior_window)` 直接用于持续认证决策。
- **soft top-2**：取 `{(k1,w1),(k2,w2)}`（`w` 为 top-2 概率归一化）→ 融合分数 `s = w1·E_{k1}(·) + w2·E_{k2}(·)`。在场景过渡（如阅读↔滑动）时更平滑，减少误路由的认证抖动。

### 7.2 两种训练范式与推荐

- **(a) 分离式（推荐，当前阶段）**：先独立训 router（本文），各专家在各自场景金标签数据上独立训。优点：模块解耦、可分别评估、专家未实现时 router 可先上线/先评估；契合「单用户、专家未实现」现状。
- **(b) 联合式（未来）**：router+experts 端到端，加 **MoE 负载均衡/重要性辅助损失**（防专家坍塌，见 §7.3），用门控权重对专家损失加权回传。需要：专家已实现 + 多用户数据 + 足量样本。**接口预留**：router 输出 8 维概率张量、专家接口统一为 `score(features)->[0,1]`，融合层可微，便于日后把 stop-gradient 去掉做联合微调。

### 7.3 负载均衡 / importance 辅助损失（soft gating 时）

标准 MoE 顽疾是**专家坍塌**（门控总路由到少数专家）。soft 训练时加：

- **importance loss**：`L_imp = CV(Σ_batch p_k)²`（各专家被分配概率和的变异系数平方），鼓励均衡使用。
- **load-balancing loss**（Switch-Transformer 式）：`L_lb = N · Σ_k f_k · P_k`（`f_k`=路由到 k 的样本占比，`P_k`=平均门控概率）。
- **本系统特殊性**：8 专家对应**语义固定的场景**，不是可互换的同质专家，**理想分布≠均匀**（现实里 IDLE/滑动远多于转腕）。故**负载均衡权重要小**（如 0.001–0.01），仅防完全坍塌，**不要强行拉平**，否则会逼 router 违背真实上下文分布。**本阶段（hard、分离式）不需要该损失**，仅在未来 soft 联合训练引入并解释清楚。

---

## 8. 推理流程

### 8.1 端侧 vs 云侧部署权衡

| 维度 | 云侧 router（推荐先做） | 端侧 router |
|---|---|---|
| 现状契合 | server 已收全量明文，**零端改动即可迭代** | 需打包模型进 app、随版本更新 |
| 迭代速度 | 快（改 server 即可） | 慢（发版） |
| 隐私 | 上传脱敏 UI（已满足约束） | 更优（特征不出端） |
| 延迟 | 受网络影响，但认证非硬实时 | 本地低延迟 |
| 资源 | server 算 | 占端侧 CPU/电 |

→ **推荐：M1–M3 在云侧实现 router（迭代快、利用已有明文）；M4 之后若需实时/隐私增强，再蒸馏/量化下放端侧**（GBDT/小网络均可）。

### 8.2 推理管线（云侧）

```
上传 envelope → LZ4 解压(§3.2) → 按事件构造样本
  → 抽特征(UI §4.1 + meta §4.2 + 派生 §4.3，剔泄漏项；可选运动 §4.4 对齐窗)
  → router 前向 → softmax 8 维概率 p
  → hard: argmax；或 soft: top-2 (k,w)
  → 置信判定(§8.3)
  → 选择/加权专家 E_k → 持续认证打分（专家未实现，此处为接口桩）
```

### 8.3 置信度阈值与回退策略

- `max(p) ≥ τ_high`（如 0.85）：正常路由（hard 单专家 / soft top-2）。
- `τ_low ≤ max(p) < τ_high`：**soft top-2 加权**或**多专家平均**，降低误路由风险。
- `max(p) < τ_low`（含 UNKNOWN/空 UI/无运动）：**回退**——选项：(i) 路由到「默认专家」（取训练集先验最大类，如 SCROLL_BROWSE/IDLE）；(ii) 对所有专家平均；(iii) **暂缓本窗认证**（最保守，认证是安全敏感，宁可不判也不误判）。建议安全场景默认 (iii)。
- 阈值 `τ` 在轨道 1 验证集上以「弃判率-准确率」折中标定。

### 8.4 延迟与资源估计

- GBDT baseline：特征抽取（670 事件/批，单事件 µs–ms 级）+ 树推理 < 1ms；整批 < 10ms。
- 多模态网络（≤200K 参数）：单样本 < 5ms（端侧中端机），FFT/统计特征是主要开销，可缓存窗。

---

## 9. 评估方案

### 9.1 双轨指标（呼应 §3.4，避免虚高）

- **轨道 1（泛化诊断，主）**：leave-one-batch-out（同类内，仅 ≥2 批类）→ 跨批 **macro-F1 / accuracy** 均值±std。这是最接近「真泛化」的当前可得指标，但**仅覆盖部分类、仍同用户同 session**，结论须限定为「同用户跨批稳定性」。
- **轨道 2（分类能力诊断，辅）**：StratifiedKFold(k=5) 覆盖 8 类 → accuracy、**macro-F1**、**8×8 混淆矩阵**、per-class P/R/F1。**红字标注**：含会话/批内泄漏，偏乐观。
- **弃判率（abstain rate）**：UNKNOWN/低置信占比，单列。

### 9.2 与现有启发式对比（关键基线）

- 基线 = `estimated_context_category`（**只能用 THIRD_PARTY 段的真启发式，或对金标签集用不含标签的规则重算**，见修正 A、§4.3 警告）。
- 指标：在同一评估折上比 学习版 router vs 启发式 的 macro-F1 / 各类召回（尤其 C0/C1/C5/C6/C7——启发式对这些**零召回**，是学习版的主要增益来源）。这能把「为什么要训练 router 而不用规则」量化讲清楚。

### 9.3 下游认证增益评估（需多用户，给实验设计）

router 的**终极价值是提升认证准确率**，须在专家与多用户数据到位后评估：

- **对照设计**：A=「无路由 / 单一全局认证模型」 vs B=「MoE（router→专家）」 vs C=「Oracle 路由（用金标签直接选专家，上界）」。
- **认证指标**：分场景 **EER / FAR / FRR**，以及全局加权 EER；按攻击者类型（zero-effort impostor）分别报。
- **观察**：B 相对 A 的 EER 下降量 = MoE 净增益；B 相对 C 的差距 = router 误差造成的损失（隔离「路由错误」与「专家能力」）。
- **必要条件**：**多用户**（否则无 impostor、EER 无意义）——这是 §10 头号缺口。

### 9.4 消融（验证「学到的是上下文而非指纹」）

逐项关闭并比 macro-F1：去 UI / 去运动 / 去派生 / 去包名 / 去方向（防 router 靠 landscape 作弊识别 C5/C6 全屏场景）/ 去 `has_text` 族 / 换映射（recommended vs alt_c5_tilt，§2.2）。重点结论应是：**UI 结构 + has_text + 键盘 + 类名直方图**贡献主要可分性，运动主要补 C0/C7/C5，包名/viewId 贡献应小（若大则警惕指纹化/过拟合）。

---

## 10. 数据缺口对方案的影响与补采清单

| 缺口 | 现状 | 对方案影响 | 补采优先级 |
|---|---|---|---|
| **单用户 / 单设备** | 1 个 device_id | 无法评估认证 EER、无法证明跨人泛化、运动特征混入用户指纹 | **P0**：≥20–30 名被试、多机型 |
| **每类单 session** | C0–C7 各 1 session（修正 B） | 会话隔离评估退化为留一类，泛化结论不可得 | **P0**：每类 ≥3–5 个独立 session/被试 |
| **触控全空** | `touch_events=0`，且 schema 无坐标/压力（`schemas.py:176-191`） | 失去对 C5(点击)/C3(键入)/C2(滑动) 极强的触控动力学特征 | **P1**：评估在合规前提下扩 schema 采坐标/压力/手指尺寸（需重新评估隐私与无障碍可得性——无障碍通道能否拿到坐标待验证） |
| **类极不平衡** | C0=8 event | C0 学习不稳、增强也难救 | **P1**：每类目标 ≥数百 event |
| **真实 app 覆盖窄** | THIRD_PARTY 仅 4 包，启发式仅覆盖 4 类 | 半监督偏置、域偏移 | **P1**：覆盖更多真实 app 的 8 类场景，最好带（半）人工标注 |
| **方向与场景纠缠** | landscape 51 = C6 32 / C5 17 / 其它 2（C6+C5 全屏场景共享，C6 居多） | router 可能用方向作弊识别 C5/C6 | **P2**：补采各方向下的多场景 |
| **文本全丢** | 0 文本（设计使然） | UI 语义靠结构+has_text 间接表达 | 设计约束，**不补**（保持隐私）；靠结构特征补偿 |

---

## 11. 落地路线图

- **M1（1–2 周）｜跑通 baseline router**：用 348 个金标签 event + §4.1/4.2/4.3（剔泄漏） → LightGBM 8 类；双轨评估 + 与启发式对比；产出特征重要度与混淆矩阵。**交付：可复现训练/评估脚本（云侧）+ 基线指标报告**。
- **M2（2–3 周）｜半监督扩展（谨慎）**：对 22 THIRD_PARTY 批自训练，置信筛选 + 偏置告警 + 消融净收益；若负收益转为「THIRD_PARTY 仅做无监督表征预训练」。
- **M3（3–5 周）｜多模态 + 进阶 UI**：加运动分支（对齐窗特征 → 中间融合）、DeepSets UI 编码；消融确认运动带来「场景」而非「指纹」增益；导出 ONNX/TFLite 预研端侧。
- **M4（依赖多用户数据）｜与专家联调评估认证增益**：接入专家接口（先桩后真），多用户数据下做 A/B/Oracle 对照，报告分场景 EER/FAR/FRR；评估 soft top-2 与（可选）联合训练 + 负载均衡。

---

## 12. 可直接起步的代码骨架（文档内呈现，勿落盘）

> 以下为**伪代码/骨架**，字段名以真实 snake_case JSON 为准；仅依赖 numpy/pandas/lightgbm/scikit-learn/scipy（运动）。

### 12.1 读数据 + 按事件抽特征 + 标签连通性

```python
import json, glob, os
import numpy as np

DATA_DIR = "data/testdata/2026-06-16"
# 推荐映射（§2.1）：C0..C7 -> 专家 id 0..7
CATEGORY_TO_EXPERT = {  # name -> expert_id
    "C0": 0,  # IDLE_HOLDING
    "C1": 1,  # STATIC_READING
    "C2": 2,  # SCROLL_BROWSE
    "C3": 3,  # TYPING
    "C4": 4,  # FORM_FILLING
    "C5": 5,  # TAP_NAVIGATION
    "C6": 6,  # VIDEO_WATCHING
    "C7": 7,  # GAME_OR_TILT
}
SIMPLE_CLASSES = [  # 22 类封闭集（§4.1）
    "TextView","View","Button","FrameLayout","LinearLayout","ImageView","ViewGroup",
    "RelativeLayout","ProgressBar","RecyclerView","CheckBox","ActionBar$Tab","ScrollView",
    "ViewPager","AppWidgetHostView","SeekBar","EditText","RadioButton","DrawerLayout",
    "HorizontalScrollView","ViewFactoryHolder","VideoView",
]
ACTIONS = ["CLICK","LONG_CLICK","SCROLL","CHECK","EDIT"]
EVENT_TYPES = ["TYPE_WINDOW_CONTENT_CHANGED","TYPE_VIEW_SCROLLED","TYPE_WINDOWS_CHANGED",
               "FOREGROUND_SNAPSHOT","TYPE_WINDOW_STATE_CHANGED","TYPE_VIEW_SELECTED",
               "TYPE_VIEW_CLICKED","TYPE_VIEW_FOCUSED"]
GRID_W, GRID_H = 173, 158
STATE_BOOLS = ["clickable","editable","scrollable","checkable","checked","focused",
               "selected","enabled","visible_to_user","long_clickable"]

def load_batches(dir_=DATA_DIR):
    files = sorted(f for f in glob.glob(os.path.join(dir_, "*.json"))
                   if not f.endswith(".meta.json"))
    return [json.load(open(f)) for f in files]

def ui_features(event):
    nodes = event.get("root_nodes", []) or []
    N = len(nodes)
    feat = {}
    feat["node_count"] = N
    feat["ui_empty"] = int(N == 0)
    if N == 0:
        # 空 UI：结构特征置零（§3.3）；运动仍可用
        for s in STATE_BOOLS: feat[f"ratio_{s}"] = 0.0; feat[f"cnt_{s}"] = 0
        for c in SIMPLE_CLASSES: feat[f"cls_cnt_{c}"] = 0; feat[f"cls_ratio_{c}"] = 0.0
        for a in ACTIONS: feat[f"act_cnt_{a}"] = 0
        feat.update({"depth_max":0,"depth_mean":0.0,"child_max":0,
                     "has_text_ratio":0.0,"has_cd_ratio":0.0,
                     "screen_fill_ratio":0.0,"largest_area_ratio":0.0,
                     "vspread":0.0,"hspread":0.0})
        for i in range(9): feat[f"grid3x3_{i}"] = 0.0
        return feat
    depths = np.array([n.get("depth",0) for n in nodes])
    childs = np.array([n.get("child_count",0) for n in nodes])
    feat["depth_max"], feat["depth_mean"] = int(depths.max()), float(depths.mean())
    feat["child_max"] = int(childs.max())
    for s in STATE_BOOLS:
        c = sum(1 for n in nodes if n.get(s, False if s!="enabled" and s!="visible_to_user" else True))
        feat[f"cnt_{s}"] = c; feat[f"ratio_{s}"] = c / N
    feat["has_text_ratio"] = sum(1 for n in nodes if n.get("has_text")) / N
    feat["has_cd_ratio"]   = sum(1 for n in nodes if n.get("has_content_description")) / N
    # 类名直方图（封闭集 + OTHER）
    cls_cnt = {c: 0 for c in SIMPLE_CLASSES}; other = 0
    for n in nodes:
        cn = (n.get("class_name") or "").rsplit(".", 1)[-1]
        if cn in cls_cnt: cls_cnt[cn] += 1
        elif cn: other += 1
    for c in SIMPLE_CLASSES:
        feat[f"cls_cnt_{c}"] = cls_cnt[c]; feat[f"cls_ratio_{c}"] = cls_cnt[c] / N
    feat["cls_cnt_OTHER"] = other
    # actions
    act = {a: 0 for a in ACTIONS}
    for n in nodes:
        for a in n.get("actions_summary", []) or []:
            if a in act: act[a] += 1
    for a in ACTIONS: feat[f"act_cnt_{a}"] = act[a]
    # 几何：占屏 + 最大节点 + 3x3 网格 + 离散度
    areas, cxs, cys = [], [], []
    grid = np.zeros(9)
    for n in nodes:
        b = n.get("bounds_grid") or {}
        l,t,r,bo = b.get("left",0),b.get("top",0),b.get("right",0),b.get("bottom",0)
        a = max(0,(r-l))*max(0,(bo-t)); areas.append(a)
        cx,cy = (l+r)/2,(t+bo)/2; cxs.append(cx); cys.append(cy)
        gx = min(2,int(cx/(GRID_W/3+1e-9))); gy = min(2,int(cy/(GRID_H/3+1e-9)))
        grid[gy*3+gx] += 1
    tot_area = GRID_W*GRID_H
    feat["screen_fill_ratio"] = float(min(1.0, sum(areas)/tot_area))
    feat["largest_area_ratio"] = float(max(areas)/tot_area) if areas else 0.0
    feat["vspread"] = float(np.std(cys)) if cys else 0.0
    feat["hspread"] = float(np.std(cxs)) if cxs else 0.0
    for i in range(9): feat[f"grid3x3_{i}"] = grid[i] / N
    return feat

def meta_derived_features(event, cf):
    feat = {}
    # 方向 one-hot（仅 portrait/landscape 实测出现；其余留 0）
    o = event.get("coarse_orientation","unknown")
    feat["is_landscape"] = int(o == "landscape")
    # event_type one-hot
    et = event.get("event_type")
    for t in EVENT_TYPES: feat[f"evt_{t}"] = int(et == t)
    # 派生（来自 context_feature；★剔除泄漏项 game_like_score / estimated_context_category）
    feat["input_method_visible"] = int(event.get("input_method_visible", False))
    feat["keyboard_visible_estimated"] = int(bool(cf.get("keyboard_visible_estimated")))
    for k in ["editable_count","scrollable_count","clickable_count",
              "media_like_score","list_like_score","form_like_score"]:
        feat[k] = cf.get(k, 0)
    # 包名：留作弱先验/消融（这里仅做 4 类 one-hot + UNK 桶）
    pk = event.get("app_package_name") or "<UNK>"
    for p in ["com.contextauth","com.xingin.xhs","com.miui.home","com.miui.personalassistant"]:
        feat[f"pkg_{p}"] = int(pk == p)
    feat["pkg_is_unk"] = int(pk not in
        {"com.contextauth","com.xingin.xhs","com.miui.home","com.miui.personalassistant"})
    return feat

def build_dataset(batches):
    rows, ys, groups, meta = [], [], [], []
    for b in batches:
        src = b["collection_source"]; sess = b["session_id"]
        # event_id -> context_feature 映射
        cf_by_eid = {c["event_id"]: c for c in b.get("context_features", [])}
        tc = b.get("task_category")  # BUILTIN 才有
        for e in b.get("context_events", []):
            cf = cf_by_eid.get(e["event_id"], {})
            f = {}
            f.update(ui_features(e))
            f.update(meta_derived_features(e, cf))
            rows.append(f)
            ys.append(CATEGORY_TO_EXPERT[tc] if (src=="BUILTIN_TASK" and tc in CATEGORY_TO_EXPERT) else -1)
            groups.append(sess)
            meta.append({"batch_id": b["batch_id"], "src": src, "category": tc,
                         "event_id": e["event_id"]})
    import pandas as pd
    X = pd.DataFrame(rows).fillna(0.0)
    return X, np.array(ys), np.array(groups), meta

# ---- 连通性自检（应得：金标签样本=348，8 类齐全；无标签=322）----
if __name__ == "__main__":
    bs = load_batches()
    X, y, g, meta = build_dataset(bs)
    print("samples:", len(X), "features:", X.shape[1])
    print("labeled(gold):", (y>=0).sum(), "unlabeled:", (y<0).sum())
    import collections; print("per-class:", collections.Counter(y[y>=0].tolist()))
```

### 12.2 LightGBM 训练 + 双轨评估（GroupKFold 诊断）

```python
import numpy as np, lightgbm as lgb
from sklearn.model_selection import GroupKFold, StratifiedKFold
from sklearn.metrics import f1_score, classification_report, confusion_matrix

def train_eval(X, y, groups, meta):
    mask = y >= 0
    Xl, yl, gl = X[mask].reset_index(drop=True), y[mask], groups[mask]
    cats = np.array([m["category"] for m in meta])[mask]

    params = dict(objective="multiclass", num_class=8, learning_rate=0.05,
                  num_leaves=15, max_depth=4, min_child_samples=10,
                  feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
                  class_weight="balanced", n_estimators=300, verbose=-1)

    # ---- 轨道 2（分类能力诊断；★含会话/批内泄漏，偏乐观）----
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=0)
    f1s = []
    for tr, te in skf.split(Xl, yl):
        clf = lgb.LGBMClassifier(**params)
        clf.fit(Xl.iloc[tr], yl[tr])
        pred = clf.predict(Xl.iloc[te])
        f1s.append(f1_score(yl[te], pred, average="macro"))
    print(f"[轨道2/泄漏] 5-fold macro-F1 = {np.mean(f1s):.3f} ± {np.std(f1s):.3f}  (偏乐观)")

    # ---- 轨道 1（泛化诊断：同类 leave-one-batch-out；仅 ≥2 批类）----
    bids = np.array([m["batch_id"] for m in meta])[mask]
    accs = []
    for c in np.unique(cats):
        cbatches = np.unique(bids[cats == c])
        if len(cbatches) < 2: continue          # C0/C3 单批，跳过留出
        for hb in cbatches:                       # 留出该类一个 batch 当正例验证
            tr = ~((cats == c) & (bids == hb))
            te = (cats == c) & (bids == hb)
            clf = lgb.LGBMClassifier(**params); clf.fit(Xl[tr], yl[tr])
            pred = clf.predict(Xl[te])
            accs.append((pred == yl[te]).mean())
    print(f"[轨道1/跨批] leave-one-batch acc = {np.mean(accs):.3f} ± {np.std(accs):.3f}")

    # 全量拟合 + 特征重要度 + 混淆矩阵（诊断用）
    clf = lgb.LGBMClassifier(**params).fit(Xl, yl)
    imp = sorted(zip(Xl.columns, clf.feature_importances_), key=lambda t:-t[1])[:20]
    print("Top-20 features:", imp)
    print(confusion_matrix(yl, clf.predict(Xl)))   # 训练集混淆（仅看结构）
    return clf

def vs_heuristic(meta, y_true_name):
    # 与启发式对比：★只对 THIRD_PARTY 用 estimated（真启发式），
    # 或对金标签集用「不含标签的规则重算」（此处略）。详见 §4.3 / §9.2 警告。
    pass
```

### 12.3 运动短窗对齐（多模态用）

```python
import numpy as np
from scipy.fft import rfft, rfftfreq

def motion_window(batch, t_center_ms, W_ms=2000):
    out = {}
    for ch in ["ACCELEROMETER","GYROSCOPE","MAGNETIC_FIELD"]:
        S = [s for s in batch.get("sensor_samples", [])
             if s["sensor_type"]==ch and abs(s["wall_time_estimated_millis"]-t_center_ms) <= W_ms/2]
        if len(S) < 8:
            for ax in ["x","y","z","mag"]:
                out[f"{ch[:3]}_{ax}_mean"]=0.0; out[f"{ch[:3]}_{ax}_std"]=0.0
            out[f"{ch[:3]}_missing"]=1; continue
        out[f"{ch[:3]}_missing"]=0
        arr = {ax: np.array([s[ax] for s in S], float) for ax in ["x","y","z"]}
        arr["mag"] = np.sqrt(arr["x"]**2+arr["y"]**2+arr["z"]**2)
        for ax,v in arr.items():
            out[f"{ch[:3]}_{ax}_mean"]=float(v.mean()); out[f"{ch[:3]}_{ax}_std"]=float(v.std())
            out[f"{ch[:3]}_{ax}_rms"]=float(np.sqrt((v**2).mean()))
            out[f"{ch[:3]}_{ax}_zcr"]=float(((v[:-1]*v[1:])<0).mean())
            out[f"{ch[:3]}_{ax}_jerk"]=float(np.diff(v).std())
        # 频域（对 mag）
        v = arr["mag"] - arr["mag"].mean()
        sp = np.abs(rfft(v)); fr = rfftfreq(len(v), d=W_ms/1000/len(v))
        out[f"{ch[:3]}_peakfreq"]=float(fr[sp.argmax()]) if len(fr) else 0.0
        out[f"{ch[:3]}_specenergy"]=float((sp**2).sum())
    # 姿态（用 ACCEL 估倾角）
    return out
```

### 12.4 多模态 PyTorch gating 网络骨架（进阶/M3）

```python
import torch, torch.nn as nn

class UiBranch(nn.Module):
    """表格 UI 特征 -> embedding（M3 可替换为 DeepSets/Set-Transformer）"""
    def __init__(self, in_dim, h=128, out=64):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(in_dim,h), nn.ReLU(), nn.Dropout(0.4),
                                 nn.Linear(h,out), nn.ReLU())
    def forward(self,x): return self.net(x)

class MotionBranch(nn.Module):
    """对齐窗多通道 -> 1D-CNN/TCN（此处给 1D-CNN）"""
    def __init__(self, in_ch=9, out=64):  # 3 通道 x{x,y,z} = 9
        super().__init__()
        self.cnn = nn.Sequential(
            nn.Conv1d(in_ch,32,5,padding=2), nn.ReLU(), nn.MaxPool1d(2),
            nn.Conv1d(32,64,5,padding=2), nn.ReLU(), nn.AdaptiveAvgPool1d(1))
        self.fc = nn.Linear(64,out)
    def forward(self,x):           # x: (B, in_ch, T)
        return torch.relu(self.fc(self.cnn(x).squeeze(-1)))

class GatingNet(nn.Module):
    """中间融合 -> 8 维 logits；hard=argmax, soft=top-2(§1.2/§7)"""
    def __init__(self, ui_dim, use_motion=True, n_expert=8):
        super().__init__()
        self.ui = UiBranch(ui_dim); self.use_motion=use_motion
        self.mot = MotionBranch() if use_motion else None
        fuse_in = 64 + (64 if use_motion else 0)
        self.head = nn.Sequential(nn.Linear(fuse_in,64), nn.ReLU(), nn.Dropout(0.4),
                                  nn.Linear(64,n_expert))
    def forward(self, ui_x, mot_x=None):
        z = self.ui(ui_x)
        if self.use_motion and mot_x is not None:
            z = torch.cat([z, self.mot(mot_x)], dim=-1)
        return self.head(z)        # logits (B,8)

def topk_gate(logits, k=2):
    p = torch.softmax(logits, -1)
    w, idx = torch.topk(p, k, dim=-1)
    return idx, w / w.sum(-1, keepdim=True)   # 专家索引 + 归一化权重

# 训练损失（hard 分离式：仅 CE+类权重/focal；soft 联合训练时再加 §7.3 负载均衡）
# loss = nn.CrossEntropyLoss(weight=class_w)(logits, y)
```

---

## 13. 关键文件与代码位置索引（便于复核）

- 端侧特征/启发式路由：`android-app/src/main/java/com/contextauth/core/ContextFeatureExtractor.kt:10-59`（金标签直通 `:31-32`；启发式分支 `:33-38`；`gameLikeScore` 用标签 `:26-30`，**训练时须剔除**）。
- 数据模型（字段权威定义）：`android-app/src/main/java/com/contextauth/core/Models.kt`（`NodeSnapshot` `:124-147`，含 `hasText/hasContentDescription` 与注释 `:134-137`；`TaskCategory` C0–C7 `:21-112`；`ContextFeature` `:170-190`）。
- server 强制契约（金标签可信来源）：`server/app/schemas.py`（金标签一致性校验 `:348-356`；BUILTIN 必填 `:313-331`；`NodeSnapshot` `extra="allow"` 故 `has_text` 等额外字段合法 `:129-161`；`TouchEvent` 仅时间戳 `:176-191`）。
- server 解压管线（生产读法）：`server/app/main.py:180-212`（`decode_base64` → `lz4.frame.decompress` → `json.loads`）。
- 端侧 LZ4+Base64 封装：`android-app/src/main/java/com/contextauth/core/JsonCodec.kt:80-116`。
- 序列化字段名（camelCase→snake_case）：`JsonCodec.kt:105-210`。

---

## 14. 总结

- 把 MoE 自动路由落为**带覆盖兜底的 8 类上下文门控分类**，**本阶段先 hard routing + GBDT baseline**，输出层预留 soft top-2 与联合训练接口。
- 推荐映射：**C0→IDLE_HOLDING、C1→STATIC_READING、C2→SCROLL_BROWSE、C3→TYPING、C4→FORM_FILLING、C5→TAP_NAVIGATION、C6→VIDEO_WATCHING、C7→GAME_OR_TILT**（解 C4=表单、C5=点击、C7=倾斜，避免空专家）。
- 特征以**纯结构 UI（节点统计/状态占比/22 类名直方图/has_text 族/几何网格）+ 键盘/方向/event_type 派生**为主，运动短窗补 C0/C5/C7，**严防 `game_like_score`/`estimated_context_category`/`viewId`/`方向` 造成标签泄漏或会话指纹化**。
- **最大约束是单用户 + 每类单 session**：采用**双轨评估**（跨批泛化诊断 + 8 类分类诊断，后者明示泄漏），并把多用户/每类多 session/触控坐标列为 P0/P1 补采；下游认证 EER 增益须等多用户数据再评。
- 半监督用 22 个 THIRD_PARTY 批**需谨慎**（启发式仅覆盖 C2/C3/C4，零稀类），优先作无监督表征预训练。
- §12 给出可直接起步、字段对齐真实 JSON 的读数据→抽特征→LightGBM 双轨评估骨架，及多模态 gating 网络骨架。
