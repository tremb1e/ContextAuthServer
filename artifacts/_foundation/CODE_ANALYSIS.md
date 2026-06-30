# 基础事实材料 A —— 代码侧权威分析（只读，供文档撰写引用）

> 本文件由 code-analyst 子代理产出，覆盖 `android-app/src/main` 全量源码、清单/`res/xml` 配置、关键单测、服务端 `server/app/*`、`server/.../default_rules.json`，以及仓库自带设计文档。引用格式 `path:line`，推断标注 `(inferred)`。撰写正式文档时以本文件 + DATA_ANALYSIS.md 为权威依据。

---

## 0. 执行摘要 / schema 校正

上传批次 schema 基本如下，`JsonCodec.kt` 中的精确校正：

- 批次除 `task_category` 外，还并列输出 `task_id`、`task_sequence`、`task_name`、`task_intuitive_description`、`task_session_id`、`task_started_at_wall_millis`、`task_elapsed_seconds_at_batch_end`，以及 `foreground_activity_class_name` / `foreground_component_name`（`JsonCodec.kt:35-46`）。`task_name`/`task_intuitive_description` 仅上传英文（`taskNameEn`、`intuitiveDescriptionEn`）。
- 顶层存在 **`touch_events[]`**：仅全局触摸交互起止时间戳（START/END），**无坐标、无压力**（`JsonCodec.kt:55,133-139`）。
- `batch_duration_seconds` 被**重算**为 `(ended-started)/1000`（`JsonCodec.kt:38`），并非恒为 5（息屏门控/停止 flush 可更短）。
- 节点字段 `viewIdResourceName` 以该 camelCase 键原样输出（`JsonCodec.kt:158`）；另有 `input_type_category`：editable 时为 `"text"`，否则 `null`（`JsonCodec.kt:171`）。
- `keyboard_visible_estimated` 为派生特征 = `inputMethodVisible || editableCount>0`（`JsonCodec.kt:192`）；`input_method_visible` 同时出现在事件级与特征级。
- `estimated_context_category` 取值 `C2..C7`/`UNKNOWN`；**启发式从不产出 C0/C1/C5/C7**（仅任务标签能产出，见 §3）。
- `bounds_grid` 量化为 **屏幕像素整除 24**（24px/格，左上原点）（`ResearchAccessibilityService.kt:121-126`）；非定长 NxN，原始像素/24，受分辨率影响、取值无界。

---

## 1. 源码逐文件角色表（`com/contextauth/**`）

**根**
- `ContextAuthLabApplication.kt`：`Application`，惰性持有唯一的 `CollectionCoordinator` 与 `SupervisorJob`+`Dispatchers.Default` 的 `collectionScope`；`onCreate` 调 `coordinator.startRuntime(scope)`（:11-17）。

**`accessibility/`**
- `ResearchAccessibilityService.kt`：`AccessibilityService`。接收 a11y 事件，把前台应用窗口树遍历为**零文本**的 `NodeSnapshot`（drop-all-text），向 `AccessibilityEventBus` 发 `ContextEventSnapshot`，向 `TouchEventBus` 发触摸起止；暴露静态 `captureForegroundSnapshot()` 供每批主动快照。持有捕获期唯一的 `RedactionEngine`（:25）。

**`core/`（核心几乎都在此）**
- `Models.kt`：所有数据类（`SensorSample`、`NodeSnapshot`、`ContextEventSnapshot`、`TouchEventSnapshot`、`ContextFeature`、`Batch`、`PayloadEnvelope`、`UiState`、`DiagnosticsState`、`AppSettings`、`ClockSyncState`），**`TaskCategory` 枚举 C0–C7**（标签来源），`RuleDefaults`，常量 `DEFAULT_SERVER_URL="https://cca.macrz.com"`、`SERVER_STUDY_SALT="Continuous_Authentication"`。
- `ContextFeatureExtractor.kt`：单 `ContextEventSnapshot` → 单 `ContextFeature`（计数、*_like_scores、直方图、`estimated_context_category`）。**路由特征派生核心。**
- `RedactionEngine.kt`：`RawNodeSnapshot`/`RedactionSummary` + 引擎。**隐私核心**——drop-all-text：丢弃全部节点文本/contentDescription/标题，仅产出布尔 `hasText`/`hasContentDescription` 与 `dropped_*` 计数。（旧的 `RedactionPatternRule`/`RedactionPolicy`/`RedactionPolicyStore` 已**移除**。）
- ~~`RuleUpdateClient.kt`~~：**已移除**（drop-all-text 后 App 不再拉取 `/api/v1/rules`，无云端脱敏规则更新）。
- `SensorCollector.kt`：100Hz 注册 accel/gyro/mag，缓冲 `SensorSample`，计算实时 Hz / 丢样。
- `CollectionCoordinator.kt`：编排状态机——权限门控、5s 批循环、息屏门控、快照合并、批构建、上传、时钟同步、失败队列回放（drop-all-text 后**不再有规则检查/拉取**）。**全 App 枢纽。**
- `CollectionControlBus.kt`：前台 `Service` 请求停止的 `SharedFlow`。
- `AccessibilityEventBus.kt`：两个 `SharedFlow`（上下文事件、触摸事件，`DROP_OLDEST` 512 缓冲）+ `AccessibilityCollectionGate.active` 门控所有捕获。
- `JsonCodec.kt`：批次→JSON 序列化（手写）、LZ4-frame 压缩、SHA-256、信封构建。声明 `ALGORITHM="LZ4_FRAME+JSON"`，写 `"encryption":"none"`。
- `Uploader.kt`：POST `/api/v1/ingest`，磁盘失败队列、死信、重试/退避（`FailureQueuePolicy`）。
- `QueueMetadataStore.kt`：上传队列 + 历史的 SQLite。
- `ClockSyncService.kt`：NTP（中国主机）+ HTTP 中点 `/api/v1/config` 回退；`ClockSyncMath`、`NtpClient`。
- `ConnectionTester.kt`：GET `/ready` 健康检查。
- `DeviceIdProvider.kt`：`device_id = HMAC-SHA256(salt, ANDROID_ID)` hex，存 `EncryptedSharedPreferences`。
- `SettingsStore.kt`：`SharedPreferences` 支撑的 `AppSettings`；**batch=5s、task=30s 硬固定**（:17-18,56-57）。
- `SamplingConfig.kt`：`SAMPLING_RATE_HZ=100`、`SAMPLING_PERIOD_US=10_000`、`MAX_REPORT_LATENCY_US=200_000`。
- `CoarseOrientation.kt`：Configuration 朝向 + 显示旋转 → `portrait/landscape/portrait_reverse/landscape_reverse/unknown`。
- `MazeLogic.kt`：倾斜迷宫模型；**出货流程中为死代码**（无任务使用）。`ui/MazePhysics.kt` 同为未用物理。
- `Localization.kt`：`LocaleText.pick(zh,en)` 双语助手。

**`service/`**
- `DataCollectionService.kt`：前台 `dataSync` 服务；Activity 不在时维持采集，处理 `ACTION_STOP_COLLECTION`。

**`ui/`**
- `MainActivity.kt`（1775 行）：全部 Compose UI（同意/引导/主页/任务/任务执行/设置/研究者/诊断）+ **8 个内建引导任务 UI**（C0 时钟 / C1 阅读 / C2 信息流 / C3 抄写框 / C4 设置控件 / C5 全屏蓝球游戏 / C6 全屏视频 / C7 手腕引导）。监督标签生产者。
- `MainViewModel.kt`：瘦 `AndroidViewModel`，委托协调器。
- `WristGuideAnimation.kt`：C7 三面板手腕动作示教（纯 UI）。
- `HiddenGestureDetector.kt`：7 连击 / 3s 长按解锁隐藏研究者页。
- `MazePhysics.kt`：未用倾斜迷宫物理。
- `theme/Theme.kt`：Material3 主题。

---

## 2. 采集管线端到端

### 2.1 `ResearchAccessibilityService.kt`
- **处理事件类型**（清单 `res/xml/research_accessibility_service.xml`）：`typeWindowStateChanged|typeWindowsChanged|typeWindowContentChanged|typeViewScrolled|typeViewClicked|typeViewLongClicked|typeViewFocused|typeViewSelected|typeTouchInteractionStart|typeTouchInteractionEnd`，标志 `flagRetrieveInteractiveWindows|flagReportViewIds`，`canRetrieveWindowContent=true`，`notificationTimeout=150`。
- 全部 `runCatching` 包裹并**受 `AccessibilityCollectionGate.active` 门控**（:43）。
- `typeTouchInteractionStart/End` 拆入 `TouchEventBus`，仅 `eventTime`/估计墙钟/`collectedAtWallMillis`，**无坐标无压力**（:158-176）。
- `TYPE_VIEW_TEXT_CHANGED` **整体丢弃**（`shouldProcess` 返回 false，:203）——反击键措施；其文本无论如何在 drop-all-text 下也不会输出。
- **节流**（:202-218）：scrolled ≥120ms、content-changed ≥180ms、windows-changed ≥250ms，其余不节流。
- **树遍历 root_nodes**（`collectApplicationRoots` :223-253，`traverse` :86-141）：遍历 `windows`，仅保留根包名匹配前台包的 `TYPE_APPLICATION` 窗口（`belongsToForeground` :255-258）；无果则回退 `rootInActiveWindow`。DFS，`MAX_DEPTH=14`、`MAX_NODES_PER_EVENT=320`（:341-342,93）。
- `depth`=递归深度（应用窗口路径以窗口序号播种，:235；活动窗口回退为 0，:247，故深度为窗口相对，inferred quirk）。`child_count`=`node.childCount`（原值，:119）。`node_id`=`"${depth}_${node.hashCode()}"`（:104，**跨快照不稳定**）。`bounds_grid`=`getBoundsInScreen` 后各值整除 24（:99,121-126，**原始像素、无界、随分辨率变化**）。
- **密码子树剪枝**：`isPassword` 节点丢弃且**子树不再遍历**（:131）。`editable` 由 `isEditable` 或类名含 "EditText"/"TextInput" 推断（:96-98）。`actions_summary` ⊆ {CLICK,LONG_CLICK,SCROLL,CHECK,EDIT}（:186-192）。每节点入列前过 `redactionEngine.sanitizeNode`（:129）。
- **`text` / `text_redacted` / `content_desc_redacted` / `has_text` / `has_content_description`**（`RedactionEngine.sanitizeNode` :50-78，序列化 `JsonCodec.kt:175-181`）—— **drop-all-text**：
  - **所有节点（无论是否可编辑）** → `text=null`、`text_redacted=null`、`content_desc_redacted=null`（文本/描述/标题一律不输出）。
  - 引擎判断原始 `node.text`/`contentDescription` 是否非空，分别写入布尔 `has_text` / `has_content_description`（:57-58），仅是"是否存在"标志，不含内容。
  - 旧的"保留型脱敏 + 占位符替换"——`visibleTextForTarget` 对非编辑节点保留普通词、`redactTextForTarget` 把 content-desc/标题折叠为 `<TEXT_REDACTED>` 的不对称设计——**已整体移除**。
- **`FOREGROUND_SNAPSHOT` vs 反应式**：每批 `buildBatch` 调 `captureForegroundSnapshot()`（:285-306）产生合成 `eventType="FOREGROUND_SNAPSHOT"` 事件并追加（`CollectionCoordinator.kt:576-577`），保证每批 ≥1 个 UI 上下文事件。
- **前台包/活动与 API30 bug（已确认并修复）**：`currentCoarseOrientation()` 原调 `Service.getDisplay()`，API≥30 抛 `UnsupportedOperationException`，被外层 `runCatching` 吞掉 → UI 捕获全失败、只剩传感器。修复（:330-337）改用 `DisplayManager.getDisplay(DEFAULT_DISPLAY)` 并默认 `UNKNOWN`。回归测试 `ResearchAccessibilityServiceCaptureTest.kt`。**2026-06-11 之前的数据集为传感器-only。**

### 2.2 `SensorCollector.kt`
- 仅 `TYPE_ACCELEROMETER`、`TYPE_GYROSCOPE`、`TYPE_MAGNETIC_FIELD`（:64-66）。**无 RotationVector/GameRotationVector/Gravity/LinearAcceleration/StepDetector。**
- 请求 100Hz（`SAMPLING_PERIOD_US=10_000`）+ 200ms 批延迟；实测受设备 minDelay 限制（:121-137）。
- 每样本（`SensorSample` :101-109）：`sensorType`、`timestampElapsedNanos`（elapsed-realtime ns）、`wallTimeEstimatedMillis`（时钟同步校正的墙钟）、`x,y,z`、`accuracy`。`onAccuracyChanged` 空实现。

### 2.3 `ContextFeatureExtractor.kt`（精确公式，引用）
```kotlin
val editable = nodes.count { it.editable }
val scrollable = nodes.count { it.scrollable }
val clickable = nodes.count { it.clickable }
val passwordSeen = nodes.any { it.password }
val histogram = nodes.mapNotNull { it.className?.substringAfterLast('.') }
                     .groupingBy { it }.eachCount()
val mediaScore = if (histogram.keys.any { it.contains("Surface", true) || it.contains("Player", true) }) 0.8 else 0.0
val listScore = if (scrollable > 0) 0.8 else 0.1
val formScore = when { editable >= 2 -> 0.8; editable == 1 && clickable > 2 -> 0.6; else -> 0.1 }
val gameScore = when { taskCategory == TaskCategory.C5 -> 0.8; mediaScore > 0.5 && clickable <= 1 -> 0.4; else -> 0.1 }
```
- `media_like_score`：类名含 "Surface"/"Player" 才 0.8，否则 0.0 → **真实 `VideoView` 不命中，C6 恒为 0.0（缺陷）**。
- `list_like_score`：有可滚动节点 0.8，否则 0.1。
- `form_like_score`：≥2 editable→0.8；1 editable 且 >2 clickable→0.6；否则 0.1。
- `game_like_score`：任务 C5→0.8；media 且 ≤1 clickable→0.4；否则 0.1。**第三方模式永远到不了 0.8。**
- `keyboard_visible_estimated`（JSON 期算，`JsonCodec.kt:192`）= `inputMethodVisible || editableCount>0`。

**`estimated_context_category` 精确规则（引用，:31-39）：**
```kotlin
val estimated = when {
    taskCategory != null -> taskCategory.name        // 内建任务：标签优先
    editable >= 2 -> "C4"
    event.inputMethodVisible || event.eventType == "TYPE_VIEW_TEXT_CHANGED" || editable == 1 -> "C3"
    event.eventType == "TYPE_VIEW_SCROLLED" || scrollable > 0 -> "C2"
    mediaScore > 0.5 -> "C6"
    clickable > 4 -> "C4"
    else -> taskCategory?.name ?: "UNKNOWN"
}
```
要点：有任务标签时 `estimated == task_category`（故"一致率"是构造性的，非验证）；`editable==1` 恒判 C3（单字段表单被误标打字）；`editable>=2`→C4；`TYPE_VIEW_TEXT_CHANGED` 分支永不触发（已被前置过滤）；第三方仅能产出 **C2/C3/C4/C6/UNKNOWN**，从不 C0/C1/C5/C7。媒体分支因 §2.3 缺陷实际从不命中。

### 2.4 `CollectionCoordinator.kt` 批处理/窗口/门控
- **启动门控** `canStart()`（:319-325）：同意 ∧ 合法 64-hex device_id ∧ 无障碍已开 ∧ 电池白名单 ∧ 通知允许 ∧ 屏亮且解锁。
- **5s 循环**（:229-234）：`delay(batchSeconds*1000)` 后 `flushBatch()`。
- **快照合并**（:576-577）：`allEvents = events + snapshot`（快照末位）；前台包/活动/组件取最后一个非空值（:589-591，通常快照胜）。
- **空批保护**（:373）：传感器+触摸+上下文全空则丢弃。
- **息屏门控**（:497-520）：`SCREEN_OFF`→暂停+flush `PAUSED_BY_SCREEN_OFF`；`SCREEN_ON`+锁屏→`PAUSED_BY_LOCKED`；`USER_PRESENT`→`resumeAfterUnlock`。
- **门控恢复**（:261-267,213-222）：解锁后沿用同一 `taskSessionId`/`taskStartedAt`，`gatedResume=true`。`session_id = taskSessionId ?: collectionSessionId`。
- **来源**（:588）：有 `taskCategory` 则 `BUILTIN_TASK`，否则 `THIRD_PARTY_APP`。

### 2.5 传输 —— `Uploader.kt` + `JsonCodec.kt`
- 序列化：手写 JSON（无库），手工转义（:207-240）。
- 压缩：`LZ4FrameOutputStream` 压 UTF-8 JSON（:114-118）；诊断 `"compression":"lz4_frame"`。
- **加密：确为 `none`。** payload 写 `"encryption":"none"`（:66），信封 `algorithm="LZ4_FRAME+JSON"`（:10）；**无任何 AES/内容加密**（`PayloadEnvelopeTest` 断言、服务端 `BatchDiagnostics.encryption: Literal["none"]`、同意书均确认）。App 内唯一"加密"是本地 device-id 存储用 `EncryptedSharedPreferences`，与载荷无关。**加密应插入处**：`buildEnvelopeWithMetrics`（:80-87）的 `lz4Frame(jsonBytes)` 与 base64 之间——加密压缩后字节、改 `ALGORITHM`、给 `PayloadEnvelope`/服务端 `Envelope` 加 key/nonce 字段（服务端当前把 algorithm 硬钉为单一字面量）。
- 信封（`envelopeToJson` :103-112）：`algorithm`、`payload_base64`、`payload_sha256_hex`（**压缩后字节**的 SHA-256，仅完整性）、`device_id`、`batch_id`、`rule_version`、`rule_hash`、`created_at_wall_millis`（=批 `startedAtWallMillis`）。
- POST `/api/v1/ingest`（`application/json`）。OkHttp 超时 connect5/read8/write8/call12 s。
- 失败队列：可重试失败写 `filesDir/upload_queue/`，SQLite 元数据；`wifiOnly && !wifi` → `queueOnly`。重放每 15s；退避基 5s 指数封顶 5min 全抖动；可重试=408/429/≥500/非 HTTP 错误；20 次后死信；队列上限 200MB FIFO。

### 2.6 支撑服务
- `DeviceIdProvider`：`device_id=hex(HMAC-SHA256(key=serverStudySalt, msg=ANDROID_ID))`；`EncryptedSharedPreferences` 缓存；无 IMEI/serial/MAC。
- `ClockSyncService`：每 60s 中国 NTP，失败回退 HTTP 中点；`offset=serverTime−(t0+rtt/2)`，馈入传感器墙钟估计。
- `ConnectionTester`：GET `/ready`。`QueueMetadataStore`：SQLite（`upload_queue`+`upload_history`）。

---

## 3. 上下文类别体系 C0–C7（权威表）

**`Models.kt:21-112` 的 `TaskCategory` 枚举**，各项带双语 `intuitiveDescription`/`taskName`/`subtitle`，`sequence=ordinal`。**服务端校验** `TASK_CATEGORIES={C0..C7}` 并强制 `task_sequence==int(task_id[1:])`、`task_id==task_category`（`schemas.py:328-331`）。

| 代码 | 直觉(zh) | 任务名 | 含义/行为 | 内建 UI |
|---|---|---|---|---|
| **C0** | 持机静止 | 静置计时 | 静持、极少交互；手颤+姿态 | `QuietHoldClock()`（:738） |
| **C1** | 静态阅读 | 研究协议阅读 | 静态阅读、轻滑 | `ProtocolReader()`（:751） |
| **C2** | 单指滑动信息流 | 研究咨询流 | 单指信息流：滑/停/展开/返回 | `ResearchFeed()`（:767） |
| **C3** | 文本输入 | 段落抄写 | 文本录入；打字节奏+握姿 | `CopyWritingTask()`（:788） |
| **C4** | 多控件操作 | 模拟手机设置 | Tab/按钮/滑块/单选/复选/字段 | `PreferenceControls()`（:807） |
| **C5** | 横屏触控挑战 | 点击蓝色小球 | 横屏目标点击计时 | `BlueBallTapGame()` 全屏30球（:879） |
| **C6** | 视频观看 | 本地视频播放 | 视频：暂停/倍速/拖动/朝向 | `VideoWatchingTask()` 真 `VideoView`（:1027） |
| **C7** | 显式转腕挑战 | 手腕转动 | 标准化手腕平移/旋转/摆动 | `WristGuide()`（:732，`WristGuideAnimation.kt`） |

### 与 8 专家的建议映射（**非出货代码**，启发式只产 C* 字符串；映射取自仓库设计文档）
| 专家 | 最佳 C 源 | 备注 |
|---|---|---|
| **IDLE_HOLDING** | **C0** | 1:1 |
| **STATIC_READING** | **C1** | 1:1 |
| **SCROLL_BROWSE** | **C2** | 1:1 |
| **TYPING** | **C3** | 1:1（C3 亦涉 FORM_FILLING） |
| **FORM_FILLING** | **C4**（偏表单） | C4 歧义，按 `editable>=2`/`form_like_score` 路由 |
| **TAP_NAVIGATION** | **C4**（偏导航）/**C5** | 歧义，按 `clickable_count`/点击节奏 |
| **VIDEO_WATCHING** | **C6** | 1:1 |
| **GAME_OR_TILT** | **C7**（兼 **C5**） | C7 最纯倾斜源 |

**不一致点**：8 任务 ↔ 8 专家但**非双射**；C4 散到 TAP_NAVIGATION+FORM_FILLING；C5 在 TAP_NAVIGATION 与 GAME_OR_TILT 间共享；建议 C4/C5 用**软/双标签**。**生产路由不能用 `task_category`**（第三方批为 null），须由 包名+UI 特征 推断。

---

## 4. 引导任务 / 标注子系统（监督标签来源）
- `BuiltInTasksScreen`（:583）列出全部 `TaskCategory.entries`；"按序完成 8 项"从 C0 起。点任务→`TASK_RUNNER`。C0–C4、C7 在 `canStart` 即自动起 30s 计时；**C5/C6** 等用户点 Start/Play 才起（`waitsForInteraction`）。
- `onStart`→`viewModel.startCollection(task)`→协调器以该 `taskCategory` 产 `BUILTIN_TASK` 批，带全部 `task_*`。30s 倒计时（C5 至 30 球）。每任务约 6 个 5s 批。
- `task_session_id`=每次任务新 UUID（跨锁屏复用）；`task_started_at_wall_millis` 锚定 `task_elapsed_seconds_at_batch_end`，便于服务端按任务时间线分组。
- **服务端强制**使标签可信：`BUILTIN_TASK` 要求 8 个 `task_*` 全非空且自洽，且**每个 `context_feature` 须回显批次 `collection_source`/`task_*`**（`schemas.py:309-362`）；THIRD_PARTY 须 `task_*` 全 null。
- `MazeLogic`/`MazePhysics`：倾斜迷宫，**未接入任何任务**（出货 `TaskContent` 中 C5 是蓝球、C7 是手腕引导），系遗留/备选。
- `WristGuideAnimation`：C7 示教（标准化动作 ±30°平移/±55°旋转/±28°摆动），**不采集不评分**。
- **C5/C6 任务内真值被丢弃**：蓝球位置/命中、视频播放/暂停/拖动/倍速 仅本地算、**不上传**——已具备且隐私安全的标签/行为通道被浪费。

---

## 5. 文本端侧丢弃（drop-all-text）/ 隐私
### 5.1 文本处理总则
当前构建对**所有显示/输入文本一律端侧丢弃**：节点文本、输入框内容、contentDescription、窗口/事件标题一概不采集、不序列化。`text`、`text_redacted`、`content_desc_redacted`（`JsonCodec.kt:179-181`）与事件级 `window_title_redacted`（`JsonCodec.kt:152`）**恒为 `null`**（键保留仅为 schema/存储兼容）。节点改为输出布尔存在标志 `has_text` / `has_content_description`（`JsonCodec.kt:175-176`，源自 `RedactionEngine.kt:57-58` 对原始文本/描述是否非空的判定），仅表达"是否存在"，不含任何内容。

> 旧构建的"保留型脱敏 + 占位符替换"基线正则（email/phone/url/card/id_number/long_number/token → `<EMAIL>`/…，并对非编辑节点**保留普通词**）以及 `<EDITABLE_TEXT_DROPPED>`/`<TEXT_REDACTED>`/`<EMPTY>`/`<LONG_TEXT_DROPPED>` 等占位符**均已移除**——文本通道现一概为空，不存在任何"通过模式后保留明文"的路径。

### 5.2 丢弃规则（替换/保留路径已不存在）
- **密码节点** → 整节点丢弃，`dropped_password_nodes++`，子树跳过，从不序列化。
- **可编辑节点文本** → 丢弃：`text=null`、`text_redacted=null`、`dropped_editable_texts++`。`TYPE_VIEW_TEXT_CHANGED` 事件亦丢（反击键）。
- **非编辑节点文本** → 丢弃：`text=null`、`dropped_text_nodes++`；仅 `has_text` 标志为真。**不再保留任何可见 UI 词**（旧的 `visibleTextForTarget` 普通词保留路径已移除）。
- **contentDescription / 窗口标题** → 丢弃：`content_desc_redacted=null` / `window_title_redacted=null`，`dropped_content_descriptions++` / `dropped_window_titles++`；仅 `has_content_description` 标志反映 desc 是否存在。

### 5.3 `redaction_summary` 计数器（`RedactionEngine.kt:37-41`）
仅五个**丢弃计数**键：`dropped_password_nodes`、`dropped_editable_texts`、`dropped_text_nodes`、`dropped_content_descriptions`、`dropped_window_titles`。仅聚合计数，无内容。（旧的 `redacted_plain_text`、`replaced_{email,phone,url,number,card,id_number,token}`、`dynamic_<ruleName>` 键均已移除。）

### 5.4 云端规则更新机制已移除
- App **不再拉取 `/api/v1/rules`**；`RuleUpdateClient`、`RedactionPolicy`/`RedactionPatternRule`/`RedactionPolicyStore`、`maxTextLength`/`defaultTextAction`、规范化与 canonical 哈希校验、"检查规则" UI 与规则版本/哈希展示**均已删除**。服务端 `/api/v1/rules` 端点仍在（服务端未改）但 App 不消费。
- `rule_version="1"`、`rule_hash`=64 个 0（`ZERO_HASH`）仍随 payload/信封发出，但仅为**未改动服务端 schema 要求的固定基线常量**，不代表任何文本脱敏策略，也不再随云端规则变化。

### 5.5 残留隐私（评估）
- **`viewIdResourceName` 原样输出、不脱敏**（`JsonCodec.kt:158`；测试保留 `com.example.notes:id/account_email`）。这是**编译期开发者资源 ID 语义（非用户数据）**，虽常含语义（"account_email"/"balance"/"phone_input"）并经包前缀暴露应用，但不是用户内容/UGC。MoE 侧可按"默认不用 / 谨慎使用"对待，但不应将其视为文本/UGC 泄露。
- **节点文本 / contentDescription / 标题** → 已全部丢弃，**无任何用户可见文本出域**（drop-all-text 已消除旧构建的 `text` 残留 UGC 风险）。
- `bounds_grid` 虽 24px 粗化但**未归一原始像素**，泄露分辨率与粗布局；配类直方图或可指纹特定应用界面（中低）。
- 应用/活动/组件名完整保留（按设计，路由需包名）——暴露用户访问的 App/界面。
- `device_id` 为 ANDROID_ID 的稳定 HMAC——假名但**跨上传持久可关联**；无原始硬件 ID。
- **无击键、无触摸坐标、无截屏、无密码文本、无任何显示/输入文本**——这些高危通道确实缺失。
- **传输层**：载荷**压缩但不加密**，机密性全靠 TLS。

---

## 6. 服务端（`server/`）
**接收/校验/存储（`main.py /api/v1/ingest` :151-310）**：解析 `Envelope`（`extra="forbid"`，algorithm 钉为 `"LZ4_FRAME+JSON"`，device_id/sha256 正则校验，batch_id 须 UUID）→ base64 解码 → **校验压缩字节 SHA-256**（constant-time）→ **LZ4-frame 解压**（指标 `ingest_decrypt_seconds` 显式 no-op，"本原型不做解密"，确认无解密阶段）→ JSON 解析 → **Pydantic `Batch` 校验**（失败则**隔离**：仅写 `payload_sha256`+排序后顶层键名，非内容，到 `quarantine/`；强制密码节点须丢弃、可编辑文本须丢弃、任务标签契约、特征↔批一致、诊断计数、`started<=ended`）→ 交叉核对 device_id/batch_id → **存储**。

**文件布局（确认"服务端只存储"）**：批 JSON 存 `data_dir/devices/<device_id>/<YYYY-MM-DD>/<batch_id>.json`（pretty，排序键，日期取自 `started_at_wall_millis` UTC）；旁车 `<batch_id>.meta.json`（`request_id`、`ingested_at_wall_millis`、**去 `payload_base64` 的信封**、压缩/解压字节、`"schema_validation_result":"ok"`）；内建任务另建 `by_category/<task_category>/...` 符号链接索引；追加式 `index/{devices,batches,errors}.jsonl`。幂等：同 batch_id 同内容重传 no-op，异内容 409。另有 `GET /api/v1/config`（salt、规则版本、服务器时间、NTP 建议）、`GET /api/v1/rules`（**服务端仍提供，但 App 已不再消费**）、`/health`、`/ready`、`/metrics`。**服务端零路由/MoE/认证/训练。**

**加密现状**：**静态无加密**（明文 JSON 落盘，仅目录权限/磁盘检查；meta 故意省 base64，隔离仅存哈希+键名，但被接受的批文件是完整明文）。**传输仅 TLS**（`INGEST_REQUIRE_AUTH` 显式不支持 → **ingest 端点未鉴权**，知 URL 即可 POST；机密/真实性全靠部署层 HTTPS）。

---

## 7. 传输/信封/规则版本 vs 用户步骤(3)
- `"LZ4_FRAME+JSON"` 字面=序列化→JSON→LZ4 frame→base64，**无加密令牌**；`"encryption":"none"` 在载荷与服务端 schema 双重断言。Android 端强制 HTTPS（`network_security_config cleartextTrafficPermitted=false`），故有线路机密性，但**应用/载荷层"加密+压缩"仅满足一半（压缩有、加密无）**。
- 规则版本：`rule_version`+`rule_hash` 仍同 travel 于批体与信封，但 drop-all-text 后**为固定基线常量**（`"1"` / 64 个 0）——不再代表任何可更新脱敏策略，也不再用于把批次归因到某脱敏规则；保留仅为未改动服务端 schema 的兼容字段。
- **完整满足步骤(3)** 需在 `JsonCodec.buildEnvelopeWithMetrics`（:80-87）插入对 LZ4 字节的对称加密，扩展 `PayloadEnvelope`/服务端 `Envelope` 的 key-id/nonce，并放宽服务端 algorithm `Literal`。当前无任何密钥管理。

---

## 8. 从代码可见的、对研究设想的差距
1. **无触摸坐标/轨迹/压力/尺寸**，仅 `TOUCH_INTERACTION_START/END` **时间戳** → TAP_NAVIGATION 失命中精度/点间位移；SCROLL_BROWSE 失速度/惯性/距离（仅剩滚动事件节奏）。
2. **完全无击键动力学**：`TYPE_VIEW_TEXT_CHANGED` 故意丢、可编辑文本从不输出 → TYPING 无逐键 down/up/dwell/flight；仅 `input_method_visible`+`editable_count` 存活。可用窗口级聚合替代（焦点驻留秒、焦点→焦点 ΔT）。
3. **无高阶运动传感器**（仅 accel/gyro/mag）→ GAME_OR_TILT（C7）与 IDLE_HOLDING 微颤区分须靠原始 IMU；建议补 RotationVector/Gravity/LinearAccel（P0）。
4. **除 IMU+UI 结构外无逐用户生物特征**：无脸/指纹、无步态、无 UsageStats。身份信号全在（传感器动力学 + UI 上下文）；真正认证模型超出当前范围（服务端只存）。
5. `estimated_context_category` 为弱启发式、从不产 8 专家名（第三方仅 C2/C3/C4/UNKNOWN）。**生产路由不能用 `task_category`**（第三方为 null）。**服务端弱标签路由训练管线尚不存在**（TODO），内建标签只覆盖 8 个实验任务。
6. **同一包跨多上下文**（微信=静持/滑动/打字/视频/点击）→ 仅 `app_package_name` 不足；须 包名+UI 特征 联合，而当前判别性特征偏薄。
7. **C5/C6 任务内真值被丢弃**（蓝球命中、视频播控）——已具备、隐私安全的标签/行为通道被浪费。
8. `node_id` 不稳定（`depth_hashCode`）→ 无法跨快照追踪节点，任何逐元素时序特征不可行。
9. **内建数据集不均衡**且 2026-06-11 修复前数据为传感器-only；训 UI 上下文路由需新的、均衡的、修复后重采。

**净结论**：App 干净交付 *步骤(1)* UI 上下文捕获（修复后）、*步骤(2)* 端侧文本丢弃（drop-all-text，零文本、零云端规则）、*部分步骤(3)*（压缩有/加密无）、*步骤(4)输入*（包名 + 零文本 UI 结构特征 + 传感器）。完整 MoE 路由+8 专家+持续认证 还缺：加密、更丰富传感器、任何触摸/击键微动力学、服务端弱标签路由、均衡的修复后数据集。
