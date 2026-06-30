# 采集数据与 MoE 上下文认证方案契合度分析

> 本文档把研究 idea「无障碍 UI 上下文 + MoE 多场景专家路由 + 持续身份认证」拆解为数据需求，并基于《当前 App 采集数据全景分析_claude.md》的字段盘点，进行三栏对照（需求 ↔ 当前采集 ↔ 差距），最后给出工程化改进清单。

## 目录

1. 研究 idea 拆解：从 step1 → step5 到数据需求
2. 8 个专家场景的关键信号需求清单
3. MoE Gating（路由）的特征需求清单
4. 单专家持续认证的特征需求清单
5. 需求 ↔ 当前采集 ↔ 差距：分维度对照
6. 三大主题深度讨论
   - 6.1 场景区分信号是否充足
   - 6.2 文本端侧丢弃（drop-all-text）对认证特征的破坏程度
   - 6.3 时间序列连续性 / 采样窗口 / 丢包
   - 6.4 app 包名 + UI 信息是否够做路由 key
   - 6.5 隐私 / 合规边界对方案的硬约束
7. 改进建议清单（必须新增 / 建议增强 / 可选优化）
8. 结论：是否能支撑 idea 落地？需要多少改造？

---

## 1. 研究 idea 拆解

研究 idea 的 5 个步骤可拆解为对**输入数据**的 5 个具体诉求：

| Idea Step | 数据诉求 |
|---|---|
| ①  无障碍读取 UI | UI 节点结构、文本、控件类型、布局、深度、可视性、事件序列 |
| ②  端侧文本丢弃（drop-all-text） | 端侧丢弃全部显示/输入文本（节点文本、输入框、content-description、窗口标题），仅保留结构/元数据与 `has_text`/`has_content_description` 存在标志；不破坏 UI 结构类认证特征 |
| ③  加密压缩传输 | LZ4 + 加密 envelope（可选 AES）；带 batch/device/session 元数据 |
| ④  MoE 路由（包名+UI → 8 专家） | (1) 路由特征：包名、UI 类型分布、IME 状态、节点直方图、视频/列表/表单/游戏 like-score；(2) 真值标签：8 类场景 |
| ⑤  专家模型做认证（场景特化） | 行为生物特征：IMU 序列、触控时序、按键节律、滑动速度、姿态稳定、屏幕方向、注视点（不可得）等；正负样本 = 同一用户 vs 跨用户 |

把这些诉求与第一份文档的字段盘点对齐后，本节作为后续比对的"需求向量"。

---

## 2. 8 个专家场景的关键信号需求清单

`docs/experiment_notes.md` 给出了启发式表，但**仅用作数据质量分析**，不是模型签名。下表把每个场景所需的"判别签名"严格拆开：

| 场景 | UI 标签 | 主导生物特征 | 关键信号 |
|---|---|---|---|
| **C0 IDLE_HOLDING**（持机静止） | `editable=0, scrollable=0, clickable_count` 低；事件极少 | 手部微抖、姿态稳定 | Accel 三轴 var、Gyro 低频幅值、姿态四元数稳定、零触控、零按键 |
| **C3 TYPING**（文本输入） | `input_method_visible=true`、`editable≥1` | 按键节律 / 击键间隔 / hold time / 多指节奏 | **逐键 down/up 时间戳、键间隔分布、IME 状态变化、击键时手部小幅 yaw**——当前**全无** |
| **C2 SCROLL_BROWSE**（信息流） | `TYPE_VIEW_SCROLLED` 高频、`scrollable>0` | 滑动持续时间、速度峰值、惯性滑、停顿模式 | **滑动距离、速度、加速度、fling 终止 ΔT**——当前只有 `down/up` 时间，**无坐标**故无速度 |
| **C1 STATIC_READING**（静态阅读） | TextView 多，事件密度低 | 微抖、轻微滚动、姿态 | Accel 低带、Gyro 慢俯仰漂移、零至偶发滚动事件 |
| **C6 VIDEO_WATCHING**（视频观看） | Surface/VideoView、`media_like_score>0.5` | 持机姿态、横竖屏切换、播放控件交互间隔 | 屏幕方向变化、姿态漂移、播放暂停 ΔT、横屏 IMU 重力分解 |
| **C5 TAP_NAVIGATION**（点击导航 / 蓝球） | `clickable_count` 显著、SurfaceView 类游戏 UI | 目标命中精度、tap-to-tap ΔT、握持小变化 | **触控点位置、压力、tap interval、tap-to-tap displacement** ——当前**仅时间戳** |
| **C4 FORM_FILLING**（表单填写） | `editable≥2`、`form_like_score=0.8`、Tabs/Sliders/Radio 混合 | 切控件 ΔT、tab/seek 行为、输入与点击切换节奏 | 控件焦点转移序列、表单跨字段时间分布 |
| **C7 GAME_OR_TILT**（手腕转动 / 倾斜操控） | 当前 UI 信号薄弱（含 Canvas） | 旋转向量、角速度峰值与节奏 | **GameRotationVector / RotationVector / 线性加速度 / 重力**——当前**未采** |

> 注：上述场景标签来自当前内置任务，但 idea 提到的 MoE 8 个专家其实是行为分类的语义簇（IDLE_HOLDING / TYPING / SCROLL_BROWSE / STATIC_READING / VIDEO_WATCHING / TAP_NAVIGATION / GAME_OR_TILT / FORM_FILLING）。两套标签一一对应，但**生产环境路由不能依赖 BUILTIN_TASK 的 `task_category`**，因为第三方采集时 `task_category=null`。

---

## 3. MoE Gating（路由）的特征需求清单

由 idea step④，路由 key 应是「app 包名 + 端侧文本丢弃后的 UI 前端结构信息」。可拆为：

| 路由维度 | 当前是否有 | 备注 |
|---|---|---|
| `app_package_name`（明文） | ✅ | `Batch.app_package_name`、每 context event 也带；**自主动前台快照（`FOREGROUND_SNAPSHOT`）修复后，无障碍服务连接时每批都有一条带前台包名 + UI 的 context event，包名不再因静态前台界面退化为 `"unknown"`**（仅服务断开时该批可能无快照） |
| `foreground_activity_class_name`（明文） | ✅ | 被动来源不稳定（仅 `WINDOW_STATE_CHANGED`）；前台快照在每批 flush 时由 `resolveActivityComponent` 补一份当前前台 Activity/Component |
| `foreground_component_name` | ✅ | |
| `input_method_visible` | ✅ | event 级 + feature 级 |
| `editable_count` / `scrollable_count` / `clickable_count` | ✅ | feature 级 |
| `node_class_histogram` | ✅ | 直方图含 RecyclerView / Surface / EditText / Button / Tab 等关键类 |
| `media_like_score` / `list_like_score` / `form_like_score` / `game_like_score` | ✅ | 启发式 0.1 / 0.8 离散值 |
| `coarse_orientation` | ✅（动态 portrait/landscape/反向/unknown） | 可区分 C5/C6 横屏段 |
| 事件 RPS（每秒事件数、滚动事件突发性） | 🟡 部分 | 可从 `event_type` 时间戳重建，但当前没有派生字段 |
| **触控密度 / 触控速率** | ❌ | 无坐标，仅 tap 时间，但能算 tap rate |
| **IMU 短时统计（方差、能量、谱中心）** | ❌（端侧未派生） | 服务端可重算，但客户端的特征器没派生 |
| **场景标签真值（用来训练 router）** | 🟡 部分 | 只有 BUILTIN_TASK 的 8 类自标；THIRD_PARTY 无标签 |

---

## 4. 单专家持续认证的特征需求清单

| 专家 | 必需输入 | 当前覆盖 |
|---|---|---|
| TYPING | 键时序（down/up/dwell/flight）、IME 高度、击键期间 IMU 小幅 yaw、击键-击键 ΔT 分布 | ❌ 完全缺击键时序；🟡 仅有 IME 可见标志与 IMU |
| SCROLL_BROWSE | 滚动轨迹、速度、惯性滑次序、滚动幅度、停顿 ΔT、IMU 抖动伴随模式 | ❌ 无坐标 / 速度；✅ IMU；🟡 仅 `TYPE_VIEW_SCROLLED` 事件可推断节拍 |
| TAP_NAVIGATION | tap 位置、压力、size、tap-to-tap displacement、命中精度 | ❌ 全无空间信息；✅ tap interval |
| GAME_OR_TILT | 角速度峰、旋转向量、重力分解、姿态 quaternion | 🟡 raw gyro/accel 有；❌ 高阶旋转向量 / 重力分量未采 |
| IDLE_HOLDING | Accel/Gyro 三轴小幅振动谱（手部微抖） | ✅ 满足 |
| STATIC_READING | 微抖 + 姿态稳定 + 偶发滚动 | ✅ 基本满足 |
| VIDEO_WATCHING | 横竖屏切换、姿态漂移、播控间隔 | ✅ 已有动态粗方向 + IMU；播控仍依赖 UI/触控时间 |
| FORM_FILLING | 焦点转移序列、混合 click+edit 节奏、跨字段 ΔT | 🟡 有焦点/选中状态布尔但无序列化轨迹 |

---

## 5. 需求 ↔ 当前采集 ↔ 差距（汇总对照表）

| 需求维度 | 当前采集 | 差距 | 影响 |
|---|---|---|---|
| 触控时间戳 | ✅ 全局触控交互 start/end + uptime/wall 时间；兼容旧 in-app down/up | – | OK |
| 触控位置 (x,y) | ❌ | 完全无 | TAP/SCROLL/FORM 专家信号塌陷 |
| 触控压力 / size | ❌ | 完全无 | TAP 风控丧失重要生物维 |
| 触摸轨迹 / VelocityTracker | ❌ | 完全无 | SCROLL 速度无法计算 |
| 第三方 App 触控 | ✅ 全局触控交互时间 | 无位置/轨迹/压力/size | 可计算触控频率与间隔 |
| 击键时序（keystroke dynamics） | ❌ | 完全无 | TYPING 专家近乎无法构建 |
| IME 状态 | ✅ event/feature 级布尔 | 无时长 / 切换序列 | TYPING 路由可，但内部模型弱 |
| Accelerometer 100 Hz | ✅ ≈95 Hz | – | OK |
| Gyroscope 100 Hz | ✅ ≈95 Hz | – | OK |
| Magnetometer 100 Hz | ✅ ≈92 Hz | – | OK |
| GameRotationVector | ❌ | 全无 | GAME_OR_TILT 用 raw IMU 替代，效果次优 |
| LinearAcceleration | ❌ | 全无 | 需端侧重力分离 |
| Gravity | ❌ | 全无 | 同上 |
| StepCounter / StepDetector | ❌ | 全无 | 行走场景识别缺失（虽然 idea 没明列） |
| Proximity / Light / Pressure | ❌ | 全无 | 持机距离、暗光环境不可知 |
| 屏幕方向（动态） | ✅ `coarse_orientation` 动态值 | 仍是粗粒度 | C5/C6 横屏场景可识别 |
| 屏幕亮度 / 是否充电 / 电量 | ❌ | 全无 | – |
| 网络类型 / 信号强度 / 运营商 | ❌ | 全无 | – |
| UI 节点结构 | ✅ ≤14 深、≤320 节点 | OK | OK |
| `viewIdResourceName` 明文 | ✅（编译期资源 ID，非用户数据） | OK | OK |
| node `text`（用户内容） | ❌（drop-all-text，恒 `null`） | 仅 `has_text` 存在标志 | 本就无 text 特征；可用 `has_text` 布尔 |
| `contentDescription`（用户内容） | ❌（drop-all-text，恒 `null`） | 仅 `has_content_description` 存在标志 | 不影响认证；可用存在标志 |
| 事件 RPS / 突发性 | 🟡 隐含可重算 | 端侧没派生 | 服务端可补 |
| 事件类型直方图 | 🟡 隐含（event_type 出现一次一次） | 端侧没派生 | 服务端可补 |
| 节点类直方图 | ✅ | – | OK |
| 启发式分类 | ✅ media/list/form/game-like | 离散粗糙；C0/C5/C7 不覆盖 | 路由 v1 可用，但弱 |
| 真值标签（场景） | 🟡 仅 BUILTIN_TASK | THIRD_PARTY 无监督 | 需弱监督 / 启发式打标 / 自监督聚类 |
| 用户身份正负样本 | 🟡 同 `device_id` 算正样本，跨 device 算负样本 | 但同设备多用户（家人共用）会污染 | 需要长期 session + 设备-用户分离设计 |
| 时间同步 | ✅ HTTP midpoint + NTP，60 s 一次 | – | OK |
| 跨批次时间戳连续性 | 🟡 `base_elapsed_nanos` 给单调时基，但 5 s 切批导致 IMU 在批边界可能有 ~200 ms 不连续 | `MAX_REPORT_LATENCY_US=200000` 允许 200 ms 报告延迟 | 需窗口拼接策略 |
| 加密 | ❌（`encryption: "none"`） | LZ4 + Base64，依赖 HTTPS | 与 idea step③「加密」表述不符 |
| 规则热更新 | ➖ 已移除（drop-all-text，App 不再拉 `/api/v1/rules`） | 文本一律丢弃，无需脱敏规则 | 不再适用 |

图例：✅ = 当前已满足；🟡 = 部分满足或需后处理；❌ = 完全缺失。

---

## 6. 三大主题深度讨论

### 6.1 8 个场景的区分信号是否充足？

**结论：当 `collection_source = BUILTIN_TASK` 时，靠 `task_category` 字段可以无歧义打标；但 idea 的应用场景是 `THIRD_PARTY_APP`，路由完全要从 UI + IMU 推断，此时 8 类的可分性差异巨大。**

按区分难度从易到难：

| 场景对 | 区分能力 | 依据 |
|---|---|---|
| C3 TYPING vs others | ★★★★☆ | `input_method_visible` + `editable>0` 几乎唯一 |
| C2 SCROLL_BROWSE vs others | ★★★☆☆ | `TYPE_VIEW_SCROLLED` 事件 + `scrollable>0` |
| C6 VIDEO_WATCHING vs others | ★★★☆☆ | Surface/Player 类名 + media_like_score=0.8；但 video 内嵌于 Feed 时会被误判 |
| C4 FORM_FILLING vs C3 TYPING | ★★☆☆☆ | 都有 `editable`；区分靠 `editable≥2` 与 click 比例，弱启发式 |
| C0 IDLE_HOLDING vs C1 STATIC_READING | ★☆☆☆☆ | UI 几乎一致；只能靠 IMU 微小差异 |
| C5 TAP_NAVIGATION vs C4 / C6 | ★★☆☆☆ | 有全局触控节奏和动态屏幕方向，但仍无触控位置/命中精度 |
| C7 GAME_OR_TILT vs C6 / others | ★☆☆☆☆ | 强依赖旋转向量 / 角速度峰，当前缺高阶传感器 |

样本验证：测试样本是 C5 任务，但 `estimated_context_category="C5"` 实际是直接抄 `task_category`，而非启发式推断——切到 THIRD_PARTY_APP 后，启发式只会落到 `UNKNOWN`，因为 C5 既无 `editable` 又无 `scrollable`、`mediaLikeScore` 也是 0。

### 6.2 文本端侧丢弃（drop-all-text）对认证特征的破坏程度

**结论：当前文本处理对结构类认证特征几乎是温和的（UI 结构、控件状态、节点直方图全部保留），但对 TYPING 专家近乎致命，对 TAP/SCROLL 完全致命——而这主要源于禁采触控坐标/按键时序，并非文本丢弃本身。**

> 前提：自 drop-all-text 起，所有显示/输入文本一律端侧丢弃，`text`/`text_redacted`/`content_desc_redacted`/`window_title_redacted` 恒 `null`，仅保留 `has_text`/`has_content_description` 存在标志。下表"对认证的影响"按当前行为评估。

| 文本/隐私处理 | 对认证的影响 |
|---|---|
| Password 节点整棵丢弃 | 良性（敏感性合规要求） |
| Editable 文本丢弃（恒 `null`） | 良性（仍保留 editable 状态、IME 可见性、`has_text` 标志） |
| node `text` 一律丢弃（恒 `null`） | 良性：本就不依赖 UI 文字内容做认证；可用 `has_text` 布尔与节点直方图替代 |
| `content_desc_redacted` 一律丢弃（恒 `null`） | 损失少量语义信号；但 `viewIdResourceName`（编译期资源 ID）+ `has_content_description` 仍在，影响小 |
| `bounds_grid = pixel÷24` 离散化 | 弱化空间精度，但保留相对位置——尚可用于"按钮在屏幕哪个区"的粗特征 |
| **禁止逐字 text-change 事件** | ✗ TYPING 专家无法做 keystroke dynamics |
| **禁止触控坐标 / 压力 / size** | ✗ SCROLL/TAP/FORM 专家无法做 trajectory / pressure dynamics |
| **触控坐标 / 压力 / size 仍不采** | ✗ THIRD_PARTY_APP 模式只有全局触控时间，空间与压力相关专家仍无信号 |
| 文本内容（含长文本）一律丢弃 | 失去"文本量"语义，但可通过节点数、`has_text` 占比与控件直方图估算 |

注意：文本端侧丢弃的"温和"与触控/击键禁采的"致命"是相对认证特征而言。从隐私合规视角，drop-all-text 完全符合 `requirements_matrix.md` APP-09 的硬约束，且进一步消除了旧构建 `text` 通道残留 UGC 的隐私风险，**这正是必须直面的张力**。

### 6.3 时间序列连续性 / 采样窗口 / 丢包

| 维度 | 当前实现 | 评估 |
|---|---|---|
| IMU 采样率 | 名义 100 Hz / 实测 ≈95 Hz | 可用，已足够大多数行为生物 |
| 批次切分 | 每 5 s flush 一批 | 5 s 是常用 window，但跨批拼接时要靠 `base_elapsed_nanos` + `timestamp_elapsed_nanos` 做单调对齐 |
| 报告延迟 | `MAX_REPORT_LATENCY_US=200000`（200 ms） | 批边界可能有最多 200 ms 的乱序样本；需服务端按 nanos 重排 |
| Context event 节流 | scroll 120/content 180/window 250 ms 节流 | 高频 UI 信息有压缩损失，但批量节流是合理 |
| 锁屏 / 息屏 gating | 立刻停采、批量上传，重新解锁可复用同一 `task_session_id` | 用 `gatedResume=true` 标记 |
| 上行队列 | 200 MB 上限 / FIFO 替换 / 5 s 起的指数退避 | 安全 |
| 时钟漂移 | NTP/HTTP midpoint，60 s 一次 | OK |
| 跨设备时间对齐 | 服务端可用 `wall_time_estimated_millis` 但精度受设备时钟 + serverOffset 影响 | 跨设备建模时需统一对齐 |

**潜在风险**：5 s 窗口对触控类场景（如 C5 蓝球）会跨多个 tap；但样本里 6 个触控 + 1671 个 IMU 一起按时间戳排序后可重建。对窗口型模型，建议 server 端将多批拼接为更长 window（10-30 s）。

### 6.4 app 包名 + UI 信息作为路由 key 是否足够？

**结论：能识别"应用类型"（聊天/视频/输入法/游戏），但不足以唯一识别"行为场景"。需要叠加 UI 节点直方图 + IMU 短时统计 + IME 状态作为路由特征。**

举例：

- **前台快照对路由 key 的实际增益**：修复前若某 5 s 窗口前台界面是静态的（无被动事件），整批 `context_events`/`app_package_name` 会缺失，路由器对该窗口无 key 可用；现在 `FOREGROUND_SNAPSHOT` 在每批 flush 时主动补一条带前台包名 + UI 节点直方图的事件（服务连接时），保证窗口级路由特征**至少有一帧前台上下文**。这并未新增信号维度（脱敏与节点上限不变），但显著提升了第三方应用正常使用时路由 key 的覆盖率与连续性。
- `com.tencent.mm`（WeChat）的 UI 可以是 IDLE（聊天列表静置）、SCROLL（信息流下拉）、TYPING（聊天输入）、VIDEO（点开视频号）、TAP（小程序）——同一个 `app_package_name` 横跨至少 5 个场景。
- 路由 key 只有 `app_package_name + Activity + UI feature` 时，需要更细的 Activity 切换信号；当前 `Activity` 只在 `TYPE_WINDOW_STATE_CHANGED` 时刷新，子窗口（Fragment、对话框、IME）的切换不能用 Activity 名跟踪。
- 用 `viewIdResourceName` 直方图（明文）是一个强补充信号，但对 RecyclerView / Compose 这类生成式 UI，viewId 缺失（样本 41% 节点 `viewIdResourceName=null`）。

可行的强化方向：把 5-30 s 窗口内的 `viewIdResourceName` 集合做布隆过滤或 hash trick → 路由 embedding。

### 6.5 隐私 / 合规边界对方案的硬约束

`requirements_matrix.md` APP-09 与 `privacy_model.md` 明确：

> No performAction, gestures, screenshots, auto-click, raw input-field text, per-key timestamps, key intervals, key hold durations, touch positions, or touch trajectories.

这是产品宣示的硬红线。对 idea 而言：

- 想做 keystroke dynamics → 违反"per-key timestamps / key intervals / key hold durations 不采集"。
- 想做 swipe trajectory 鉴权 → 违反"touch positions / touch trajectories 不采集"。
- 想用截图做视觉认证 → 违反"screenshots 不采集"。

**这一节是 idea 落地的最大约束**。下面的改进建议必须在该约束下重新构思——例如不采每键时序，但可以采"该 5 s 窗口内总击键次数"或"editable focus → next focus 的 ΔT"，作为弱版本的 typing rhythm。

---

## 7. 改进建议清单

按"必须新增 / 建议增强 / 可选优化"三档列出。每条注明：理由、实现位置、隐私影响与对应文本/隐私处理、优先级与预估难度。

### 7.1 必须新增（不做无法达成 idea step④⑤）

| # | 改动 | 理由 | 实现位置 | 隐私影响 / 脱敏 | 优先级 | 难度 |
|---|---|---|---|---|---|---|
| M1 | **动态 `coarse_orientation` 已实现，后续只需在训练侧使用** | C5/C6 横屏场景可识别 | `ResearchAccessibilityService` 读取 orientation/rotation，`ContextFeatureExtractor` 复用事件字段 | 无新增 PII | Done | 低 |
| M2 | **加入 GameRotationVector / RotationVector / LinearAcceleration / Gravity 传感器** | GAME_OR_TILT 专家、VIDEO 姿态、IDLE 微抖辨识全部需要 | `SensorCollector.start` 注册 `Sensor.TYPE_GAME_ROTATION_VECTOR` 等；扩展 `SensorSample.sensorType` 枚举与服务端 schema | 无新增 PII | P0 | 低 |
| M3 | **端侧派生 IMU 短时统计窗口特征**（方差、能量、谱中心、行为带能量比） | 减轻服务端计算与带宽；为路由器提供轻量特征 | 新增 `ImuFeatureExtractor`，每 1 s 输出一个 vector，并入 `context_features` 或新建 `imu_features` 数组 | 仅是聚合，无新增 PII | P0 | 中 |
| M4 | **派生路由用窗口聚合特征**：每 5 s 的 event RPS、scroll burst 计数、tap rate、IME 切换次数、Activity 切换次数 | 路由器需要时间序列摘要，不能逐事件查 | 新增 `RoutingFeatureExtractor`，输出到 `batch.context_features` 末尾或单独 `batch.routing_features` | 无 | P0 | 中 |
| M5 | **路由真值标签的弱监督管线**：服务端用启发式规则把样本打成 8 类，作为路由训练初始标签 | THIRD_PARTY_APP 模式无 task_category | 服务端：`label_router.py`；按 `experiment_notes.md` 表 + 改进后特征推断 | 无 | P0 | 中 |
| M6 | **服务端跨批拼接窗口 (10-30 s) 与时间对齐**：按 `device_id`+`session_id`+`base_elapsed_nanos` 重建长窗口 | 5 s 太短，触控类场景需要更长上下文 | 服务端 ETL：滚动窗口构造 + IMU 重排 | 无 | P0 | 中 |
| M7 | **应用层加密**：要么补 AES-GCM payload 加密，要么明确文档把 idea step③ 改成"压缩 + HTTPS"。当前 `encryption=none` 与 idea 描述不符 | 与 idea 描述对齐 | `JsonCodec.buildEnvelopeWithMetrics` 增加 AES 选项；服务端 `Envelope.algorithm` 增加 `AES_GCM+LZ4_FRAME+JSON` | 反向降低明文存储风险 | P0 | 中 |

### 7.2 建议增强（不做会显著降低认证性能，但不致命）

| # | 改动 | 理由 | 实现位置 | 隐私影响 / 脱敏 | 优先级 | 难度 |
|---|---|---|---|---|---|---|
| E1 | **聚合后的击键节律弱特征**：不采每键时序，但采 5 s 窗口内的"editable focus 持续秒数、editable 内 input_method_visible 累计时长、focus→focus 切换次数与平均 ΔT" | TYPING 专家可在窗口尺度上做粗 typing rhythm，仍满足 APP-09 | `ContextFeatureExtractor`：基于 `event_type` 序列派生 | 不暴露逐键时间戳 | P1 | 中 |
| E2 | **聚合后的滑动节律弱特征**：5 s 窗口内 scroll event 数、平均 ΔT、起止时间分布、scroll → static 切换次数 | SCROLL 专家轻量替代 trajectory | 同 E1 | 不暴露坐标 | P1 | 中 |
| E3 | **聚合后的点击节律特征**：tap interval 分布的 P50/P95、cluster 内 tap 数 | TAP 专家窗口级特征 | 同 E1 | 不暴露坐标 | P1 | 低 |
| E4 | **viewId hash 直方图 + 节点 fingerprint**：每窗口内 viewIdResourceName 的 MinHash / SimHash + 维度数 | 路由器 embedding 输入 | 端侧或服务端皆可 | 仍可定位 UI 上下文，可适配匿名化 | P1 | 中 |
| E5 | **`contentDescription` 的存在/形状元统计**：在 drop-all-text 前提下（内容恒 `null`），仅额外记录"该节点 contentDescription 长度、字符类型分布"等不含内容的元统计（`has_content_description` 已有） | 视频类 / 表单类语义补强 | `RedactionEngine.sanitizeNode` 增加不含内容的元统计字段 | 不增加任何新明文/内容 | P2 | 低 |
| E6 | **节点类型直方图 → 路由专用编码**：把 `node_class_histogram` 离散化为定长 vector（例如 32 维 hashing trick） | 与 ML 路由对接更顺 | 端侧或服务端 | 无 | P2 | 低 |
| E7 | **批次诊断里加入 IMU 缺口指标**：`max_inter_sample_gap_ms`、`samples_per_second_actual` | 用于训练数据筛选与丢包检测 | `JsonCodec.batchToJson` | 无 | P2 | 低 |
| E8 | **设备 / 用户分离标签**：在服务端为同一 `device_id` 长期会话引入"主用户假设"，把跨 `session_id` 的稳定模式作为正样本 | 训练正负样本构造 | 服务端 ETL | 无新增采集 | P1 | 中 |
| E9 | **Step counter / 行走识别**：注册 `TYPE_STEP_DETECTOR`、`TYPE_STEP_COUNTER` | 帮助区分"走路时持机静止 vs 真正静止"；可在 IDLE_HOLDING / SCROLL 中提升精度 | `SensorCollector` | 仅采计步，不上行步频细节即可 | P2 | 低 |
| E10 | **横竖屏切换事件**：监听 `Configuration` change，作为 context event 入批 | C6/C7 关键信号 | `MainActivity` 或服务侧广播 | 无 | P1 | 低 |
| E11 | **`UsageStatsManager` foreground 窗口跟踪（备选）** | 当无障碍未授权时仍能获包名 | 需 `PACKAGE_USAGE_STATS` 权限，**要慎重**：可能违反隐私边界 | 用户额外授权 | P3 | 中 |

### 7.3 可选优化（锦上添花）

| # | 改动 | 理由 | 隐私影响 | 优先级 |
|---|---|---|---|---|
| O1 | 实时端侧 InferenceEngine：把路由器和轻量专家放端侧，减少服务端推理压力与隐私暴露 | 长期方向 | 反向降低数据上行 | P3 |
| O2 | 联邦学习 / 差分隐私 | 在不暴露原始 batch 的情况下训练专家 | 反向降低明文风险 | P3 |
| O3 | `Sensor.TYPE_PROXIMITY` / `TYPE_LIGHT` / `TYPE_AMBIENT_TEMPERATURE` 上下文 | 环境上下文（口袋 vs 手持） | 极弱 PII | P3 |
| O4 | 节点 bounds 的细粒度 grid（`÷ 4` 而非 `÷ 24`） | 不暴露坐标，但提供更细布局信息 | 弱化但仍是 grid | P3 |
| O5 | 给路由器提供"app 类别 embedding"（基于 PlayStore 类别字典） | 路由先验 | 不上行新数据 | P3 |
| O6 | 在 `redaction_summary` 中加入 `dropped_*` 计数的分布统计 | 文本丢弃覆盖审计 | 无 | P3 |
| O7 | `coarse_orientation` 已扩展为 portrait/landscape/portrait_reverse/landscape_reverse/unknown | 训练侧可做 one-hot | 无 | Done |

---

## 8. 结论：当前数据是否能支撑 idea 落地？

### 8.1 总判断

**当前数据可以支撑 idea 的"原型骨架 + 部分专家"上线，但无法支撑 idea step④⑤ 的全谱 8 专家持续认证。** 主要瓶颈不在采集量（IMU + UI 已经相当完整），而在**信号维度的对齐**：

1. **触控通道仍是弱信号**——已是全局触控交互时间，但仍无坐标 / 压力 / size。这能支持 tap rate/interval，却不能支持 TAP / SCROLL / FORM / GAME 的空间与压力特征。
2. **击键通道为零**——APP-09 硬约束下，TYPING 专家不能走传统 keystroke dynamics，必须改为窗口级聚合替代特征（建议 E1）。
3. **高阶 IMU 信号缺失**——GameRotationVector / LinearAcceleration / Gravity 仍缺失；屏幕方向已由动态 `coarse_orientation` 补齐，横屏识别改善但转腕仍依赖 raw IMU（建议 M2）。
4. **路由 ground truth 仅限内置任务**——THIRD_PARTY_APP 没有 8 类标签，需要弱监督管线（建议 M5）。
5. **"加密"与代码事实不符**——`encryption: "none"`，需要补 AES 或修订 idea 描述（建议 M7）。

### 8.2 落地阶段化建议

| 阶段 | 目标 | 必备改造 |
|---|---|---|
| **阶段 A：补齐采集与标签** | 让数据维度匹配 idea | M1（动态 orientation）、M2（高阶 IMU 传感器）、M3（IMU 派生特征）、M4（窗口聚合特征）、M5（弱监督打标）、E10（横竖屏事件） |
| **阶段 B：路由器 v1** | 启发式 + 轻量 GBDT 路由器达到 80%+ top-1 | M6（跨批窗口）、E4/E6（节点 / viewId embedding）、E8（设备-用户分离） |
| **阶段 C：专家逐个上线** | IDLE / VIDEO / STATIC_READING / GAME（不依赖触控的） 先上线 | – |
| **阶段 D：弱触控替代特征** | TYPING / SCROLL / TAP 专家使用窗口级特征 | E1/E2/E3 |
| **阶段 E：合规增强 + 加密** | 修复 encryption 不一致；考虑联邦 / DP | M7、O1、O2 |

### 8.3 一句话结论

**当前数据是「IMU 完备 + UI 结构完备 + 触控严重欠缺 + 击键完全禁采 + 路由真值仅限内置任务」的不对称配置；要进入 MoE 路由 + 8 专家阶段，必须先做改造 M1-M7（约 2-4 周工程量），随后弱监督管线 E1-E8 才能让"无触控坐标 / 无按键时序"约束下的认证体系跑通。**
