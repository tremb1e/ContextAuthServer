# 当前 App 采集数据全景分析

> 本文档对 ContextAuthLab Android 端「事件 → 特征 → 批次 payload」全链路实际采集的数据进行系统化盘点，按字段维度列出来源、频率、文本处理后形态、典型取值与代码位置。所有结论基于 master 分支代码与服务端样本 `data/testdata/51dfc982-c257-402c-a63b-d3de80c141b2.json` 的实证比对。
>
> **重要前提（drop-all-text）**：当前构建对**所有显示/输入文本一律端侧丢弃**——App 不再采集/上传任何节点文本、输入框内容、content-description、窗口/事件标题。`text`、`text_redacted`、`content_desc_redacted`、`window_title_redacted` 现**恒为 `null`**（键保留仅为 schema/存储兼容），节点改为输出布尔存在标志 `has_text`、`has_content_description`（仅"是否存在非空文本/描述"，不含任何内容）。旧的"保留型脱敏 + 占位符替换"（正则把 email/phone/url/card/id/token 替换为 `<EMAIL>`/… 并保留非编辑可见组件文本）以及云端"脱敏规则"/规则更新机制（`/api/v1/rules`、`RuleUpdateClient`、`RedactionPolicy` 等）**均已移除**。本文中凡引用 `data/testdata/2026-06-11` 的实测文本数字（如 10,651 节点保留原始 `text`、594 等）皆为**旧的保留文本构建**所采，已显式标注，不代表当前行为。`rule_version="1"`、`rule_hash`=64 个 0 仍随 payload/信封发出，但仅为未改动服务端 schema 要求的固定基线常量，不代表任何文本脱敏策略。

## 目录

1. 总览：采集源、批次结构与上行链路
2. 总览表格（一眼看全）
3. 批次顶层元数据字段（Batch Envelope）
4. 传感器采样（`sensor_samples`）
5. 触控事件（`touch_events`）
6. 无障碍上下文事件（`context_events`）
7. 节点快照（`root_nodes` 内的 `NodeSnapshot`）
8. 上下文特征（`context_features`）
9. 脱敏摘要与诊断（`redaction_summary`、`diagnostics`）
10. 设备 / 网络 / 时间同步元信息
11. 不采集 / 已显式排除的字段
12. 实际样本字段量化（按 WeChat 样本测算）

---

## 1. 总览：采集源、批次结构与上行链路

数据沿三条平行通道汇入 `CollectionCoordinator`，按固定 5 s 节奏（`SettingsStore.FIXED_BATCH_SECONDS=5`）合批为一个 `Batch`，经 `JsonCodec` 序列化 → LZ4 frame 压缩 → SHA-256 校验 → Base64 包入 `PayloadEnvelope` → HTTPS POST `/api/v1/ingest`。

```
┌──────────────────────────┐        ┌──────────────────────┐
│ AccessibilityService     │ ─ UI ─►│ AccessibilityEventBus│──► contextEvents（事件触发，可能为空）
│  (ResearchAccessibility) │        └──────────────────────┘
│                          │
│  每 5 s flush 主动拉取：  │ ─ 前台快照 ─►  FOREGROUND_SNAPSHOT（已合入 contextEvents，服务连接时恒有一条）
│  captureForegroundSnapshot() │
└──────────────────────────┘
┌──────────────────────────┐        ┌──────────────────────┐
│ AccessibilityService     │ ─ T ─► │ TouchEventBus        │──► touchEvents
│  (全局触控交互 start/end) │        └──────────────────────┘
└──────────────────────────┘                                       │
┌──────────────────────────┐                                       ▼
│ SensorEventListener      │ ─ S ─►  buffer (本地 ArrayList)──► sensorSamples
│  (SensorCollector @100Hz)│                                       │
└──────────────────────────┘                                       ▼
        5 s flush ──► buildBatch（events + 前台快照）──► context_features（每条事件 1:1）
                    ──► Batch ──► LZ4+SHA256+B64 ──► POST /ingest
```

**关键事实（主动前台快照 / 文本端侧丢弃 / "加密"边界）：**

- **主动前台快照（本次修复核心）**：每 5 s flush 时 `CollectionCoordinator.buildBatch` 调用 `ResearchAccessibilityService.captureForegroundSnapshot()` 主动拉取**当前前台窗口**，解析前台包名 + Activity/Component 并遍历脱敏后的 UI 节点树，生成一条 `eventType="FOREGROUND_SNAPSHOT"` 的 context event 并以 `allEvents = events + snapshot` 合入**每一批**。因此只要无障碍服务处于连接状态，`app_package_name`、`context_events`、`context_features` 在批内**恒为非空**（即便本窗口期内没有任何无障碍事件触发）；不再像旧实现那样「仅被动响应事件」、静态前台界面导致整批 `context_events=0 / app_package_name="unknown"`。代码位置：`CollectionCoordinator.kt:568-622`（`buildBatch`、`allEvents`）、`ResearchAccessibilityService.kt:282-347`（`buildForegroundSnapshotInternal`、`captureForegroundSnapshot`）。
- **诚实边界**：快照由静态字段 `instance` 提供（`onServiceConnected` 置位、`onUnbind`/`onDestroy` 清空）。当无障碍服务**未连接/被解绑**时 `captureForegroundSnapshot()` 返回 `null`，此时该批**没有快照**，可能退化为「仅传感器/触控」；若该批连传感器、触控、上下文都为空，`uploadBatch` 的「无内容不发送」门控会跳过它（`CollectionCoordinator.kt:371-373`）。另外即便服务已连接，若从未观测到任何前台应用窗口、`appPackageName` 解析为空，`JsonCodec` 仍会在顶层 `app_package_name` 回落为 `"unknown"`（`JsonCodec.kt:21-23`）——但正常使用中前台窗口总能解析到包名。
- **「无内容不发送」门控位置**：该门控已从 build 之前移到 `uploadBatch` 内、`buildBatch` 之后（`CollectionCoordinator.kt:363-373`），因此「仅快照」的批次（`context_events` 因快照而非空）也会照常上传，不会因缺包名/缺 UI 被丢弃。
- 服务端样本中 `diagnostics.encryption = "none"`，本阶段无应用层 AES。机密性依赖 HTTPS/TLS。代码位置：`android-app/src/main/java/com/contextauth/core/JsonCodec.kt:65`。
- **文本一律端侧丢弃（drop-all-text）**：节点遍历不再输出任何文本——`text`、`text_redacted`、`content_desc_redacted` 在 `JsonCodec` 中**恒写 `null`**（`JsonCodec.kt:179-181`），事件级 `window_title_redacted` 亦恒 `null`（`JsonCodec.kt:152`）；改为输出布尔 `has_text` / `has_content_description`（`JsonCodec.kt:175-176`，源自 `RedactionEngine` 对原始文本/描述是否非空的判定 `RedactionEngine.kt:57-58`）。密码节点整节点丢弃且子树跳过，从不序列化。旧的"保留型脱敏 + 占位符替换"与云端可更新脱敏规则（`/api/v1/rules`、`RuleUpdateClient`、`RedactionPolicy`/`RedactionPatternRule`/`RedactionPolicyStore`、`max_text_length`/`default_text_action`、"检查规则"UI 与规则版本/哈希展示）**已全部移除**；服务端 `/api/v1/rules` 端点仍在但 App 不再消费。`rule_version="1"`、`rule_hash`=64 个 0 仍在 payload/信封发出，但仅为未改动服务端 schema 要求的固定基线常量。
- 采集开始的硬门控：同意 + 无障碍 + 电池白名单 + 通知权限 + 屏幕亮且未锁 + 64 位 hex 的 `device_id`，参见 `CollectionCoordinator.canStart`，`android-app/src/main/java/com/contextauth/core/CollectionCoordinator.kt:319-325`。
- 锁屏 / 息屏立即停采，重启上锁前任务复用同一 `task_session_id`，参见 `CollectionCoordinator.kt:497-520` 与 `gatedResume` 字段。
- **进程级采集生命周期（本次修复核心之二）**：`CollectionCoordinator` 现为 `ContextAuthLabApplication` 持有的单例，运行在进程级 `CoroutineScope(SupervisorJob() + Dispatchers.Default)` 上，于 `Application.onCreate` 通过幂等的 `startRuntime` 启动一次；`MainViewModel` 复用该单例与应用 scope 而非 `viewModelScope`。前台服务 `DataCollectionService` 在 `onStartCommand` 时，只在当前**没有 RUNNING** 的采集时才（重新）启动第三方采集（避免覆盖进行中的内置任务 C0–C7）。净效果：采集在 Activity 退后台/销毁后仍存活，从而可靠捕获**其他第三方应用**在正常使用中的前台包名与 UI。代码位置：`ContextAuthLabApplication.kt:10-23`、`DataCollectionService.kt:29-45`、`MainViewModel.kt:11-13`。

---

## 2. 总览表格（一眼看全）

下表列出当前实际上传的全部字段类别。每一类后文有独立小节细化字段定义、来源代码与样本片段。

| 类别 | 主要字段 | 采集源 | 采集频率 / 触发 | 文本/隐私处理 | 代码位置 |
|---|---|---|---|---|---|
| Envelope（包络层） | `algorithm`、`payload_base64`、`payload_sha256_hex`、`device_id`、`batch_id`、`rule_version`、`rule_hash`、`created_at_wall_millis` | `JsonCodec.buildEnvelopeWithMetrics` | 每批 5 s 一次 | `device_id` 是 HMAC-SHA256；payload 本身仅压缩，未加密；`rule_version`/`rule_hash` 为固定基线常量 | `JsonCodec.kt:74-100`；`Models.kt:213-222` |
| Batch 元数据 | `batch_id`、`device_id`、`session_id`、`record_type`、`collection_source`、`app_package_name`、`foreground_activity_class_name`、`foreground_component_name`、`sampling_rate_hz`、`batch_duration_seconds`、`app_version`、`rule_version`、`rule_hash`、`consent_version`、`started_at_wall_millis`、`ended_at_wall_millis`、`base_elapsed_nanos` | `Batch` 构造 + `CollectionCoordinator.buildBatch` | 每批一份 | 包名 / Activity / Component **明文** | `Models.kt:190-211`；`CollectionCoordinator.kt:550-602`；`JsonCodec.kt:18-69` |
| 任务标签 | `task_sequence`、`task_id`、`task_name`、`task_intuitive_description`、`task_category`、`task_session_id`、`task_started_at_wall_millis`、`task_elapsed_seconds_at_batch_end` | `TaskCategory`（C0-C7）枚举 | 仅 `BUILTIN_TASK` 模式有；`THIRD_PARTY_APP` 全部为 `null` | 否（科研标签） | `Models.kt:21-112`；`JsonCodec.kt:39-46` |
| 传感器（IMU） | `sensor_type`、`timestamp_elapsed_nanos`、`wall_time_estimated_millis`、`x`、`y`、`z`、`accuracy` | `SensorCollector` 注册三种 `Sensor.TYPE_*` | 100 Hz 名义、~95 Hz 实测 | 否 | `SensorCollector.kt:86-106`；`SamplingConfig.kt:1-7`；`JsonCodec.kt:122-130` |
| 触控时序 | `event_id`、`event_type`、`event_time_uptime_millis`、`event_time_wall_millis`、`collected_at_wall_millis` | `ResearchAccessibilityService` 的全局触控交互事件 | 全局屏幕触控开始/结束；仅记录时间 | 强脱敏：**无 x/y、无 pressure、无 size、无轨迹** | `ResearchAccessibilityService.kt`；`Models.kt:161-167`；`JsonCodec.kt:132-139` |
| Context Event | `event_id`、`event_type`、`event_time_wall_millis`、`app_package_name`、`foreground_activity_class_name`、`foreground_component_name`、`input_method_visible`、`coarse_orientation`、`window_title_redacted`（恒 `null`）、`root_nodes`、`redaction_summary` | ① `ResearchAccessibilityService.onAccessibilityEvent`（被动事件）；② `captureForegroundSnapshot()`（每 5 s flush 主动前台快照，`event_type="FOREGROUND_SNAPSHOT"`） | 被动：受订阅事件 + 节流（滚动 120 ms / 内容 180 ms / 窗口 250 ms）；主动：每批 1 条（服务连接时） | 节点零文本（drop-all-text，两路复用同一文本丢弃引擎）；`window_title_redacted` 恒 `null` | `ResearchAccessibilityService.kt:34-70`（被动）、`282-347`（快照）；`Models.kt:148-159`；`JsonCodec.kt:141-153` |
| Node Snapshot | `node_id`、`class_name`、`viewIdResourceName`、`bounds_grid`、`clickable`、`editable`、`scrollable`、`checkable`、`checked`、`enabled`、`focused`、`selected`、`visible_to_user`、`long_clickable`、`password`、`input_type_category`、`child_count`、`has_text`、`has_content_description`、`text`（恒 `null`）、`text_redacted`（恒 `null`）、`content_desc_redacted`（恒 `null`）、`actions_summary`、`depth` | `ResearchAccessibilityService.traverse` + `RedactionEngine.sanitizeNode` | 每个 context event 最多 320 节点、深度 ≤ 14 | 文本全丢弃：text/contentDescription 仅留布尔存在标志 `has_text`/`has_content_description`；password 整棵丢弃 | `ResearchAccessibilityService.kt:47-100`；`Models.kt:124-146`；`RedactionEngine.kt:50-78`；`JsonCodec.kt:153-181` |
| Context Feature | `feature_id`、`event_id`、`computed_at_wall_millis`、`collection_source`、`task_*`、`input_method_visible`、`keyboard_visible_estimated`、`editable_count`、`scrollable_count`、`clickable_count`、`password_node_seen`、`media_like_score`、`list_like_score`、`form_like_score`、`game_like_score`、`node_class_histogram`、`event_type`、`coarse_orientation`、`estimated_context_category` | `ContextFeatureExtractor.extract` 对每条 context event 派生 | 1:1 对应 context event | 不含原始文本 | `ContextFeatureExtractor.kt:1-55`；`Models.kt:169-188`；`JsonCodec.kt:178-203` |
| 文本丢弃摘要 | `dropped_password_nodes`、`dropped_editable_texts`、`dropped_text_nodes`、`dropped_content_descriptions`、`dropped_window_titles` | `RedactionSummary` | 每 context event 累计 | 仅丢弃计数，无内容 | `RedactionEngine.kt:29-42` |
| 批次诊断 | `sensor_sample_count`、`context_event_count`、`touch_event_count`、`sampling_rate_hz`、`redaction_applied`、`compression`、`encryption`、`gated_resume` | `JsonCodec.batchToJson` 固定写入 | 每批一份 | / | `JsonCodec.kt:59-67` |
| `skip_events` | 占位空数组 | 当前总为空 | 已废弃 package_blocklist | / | `CollectionCoordinator.kt:599`；`schemas.py:277` |

---

## 3. 批次顶层元数据字段（Batch Envelope）

### 3.1 PayloadEnvelope（外层）

| 字段（中英） | 数据结构 | 来源 / API | 频率 | 脱敏后形态 | 典型取值 | 代码位置 |
|---|---|---|---|---|---|---|
| `algorithm` | `PayloadEnvelope` | `JsonCodec.ALGORITHM` 常量 | 每批 | 明文 | `"LZ4_FRAME+JSON"` | `JsonCodec.kt:10` |
| `payload_base64` | 同上 | Base64(LZ4Frame(JSON)) | 每批 | LZ4 压缩 + Base64，但**未加密** | ~kB 级二进制 | `JsonCodec.kt:79-87` |
| `payload_sha256_hex` | 同上 | `MessageDigest("SHA-256")` 作用于压缩字节 | 每批 | hex(64) | `"<sha256>"` | `JsonCodec.kt:81-83、119-120` |
| `device_id` 设备研究 ID | 同上 | `HMAC-SHA256(serverStudySalt, ANDROID_ID)` | 每批 | 64 位 hex，**非可逆**但可关联同一设备 | `36905bde...8f573638`（样本） | `DeviceIdProvider.kt:30-52` |
| `batch_id` 批次 UUID | 同上 | `UUID.randomUUID()` | 每批 | UUID v4 | `51dfc982-c257-402c-a63b-d3de80c141b2`（样本即文件名） | `CollectionCoordinator.kt:581` |
| `rule_version` 规则版本 | 同上 | `SettingsStore` 持久化的**固定基线常量**（drop-all-text 后不再代表任何脱敏策略） | 每批 | 明文 | `"1"` | `Models.kt:218`；`SettingsStore.kt` |
| `rule_hash` 规则哈希 | 同上 | **固定基线常量**（64 个 0 的 `ZERO_HASH` 基线；规则更新机制已移除） | 每批 | hex(64) | `0000…0000`（64 个 0） | `Models.kt`（`ZERO_HASH`） |
| `created_at_wall_millis` 创建时刻 | 同上 | 来源于批次起始时刻 | 每批 | 整型毫秒 | `1779886044121`（样本） | `JsonCodec.kt:92` |

### 3.2 Batch 顶层

| 字段 | 来源 | 频率 | 脱敏后形态 | 样本值 | 代码位置 |
|---|---|---|---|---|---|
| `record_type` | 常量 | 固定 | `"collection"` | `"collection"` | `JsonCodec.kt:32` |
| `collection_source` | `BUILTIN_TASK` / `THIRD_PARTY_APP` | 每批 | 枚举 | `"BUILTIN_TASK"`（样本） | `Models.kt:16-19`；`CollectionCoordinator.kt:568` |
| `session_id` | 内置任务时 = `task_session_id`；前台采集时 = collection session UUID | 每批 | UUID | `3a6dcfec-fe08-4813-b1af-885a956b6b2b`（样本） | `CollectionCoordinator.kt:583` |
| `app_package_name` 前台包名 | 来源于 `allEvents`（被动事件 + 主动前台快照）中最近的非空 `appPackageName`，底层取自 `Active application window` 包；**服务连接时主动前台快照保证每批至少有一条带包名的事件**，故不再退化为 `"unknown"`（仅当从未解析到任何前台窗口时才回落 `"unknown"`） | 每批 | **明文** | `"com.tencent.mm"`（样本） | `CollectionCoordinator.kt:576-607`；`ResearchAccessibilityService.kt:305-320`；`JsonCodec.kt:21-23` |
| `foreground_activity_class_name` | 来源于 `TYPE_WINDOW_STATE_CHANGED` 的 `event.className`，必须与前台包名匹配 | 每批 | **明文** | `"com.tencent.mm.ui.LauncherUI"`（样本） | `ResearchAccessibilityService.kt:193-199` |
| `foreground_component_name` | `ComponentName(packageName, activity).flattenToShortString()` | 每批 | **明文** | `"com.tencent.mm/.ui.LauncherUI"`（样本） | `ResearchAccessibilityService.kt:223-226` |
| `sampling_rate_hz` | 常量 100 | 每批 | int | `100` | `SamplingConfig.kt:5` |
| `batch_duration_seconds` | `(ended-started)/1000` | 每批 | int | `5`（样本，实际 5.94 s） | `JsonCodec.kt:38` |
| `app_version` | `BuildConfig.VERSION_NAME` | 每批 | str | `"1.0.0"`（样本） | `JsonCodec.kt:47` |
| `consent_version` | 常量 `"1"` | 每批 | str | `"1"` | `JsonCodec.kt:50` |
| `started_at_wall_millis` / `ended_at_wall_millis` | 由批内传感器 / 触控 / 上下文事件的 min/max 时间合并 | 每批 | 毫秒 | `1779886044121` / `1779886050059` | `CollectionCoordinator.kt:558-567` |
| `base_elapsed_nanos` | `SystemClock.elapsedRealtimeNanos()` 的批次基准 | 每批 | 纳秒 | `681947723611347`（样本） | `SensorCollector.kt:53、61`；`CollectionCoordinator.kt:594` |

### 3.3 任务标签字段（仅 BUILTIN_TASK 非空）

`TaskCategory` 在 `Models.kt:21-112` 定义了 8 个固定枚举 C0…C7：

| Task | `task_intuitive_description` (英) | `task_name` (英) | 中文 |
|---|---|---|---|
| C0 | Quiet hold | Still timer | 持机静止 / 静置计时 |
| C1 | Static reading | Research protocol reading | 静态阅读 |
| C2 | Single-finger feed | Research information feed | 信息流滚动 |
| C3 | Text entry | Paragraph copy | 文本输入 / 段落抄写 |
| C4 | Multi-control operation | Simulated phone settings | 多控件操作 |
| C5 | Landscape touch challenge | Blue ball tapping | 横屏触控挑战 |
| C6 | Video watching | Local video playback | 视频观看 |
| C7 | Explicit wrist rotation | Wrist rotation | 显式转腕 |

样本批次为 `task_id="C5"`，`task_sequence=5`，`task_session_id` 与 `session_id` 同值，`task_elapsed_seconds_at_batch_end=44`。

---

## 4. 传感器采样（`sensor_samples`）

### 4.1 字段

定义于 `Models.kt:114-122`、序列化于 `JsonCodec.kt:122-130`：

| 字段 | 类型 | 含义 | 样本片段 |
|---|---|---|---|
| `sensor_type` | enum string | `ACCELEROMETER` / `GYROSCOPE` / `MAGNETIC_FIELD` | 三类各占约三分之一 |
| `timestamp_elapsed_nanos` | long | `SensorEvent.timestamp`，基于 `SystemClock.elapsedRealtimeNanos` | `681985360189933` |
| `wall_time_estimated_millis` | long | `baseWallMillis + (event.timestamp - baseElapsedNanos)/1e6`；`baseWallMillis = System.currentTimeMillis() + serverOffsetMillis` | `1779886044610` |
| `x` / `y` / `z` | float→double | 三轴值，单位为 m/s² (Accel)、rad/s (Gyro)、μT (Mag) | Accel: `(0.039, 8.98, 4.23)` |
| `accuracy` | int | `SensorEvent.accuracy`，0-3 | 通常为 `3` |

### 4.2 采集策略

- 100 Hz 名义采样：`SamplingConfig.SAMPLING_RATE_HZ = 100`，`SAMPLING_PERIOD_US = 10000`，`MAX_REPORT_LATENCY_US = 200000`（200 ms 上报延迟）。
- 三种传感器一并注册：`Sensor.TYPE_ACCELEROMETER`、`Sensor.TYPE_GYROSCOPE`、`Sensor.TYPE_MAGNETIC_FIELD`。`AndroidManifest.xml:13` 申请 `HIGH_SAMPLING_RATE_SENSORS`。
- `SensorCollector.collectionHz` 计算 `min(100, 1e6/sensor.minDelay)`，对低规格设备会自动降级。
- **样本实证**：5.94 s 内 ACCELEROMETER 563 条 + GYROSCOPE 563 条 + MAGNETIC_FIELD 545 条 ≈ 94-95 Hz 实测，比名义 100 Hz 略低。

代码位置：`SensorCollector.kt:30-130`。

### 4.3 样本片段

```json
{
  "accuracy": 3,
  "sensor_type": "ACCELEROMETER",
  "timestamp_elapsed_nanos": 681985360189933,
  "wall_time_estimated_millis": 1779886044610,
  "x": 0.039481572806835175,
  "y": 8.981459617614746,
  "z": 4.227519512176514
}
```

### 4.4 注意

- 没有 **GAME_ROTATION_VECTOR / GRAVITY / LINEAR_ACCELERATION / ROTATION_VECTOR / STEP / PROXIMITY / LIGHT**：未被任何代码注册。
- 没有 **气压计、心率、光感、近距感**。
- 时间戳源是 `SystemClock.elapsedRealtimeNanos`（单调），墙钟时间为估算。`ClockSyncService` 用 HTTP midpoint + NTP fallback 维护服务器偏移，写入 `baseWallMillis` 用作墙钟基线（`SensorCollector.kt:62`）。

---

## 5. 触控事件（`touch_events`）

### 5.1 字段（极简）

定义于 `Models.kt:161-167`、序列化于 `JsonCodec.kt:132-138`：

| 字段 | 类型 | 含义 |
|---|---|---|
| `event_id` | UUID | 单次触控事件 ID |
| `event_type` | enum string | 当前全局触控为 `TOUCH_INTERACTION_START` / `TOUCH_INTERACTION_END`；服务端兼容旧的 `TOUCH_DOWN` / `TOUCH_UP` / `TOUCH_POINTER_*` / `TOUCH_CANCEL` |
| `event_time_uptime_millis` | long | `AccessibilityEvent.eventTime`（uptime） |
| `event_time_wall_millis` | long | 由 `System.currentTimeMillis()` 减 `(uptime-eventTime)` 推算 |
| `collected_at_wall_millis` | long | 无障碍服务收到触控交互事件时的墙钟 |

### 5.2 采集策略

触控时间由无障碍服务订阅 `TYPE_TOUCH_INTERACTION_START/END` 获得，作用范围是全局屏幕触控交互；代码只写入事件类型与时间戳，不读取或序列化坐标、轨迹、压力、面积，也不执行 `performAction`、手势或文本输入。`TYPE_VIEW_TEXT_CHANGED` 仍不处理，因此不会形成逐字输入或按键时序。

### 5.3 缺失字段（这是与认证需求最相关的差距来源）

`BatchSerializationTest.batchJsonContainsTouchTimingWithoutPositionFields`（`BatchSerializationTest.kt:189-219`）断言并 `assertFalse(touch.has("x"|"y"|"pressure"|"size"))`。`schemas.py:174-181` 服务端用 `extra="forbid"` 强禁这些字段。

| 不采集 | 后果 |
|---|---|
| `x`, `y` | 无法构造触摸轨迹、热区分布 |
| `pressure`, `size` | 无法刻画按压力度与指腹尺寸 |
| 多指相对距离 | 无法判别捏合 / 拖拽 |
| `historical*` 子事件 | 无法做精细 ΔT 内插 |
| 触控速度、加速度 | 无法计算滑动速度 |
| `velocity` (`VelocityTracker`) | 系统级 fling 速度未采 |

### 5.4 样本片段

```json
{
  "event_id": "3d9ac0e5-f6ce-4e02-b61d-ee725fceec0d",
  "event_time_uptime_millis": 300878721,
  "event_time_wall_millis": 1779886046939,
  "collected_at_wall_millis": 1779886046941,
  "event_type": "TOUCH_INTERACTION_START"
}
```

样本中的触控事件只表达全局触控交互开始/结束时间，可用于计算触控频率和间隔；由于不采集坐标/轨迹，不能重建命中位置或滑动路径。

---

## 6. 无障碍上下文事件（`context_events`）

### 6.1 订阅哪些 AccessibilityEvent

`research_accessibility_service.xml` 配置如下：

```xml
android:accessibilityEventTypes="typeWindowStateChanged|typeWindowsChanged|typeWindowContentChanged|typeViewScrolled|typeViewClicked|typeViewLongClicked|typeViewFocused|typeViewSelected"
android:accessibilityFlags="flagRetrieveInteractiveWindows|flagReportViewIds|flagIncludeNotImportantViews"
android:canRetrieveWindowContent="true"
android:notificationTimeout="250"
```

代码进一步显式排除 `TYPE_VIEW_TEXT_CHANGED`（`ResearchAccessibilityService.kt:200`），并对高频事件做节流（`shouldProcess`）：
- 滚动 `TYPE_VIEW_SCROLLED` ≥ 120 ms 一次
- 内容变化 `TYPE_WINDOW_CONTENT_CHANGED` ≥ 180 ms 一次
- 窗口变化 `TYPE_WINDOWS_CHANGED` ≥ 250 ms 一次

### 6.1b 主动前台快照（`FOREGROUND_SNAPSHOT`）

除上述**被动**事件外，自本次修复起 context_events 还包含一类**主动**事件。`CollectionCoordinator.buildBatch` 在每 5 s flush 时调用 `ResearchAccessibilityService.captureForegroundSnapshot()`（`ResearchAccessibilityService.kt:282-347`、`CollectionCoordinator.kt:568-577`）：

- 通过静态 `@Volatile instance`（`onServiceConnected` 置位、`onUnbind`/`onDestroy` 清空）拉取**当前前台窗口**；`activeApplicationWindowPackage()` 解析前台包名（必要时回落 `lastForegroundTarget`），`resolveActivityComponent` 解析 Activity/Component，`collectApplicationRoots` + `traverse` 走与被动路径**完全相同**的脱敏节点遍历（`MAX_DEPTH=14`、`MAX_NODES_PER_EVENT=320`）。
- 生成的 `ContextEventSnapshot` 字段特征：`event_type="FOREGROUND_SNAPSHOT"`、`event_time_wall_millis=System.currentTimeMillis()`、`window_title_redacted=null`（该路径不携带窗口标题文本）、`coarse_orientation` 为采样时刻动态值、`redaction_summary` 正常累计；`app_package_name` / `foreground_activity_class_name` / `foreground_component_name` / `root_nodes` 来自当前前台窗口。
- 该快照以 `allEvents = events + snapshot` 合入批次，并与被动事件一样**派生一条 `context_feature`**（1:1）。因此服务连接时每批都至少有一条带前台包名 + UI 的 context event。
- **诚实边界**：服务未连接/被解绑时 `captureForegroundSnapshot()` 返回 `null`，该批无快照；若整批传感器/触控/上下文皆空，`uploadBatch` 会跳过该批。

### 6.2 Context Event 字段

| 字段 | 来源 | 含义 |
|---|---|---|
| `event_id` | `UUID.randomUUID()` | |
| `event_type` | **被动**：`AccessibilityEvent.eventType` → `typeName()` 字符串；**主动**：固定 `"FOREGROUND_SNAPSHOT"` | `TYPE_*` 字符串或 `FOREGROUND_SNAPSHOT` |
| `event_time_wall_millis` | `System.currentTimeMillis()`（快照路径为采样时刻） | |
| `app_package_name` | 前台应用包名（plaintext） | |
| `foreground_activity_class_name` | 被动：来自 `WINDOW_STATE_CHANGED` 的 `event.className`；快照：来自 `resolveActivityComponent` | |
| `foreground_component_name` | `flattenToShortString()` | |
| `input_method_visible` | `windows?.any { type == TYPE_INPUT_METHOD }` | |
| `window_title_redacted` | **恒为 `null`**（drop-all-text：窗口/事件标题文本一律丢弃，被动与快照两路皆不携带标题） | `JsonCodec.kt:152` |
| `root_nodes` | 应用窗口 ↔ active 窗口的根节点 DFS 收集 | 详见第 7 节 |
| `redaction_summary` | `RedactionSummary.asMap()` | 详见第 9 节 |

> 注：下表的样本分布来自旧实现采集的样本 `51dfc982-...json`，其中没有 `FOREGROUND_SNAPSHOT`。修复后每批会额外多出 1 条 `FOREGROUND_SNAPSHOT`（服务连接时），其对应的 `context_feature` 也会出现在 `context_features` 中。

样本中 40 条 context events 的分布：

```
TYPE_WINDOW_CONTENT_CHANGED: 17
TYPE_WINDOWS_CHANGED:         7
TYPE_WINDOW_STATE_CHANGED:    6
TYPE_VIEW_SCROLLED:           5
TYPE_VIEW_SELECTED:           2
TYPE_VIEW_CLICKED:            2
TYPE_VIEW_FOCUSED:            1
```

### 6.3 节点采集策略

`ResearchAccessibilityService.collectApplicationRoots` 优先遍历所有 `TYPE_APPLICATION` 窗口；若都不属于前台包，再退到 `rootInActiveWindow`。`traverse` 递归并执行：
- `MAX_DEPTH = 14`，`MAX_NODES_PER_EVENT = 320`。
- `password=true` 节点整子树丢弃。
- 所有节点的 `text` / `contentDescription` **一律丢弃**（drop-all-text），仅保留布尔存在标志 `has_text` / `has_content_description`；不区分 editable/非 editable——文本通道一概为空。

---

## 7. 节点快照（`NodeSnapshot`）

### 7.1 字段清单（23 个）

定义于 `Models.kt:124-146`、序列化于 `JsonCodec.kt:153-181`：

| 字段 | 含义 | 文本/隐私处理 / 注意 |
|---|---|---|
| `node_id` | `"${depth}_${node.hashCode()}"` | 同一节点跨事件不稳定 |
| `class_name` | `node.className` 字符串（如 `android.widget.Button`） | 明文（控件类名，非用户数据） |
| `viewIdResourceName` | 资源 ID，如 `com.tencent.mm:id/jxs` | **明文：编译期开发者资源 ID 语义（非用户数据）** |
| `bounds_grid` | `{left,top,right,bottom}`，原始像素 `÷ 24` 离散化 | 弱坐标信息 |
| `clickable` / `editable` / `scrollable` / `checkable` / `checked` / `enabled` / `focused` / `selected` / `long_clickable` / `visible_to_user` | 节点状态布尔 | 明文 |
| `password` | 报文里固定写 `false`（password 节点已整棵剔除） | `JsonCodec.kt:178` |
| `input_type_category` | 报文写 `"text"`（editable）或 `null` | `JsonCodec.kt:173`——丢弃了 `InputType` 的细分（NUMBER/EMAIL/PHONE/PASSWORD/DATETIME 等） |
| `child_count` | `node.childCount` | |
| `has_text` | 布尔：该节点是否存在非空 `node.text`（**仅存在标志，无内容**） | `JsonCodec.kt:175`；源自 `RedactionEngine.kt:57` |
| `has_content_description` | 布尔：该节点是否存在非空 `contentDescription`（**仅存在标志，无内容**） | `JsonCodec.kt:176`；源自 `RedactionEngine.kt:58` |
| `text` | **恒为 `null`**（drop-all-text：所有节点文本一律丢弃） | `JsonCodec.kt:179`——键保留仅为 schema 兼容 |
| `text_redacted` | **恒为 `null`**（不再有 `<EDITABLE_TEXT_DROPPED>` 占位） | `JsonCodec.kt:180` |
| `content_desc_redacted` | **恒为 `null`**（contentDescription 一律丢弃，仅留 `has_content_description`） | `JsonCodec.kt:181` |
| `actions_summary` | `["CLICK","LONG_CLICK","SCROLL","CHECK","EDIT"]` 子集 | |
| `depth` | 当前递归深度（根为 0） | |

### 7.2 文本端侧丢弃（drop-all-text）

`RedactionEngine.sanitizeNode`（`RedactionEngine.kt:50-78`）对每个节点：

1. **文本一律不输出**：无论 editable 与否，`text` / `text_redacted` / `content_desc_redacted` 在序列化阶段一律写 `null`（`JsonCodec.kt:179-181`）。不再有"保留型脱敏 + 占位符替换"——旧实现里 `visibleTextForTarget` 对非编辑节点保留普通词、`redactTextForTarget` 对 content-desc/标题折叠为 `<TEXT_REDACTED>` 的**不对称设计已整体移除**，文本通道现一概为空。
2. **仅保留存在标志**：引擎判断原始 `node.text`/`contentDescription` 是否非空，分别写入布尔 `has_text` / `has_content_description`（`RedactionEngine.kt:57-58`），供下游做"是否有文本/描述"的弱特征，但不携带任何内容。
3. **密码节点**整节点丢弃且子树跳过，从不序列化。

> 历史对照（旧构建实测）：`data/testdata/2026-06-11` 由**旧的保留文本构建**采集，彼时样本中约 594 个节点保留了原始 `text`（如 WeChat 的 `'浮窗'`、按钮标签等），content-desc 则被折叠为 `<TEXT_REDACTED>`。**当前构建下此类文本残留已不存在**——`text` 恒 `null`，仅有 `has_text`/`has_content_description` 存在标志。

### 7.3 样本片段

```json
{
  "actions_summary": ["CLICK"],
  "bounds_grid": {"bottom": 13, "left": 0, "right": 0, "top": 7},
  "checkable": false,
  "checked": false,
  "child_count": 0,
  "class_name": "android.widget.ImageView",
  "clickable": true,
  "content_desc_redacted": null,
  "depth": 11,
  "editable": false,
  "enabled": true,
  "focused": false,
  "has_content_description": true,
  "has_text": false,
  "input_type_category": null,
  "long_clickable": false,
  "node_id": "11_1404765",
  "password": false,
  "scrollable": false,
  "selected": false,
  "text": null,
  "text_redacted": null,
  "viewIdResourceName": "com.tencent.mm:id/jxs",
  "visible_to_user": false
}
```

> 注：`text` / `text_redacted` / `content_desc_redacted` 在当前 drop-all-text 构建下恒为 `null`；该节点是否原本带 contentDescription 由 `has_content_description=true` 表达（但不含内容）。

### 7.4 样本量化（按本批 40 个 context_events）

- 平均每事件 104.6 个节点，最大 132，最小 0；最大深度 14。
- `bounds_grid` 数值范围在样本第一个事件中 `right ≤ 60，bottom ≤ 133`。该值是「像素 ÷ 24」量化后的相对坐标 grid——对屏幕分辨率粗略归一化但不带物理像素。

---

## 8. 上下文特征（`context_features`）

### 8.1 字段

每个 context event（**含主动的 `FOREGROUND_SNAPSHOT`**）通过 `ContextFeatureExtractor.extract` 派生一条 feature（`Models.kt:170-190`、`ContextFeatureExtractor.kt:1-55`；批内 `allEvents.map { ... }`，见 `CollectionCoordinator.kt:592-599`）。因此 `context_features` 与 `context_events` 始终 1:1，包括快照那一条：

| 字段 | 计算方式 | 取值范围 |
|---|---|---|
| `editable_count` | `nodes.count { it.editable }` | int |
| `scrollable_count` | `nodes.count { it.scrollable }` | int |
| `clickable_count` | `nodes.count { it.clickable }` | int |
| `node_class_histogram` | 按 `className.substringAfterLast('.')` 计数 | `{FrameLayout:20, TextView:1, ...}` |
| `media_like_score` | 含 Surface / Player 类名 → 0.8，否则 0.0 | 0.0 / 0.8 |
| `list_like_score` | `scrollable > 0 ? 0.8 : 0.1` | 0.1 / 0.8 |
| `form_like_score` | editable≥2→0.8；editable=1∧clickable>2→0.6；其余 0.1 | 启发式 |
| `game_like_score` | C5 内置任务置高；否则 `media>0.5 ∧ clickable≤1 → 0.4`，其余 0.1 | 启发式 |
| `keyboard_visible_estimated` | `input_method_visible || editableCount>0`（`JsonCodec.kt:190`） | bool |
| `coarse_orientation` | 从当前 `Configuration.orientation` + display rotation 归一化 | `portrait` / `landscape` / `portrait_reverse` / `landscape_reverse` / `unknown` |
| `estimated_context_category` | 启发式：先用 `taskCategory.name`，否则根据 editable/IME/scrollable/media 推断为 C2/C3/C4/C6/UNKNOWN | 字符串 |

### 8.2 当前的缺陷

- `coarse_orientation` 已改为动态值；事件发生时仍可能受系统旋转锁、折叠屏/多窗口等系统报告影响。
- `estimated_context_category` 在有 `taskCategory` 时直接抄 task 标签，等于没"估计"。样本里全部 40 条都是 `"C5"`，没有任何独立路由价值。
- 评分函数取值离散，是研究用启发式，不会自动覆盖 C0/C1/C5/C7 这些非 UI 场景。

### 8.3 样本片段

```json
{
  "clickable_count": 3,
  "coarse_orientation": "portrait",
  "collection_source": "BUILTIN_TASK",
  "computed_at_wall_millis": 1779886049156,
  "editable_count": 0,
  "estimated_context_category": "C5",
  "event_id": "7508a017-88b1-4899-b05d-1bd6e83e7778",
  "event_type": "TYPE_WINDOW_CONTENT_CHANGED",
  "feature_id": "9a4339fc-e983-492e-843c-ea1363053ded",
  "form_like_score": 0.1, "game_like_score": 0.1,
  "input_method_visible": false, "keyboard_visible_estimated": false,
  "list_like_score": 0.1, "media_like_score": 0.0,
  "node_class_histogram": {"FrameLayout":20,"ImageView":1,"LinearLayout":3,
    "LinearLayoutCompat":1,"RecyclerView":1,"RelativeLayout":2,"TextView":1,
    "View":3,"ViewGroup":4},
  "password_node_seen": false,
  "scrollable_count": 0,
  "task_category": "C5","task_id": "C5","task_sequence": 5,
  "task_name":"Blue ball tapping","task_intuitive_description":"Landscape touch challenge",
  "task_session_id": "3a6dcfec-fe08-4813-b1af-885a956b6b2b"
}
```

---

## 9. 脱敏摘要与诊断

### 9.1 `redaction_summary`（在每个 context_event 内）

`RedactionEngine.kt:29-42` 定义计数键（drop-all-text 后均为"丢弃计数"，无内容，旧的 `replaced_*`/`redacted_plain_text`/`dynamic_*` 已移除）：

| 字段 | 增加条件 |
|---|---|
| `dropped_password_nodes` | password 子树被丢弃 |
| `dropped_editable_texts` | editable 节点的原 text 被丢弃 |
| `dropped_text_nodes` | 非编辑节点存在非空 text 而被丢弃 |
| `dropped_content_descriptions` | 节点存在非空 contentDescription 而被丢弃 |
| `dropped_window_titles` | 事件/窗口标题文本被丢弃 |

### 9.2 `diagnostics`（在批次顶层）

写入位置 `JsonCodec.kt:59-67`：

```json
"diagnostics": {
  "sensor_sample_count": 1671,
  "context_event_count": 40,
  "sampling_rate_hz": 100,
  "redaction_applied": true,
  "compression": "lz4_frame",
  "encryption": "none",
  "gated_resume": false
}
```

- `redaction_applied: true` 是服务端 schema 必检字段（`schemas.py:239`）。
- `encryption: "none"` 是当前阶段的明示标记——意味着上传 payload 本身没有 AES。

### 9.3 本地诊断 / 队列状态（**不上传**）

`DiagnosticsState`、`UploadHistoryEntry`、`ClockSyncState`、`SensorRuntimeMetrics` 仅在 UI 显示并保存在本地 SQLite `upload_queue.db`（`QueueMetadataStore.kt`），不进入上传 batch。

---

## 10. 设备 / 网络 / 时间同步元信息

### 10.1 设备身份

```text
device_id = lowercase_hex(HMAC-SHA256(serverStudySalt, Settings.Secure.ANDROID_ID))
```

代码：`DeviceIdProvider.kt:30-52`。serverStudySalt 默认 `"Continuous_Authentication"`，可通过 `/api/v1/config` 下发覆盖。设备 ID 缓存到 `EncryptedSharedPreferences`（`MasterKey.KeyScheme.AES256_GCM`）。**没有采集 IMEI / serial / MAC / MediaDrm / Wi-Fi BSSID**。

### 10.2 时间同步

- `ClockSyncService` 启动后每 60 s 同步一次（`ClockSyncService.kt:33-41`）。
- 首选 NTP（默认 8 个中国区 NTP host：`ntp.ntsc.ac.cn`, `ntp.cloud.aliyuncs.com`, `ntp.aliyun.com`, `ntp.tencent.com`, `0-3.cn.pool.ntp.org`）。
- NTP 不通则退化为 `/api/v1/config.serverTimeMillis` 的 HTTP midpoint。
- 当前 ClockSync 仅影响 `baseWallMillis`（`SensorCollector.kt:62`）、即传感器与触控的墙钟时间估算。`ClockSyncState` 不进入 batch payload。

### 10.3 网络上下文

- `wifi_only` 是一个本地策略开关，控制 `Uploader` 是否在非 Wi-Fi 上传。
- 是否使用 Wi-Fi、移动数据、网络类型、信号强度、运营商、地区——**全部未采集**。
- 失败队列上限 200 MB FIFO，重试指数退避（5 s 起，上限 5 min），20 次后转入死信目录（`Uploader.kt:188-212`）。

### 10.4 其他设备状态

- 屏幕状态：`PowerManager.isInteractive`、`KeyguardManager.isKeyguardLocked`、`ACTION_SCREEN_OFF/ON/USER_PRESENT` 用作 ScreenGate，但事件本身不入 batch。
- 电池：仅查询 `isIgnoringBatteryOptimizations` 作为权限条件。**电量百分比、充电状态、温度不采集**。
- 物理姿态：仅由 IMU 推断，没有调用 `Display.getRotation` / `Configuration.orientation` 写入 payload。

---

## 11. 不采集 / 已显式排除的字段

`docs/privacy_model.md` 与 `docs/requirements_matrix.md` APP-09 明确禁止以下字段进入 batch；测试在 `BatchSerializationTest.kt` 与 `RedactionEngineTest.kt` 中以断言形式锁定：

| 类别 | 禁止字段 | 锁定测试 |
|---|---|---|
| 硬件 ID | IMEI、Serial、MAC、MediaDrm | `DeviceIdProviderTest`、要求矩阵 APP-08 |
| 视觉 | 截图、屏幕录制 | 无相关代码 |
| 输入动态 | 逐字 text-change、按键时序、按键间隔、按键时长 | `ResearchAccessibilityService.shouldProcess()` 强制 `TYPE_VIEW_TEXT_CHANGED → false` |
| 输入文本 | EditText 原文、密码 | `RedactionEngine.sanitizeNode()`；server `NodeSnapshot.reject_password_nodes` |
| 触控细节 | x、y、pressure、size、轨迹、velocity | `BatchSerializationTest.batchJsonContainsTouchTimingWithoutPositionFields`；server `TouchEvent` 模型 `extra="forbid"` |
| 自动化 | performAction、自动点击、手势注入 | `AccessibilityService` 仅订阅，未调用 `performAction` |
| 原始 MotionEvent | 任意 App 的触控坐标、历史点、pressure、size | 仅记录 Accessibility 全局触控交互开始/结束时间，不读取原始 MotionEvent |
| 包名黑名单 | 全包名跳过 | 已废弃，`skip_events` 总为空数组 |

---

## 12. 实际样本字段量化（来自 `51dfc982-...json`）

> 说明：该样本由**修复前**的实现采集（彼时 context_events 仅来自被动事件），因此其字段计数中不含 `FOREGROUND_SNAPSHOT`。修复后，在无障碍服务连接时每批会额外多出 1 条 `FOREGROUND_SNAPSHOT` 及其对应的 `context_feature`；当本窗口期无任何被动事件时，该批的 `context_events`/`context_features` 计数为 1（仅快照），而旧实现下会是 0。下表数值仍准确反映该历史样本本身。

| 维度 | 数值 |
|---|---|
| 批次时长 | 5.938 s（顶层 `batch_duration_seconds=5`） |
| 前台应用 | `com.tencent.mm` / `com.tencent.mm.ui.LauncherUI` |
| 任务 | `C5` "Blue ball tapping" |
| 上下文事件总数 | 40 |
| `context_features` 数 | 40（1:1） |
| 触控事件总数 | 6（3 down + 3 up） |
| Accelerometer 样本 | 563（≈95 Hz） |
| Gyroscope 样本 | 563（≈95 Hz） |
| Magnetometer 样本 | 545（≈92 Hz） |
| 每事件平均节点数 | 104.6（最小 0、最大 132） |
| 每事件最大深度 | 12.8（最大 14） |
| 保留原始 `text` 的节点（**旧构建实测**） | 594（合并所有 40 个事件）。**仅历史值**：该样本由 drop-all-text 之前的保留文本构建采集；当前构建下 `text` 恒 `null`，节点改用 `has_text`/`has_content_description` 存在标志 |
| `redaction_summary` 文本命中（**旧构建实测**） | 旧键 `replaced_*` 全为 0、`redacted_plain_text` 1 次（首个 context event）。**当前构建**已无这些键，仅有 `dropped_*` 丢弃计数 |
| 整批 `encryption` | `"none"` |
| 整批压缩 | `lz4_frame` |
| `coarse_orientation` 标签 | 动态 `portrait`/`landscape`/反向/`unknown`，用于区分 C5/C6 横屏段 |

---

## 附录：完整字段索引表（按 JSON 路径）

```
$.algorithm                                          envelope
$.payload_base64
$.payload_sha256_hex
$.device_id
$.batch_id
$.rule_version
$.rule_hash
$.created_at_wall_millis
$.batch_id                                           batch top-level
$.device_id
$.session_id
$.record_type
$.collection_source
$.app_package_name
$.foreground_activity_class_name
$.foreground_component_name
$.sampling_rate_hz
$.batch_duration_seconds
$.task_sequence | task_id | task_name | task_intuitive_description
$.task_category | task_session_id
$.task_started_at_wall_millis | task_elapsed_seconds_at_batch_end
$.app_version | rule_version | rule_hash | consent_version
$.started_at_wall_millis | ended_at_wall_millis | base_elapsed_nanos
$.sensor_samples[*].sensor_type
$.sensor_samples[*].timestamp_elapsed_nanos | wall_time_estimated_millis
$.sensor_samples[*].x | y | z | accuracy
$.touch_events[*].event_id | event_type
$.touch_events[*].event_time_uptime_millis | event_time_wall_millis
$.touch_events[*].collected_at_wall_millis
$.context_events[*].event_id | event_type | event_time_wall_millis
$.context_events[*].app_package_name | foreground_activity_class_name
$.context_events[*].foreground_component_name | input_method_visible
$.context_events[*].window_title_redacted
$.context_events[*].redaction_summary.{dropped_password_nodes, dropped_editable_texts, dropped_text_nodes, dropped_content_descriptions, dropped_window_titles}
$.context_events[*].root_nodes[*].node_id | class_name | viewIdResourceName
$.context_events[*].root_nodes[*].bounds_grid.{left,top,right,bottom}
$.context_events[*].root_nodes[*].clickable | editable | scrollable | checkable | checked
$.context_events[*].root_nodes[*].enabled | focused | selected | visible_to_user | long_clickable | password
$.context_events[*].root_nodes[*].input_type_category | child_count
$.context_events[*].root_nodes[*].has_text | has_content_description
$.context_events[*].root_nodes[*].text | text_redacted | content_desc_redacted   // 三者恒 null（drop-all-text）
$.context_events[*].root_nodes[*].actions_summary[*] | depth
$.context_features[*].feature_id | event_id | computed_at_wall_millis | collection_source
$.context_features[*].task_* (sequence/id/name/intuitive_description/category/session_id)
$.context_features[*].input_method_visible | keyboard_visible_estimated
$.context_features[*].editable_count | scrollable_count | clickable_count | password_node_seen
$.context_features[*].media_like_score | list_like_score | form_like_score | game_like_score
$.context_features[*].node_class_histogram.{ClassName: count}
$.context_features[*].event_type | coarse_orientation | estimated_context_category
$.skip_events                                       // 当前总是空
$.diagnostics.{sensor_sample_count, context_event_count, sampling_rate_hz,
               redaction_applied, compression, encryption, gated_resume}
```
