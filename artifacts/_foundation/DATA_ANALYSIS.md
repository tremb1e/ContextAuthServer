# 基础事实材料 B —— 数据侧权威分析（只读，供文档撰写引用）

> **[2026-07-04 历史快照批注]** 本文件基于 `data/testdata/2026-06-11` 旧采集，反映**旧 8 类 `C0..C7` 任务体系、`app_version=1.0.0`、~103 Hz** 的当时状态。任务体系此后演进为正典 **7 类 `I0..I6`**（旧 `I7`→新 `I6`、旧空间采集 `I6` 删除，`C0..C7` / `C0..C6` 均废除为 legacy 兼容标识）；有效采样率其后经 0703 回退（~86 Hz，主线程丢样）→ v1.1.0 `HandlerThread` 修复后回升（0704 在盘实测 accel/gyro 103.3 Hz、mag 100.0 Hz）。**下方正文为历史快照、未改。** 现状以 `docs/ContextAuthServer_服务端说明.md`（§2.1–§2.3、§8）为准。

> 本文件由 data-analyst 子代理产出并经主控独立 python 复算交叉核对，覆盖 `data/testdata/2026-06-11` 下全部 59 个解压批次载荷 + 59 个 `.meta.json` 信封。所有统计扫描全部 59 文件。*Confirmed*=直接实测；*Inference*=解释/推断。
>
> ⚠️ **构建前提（必读）**：`data/testdata/2026-06-11` 由**旧的保留文本构建**采集（彼时 `text` 对只读节点原样输出、content-desc 走 `<TEXT_REDACTED>` 折叠、并有云端可更新脱敏规则）。**当前构建已切换为 drop-all-text**：所有显示/输入文本一律端侧丢弃，`text`/`text_redacted`/`content_desc_redacted`/`window_title_redacted` 恒 `null`，节点改用布尔 `has_text`/`has_content_description` 存在标志；旧的 `replaced_*`/`redacted_plain_text`/`dynamic_*` 计数键与 `/api/v1/rules`/`RuleUpdateClient`/`RedactionPolicy` 均已移除，`redaction_summary` 现仅含 `dropped_*` 丢弃计数。**因此本文中所有关于 `text`/`text_redacted`/`content_desc_redacted` 的实测数字（10,651 / 38.5% / 594 / 保留 UGC 样例等）均为旧构建历史实测，不代表当前行为。** 对应的"`text` 漏 UGC"隐私风险在当前构建下已由 drop-all-text 解决（见 §D、§H）。

---

## A. 数据集概览
| 指标 | 值 |
|---|---|
| 批次载荷 | **59** 个 `<uuid>.json` + 59 个 `.meta.json` |
| 压缩字节合计（meta `compressed_size_bytes`） | **5,566,867 B（5.31 MiB）** |
| 解压字节合计（meta `decompressed_size_bytes`） | **34,851,598 B（33.24 MiB）** |
| 整体压缩比 | **6.26×**（LZ4_FRAME+JSON） |
| 加密 | **`none`**（diagnostics.encryption，全 59） |
| schema 校验 | 全 59 **`ok`** |
| `rule_version` / `rule_hash` | 全 `1` / 单值 `c6717ff1…c48e51e`（**旧构建值**；当前构建 `rule_hash`=64 个 0 固定基线常量） |
| `consent_version` / `record_type` / `app_version` / `sampling_rate_hz` | `1` / `collection` / `1.0.0` / `100`（全常量） |
| **设备数** | **1** — `36905bdefcce0ca2684a30e9d0c1202d05b8a9412fa06eba2c1d0d6f8f573638` |
| **时间跨度** | 2026-06-11 **10:50:31 → 10:55:28 UTC（约 4.94 分钟）** |
| 批时长 | min 0s / median 5s / mean 4.93s / max 7s |
| **会话数 `session_id`** | **9 个**（极不均：554375c6…=20 批、f54ca9c4…=17、0beac709…=7、e9e82c98…=6、b4971b3f…=4、33706efc…=2、其余 3 个各 1） |
| `task_session_id` | **4 个**（对应 4 个引导任务实例） |

> **meta 警示（confirmed）**：信封 `payload_sha256_hex` 与磁盘 `.json` 不匹配——磁盘文件是原设备字节流的**重新缩进/美化副本**。压缩比用 meta 的 `*_size_bytes`，磁盘文件内容忠实但非字节忠实。

---

## B. 权威字段清单（数据字典）
> "null/empty" 计 `None`/`""`/`[]`/`{}`。

### B.1 批次顶层（59）
| 字段 | 类型 | 在场/空 | 例 | 备注 |
|---|---|---|---|---|
| app_package_name | str | 59/0 | `com.xingin.xhs` | 4 distinct |
| app_version/base_elapsed_nanos/batch_duration_seconds/batch_id | - | 59/0 | - | - |
| collection_source | str | 59/0 | `THIRD_PARTY_APP` | **{THIRD_PARTY_APP:25, BUILTIN_TASK:34}** |
| consent_version/record_type/rule_hash/rule_version/sampling_rate_hz | - | 59/0 | - | 常量 |
| context_events/context_features/sensor_samples/diagnostics | list/dict | 59/0 | - | 见下 |
| device_id/session_id | str | 59/0 | - | 单设备 / 9 会话 |
| started/ended_at_wall_millis | int | 59/0 | - | epoch ms |
| foreground_activity_class_name / foreground_component_name | str | 59/**1** | - | 1 批为 null |
| **skip_events** | list | 59/**59** | `[]` | **恒空** |
| **touch_events** | list | 59/**59** | `[]` | **恒空（无触摸动力学）** |
| task_category/id/name/intuitive_description/sequence/session_id/started_at/elapsed | mixed | 59/**25** | `C5` | 仅 34 个引导任务批 |

### B.2 context_events[]（880）
| 字段 | 在场/空 | 备注 |
|---|---|---|
| app_package_name/coarse_orientation/event_id/event_time_wall_millis/event_type | 880/0 | event_type 8 distinct |
| foreground_activity_class_name / foreground_component_name | 880/**54** | 54 事件为 null |
| input_method_visible / redaction_summary | 880/0 | - |
| root_nodes | 880/**16** | **16 事件 0 节点** |
| window_title_redacted | 880/**59** | 旧构建多为空/null；**当前构建恒 `null`**（drop-all-text，标题文本一律丢弃） |

### B.3 root_nodes[]（27,692，UI 树）
| 字段 | 在场/空 | 备注 |
|---|---|---|
| node_id | 27692/0 | `{depth}_{accessibilityId}` |
| class_name | 27692/**384** | 22 distinct；null 1.4% |
| depth | 27692/0 | 0–12 |
| child_count | 27692/0 | 0–139 |
| bounds_grid{left,top,right,bottom} | 27692/0 | 粗网格，见 §D |
| actions_summary | 27692/**16738** | 60% 为空 |
| **text** *(旧构建)* | 27692/**17041** | **旧构建实测：原始用户文本，10,651 非空（38.5%）**。**当前构建恒 `null`**（drop-all-text），改用 `has_text` 布尔 |
| text_redacted *(旧构建)* | 27692/**27584** | 旧构建仅 108 个（=可编辑丢弃占位 `<EDITABLE_TEXT_DROPPED>`）；**当前构建恒 `null`** |
| content_desc_redacted *(旧构建)* | 27692/**23151** | 旧构建 4,541 个（`<TEXT_REDACTED>` 折叠）；**当前构建恒 `null`**，改用 `has_content_description` 布尔 |
| viewIdResourceName | 27692/**14659** | **13,033 个（47.1%）原样输出**（编译期资源 ID 语义，非用户数据） |
| has_text / has_content_description *(当前构建新增)* | — | 旧构建样本无此字段；当前构建以布尔存在标志替代 `text`/`content_desc_redacted` 的内容 |
| input_type_category | 27692/**27584** | 仅 108 个可编辑节点为 `text` |
| clickable / long_clickable / scrollable / editable | 27692/0 | true 率 33.2% / 11.6% / 2.3% / 0.4% |
| checkable / checked / focused / selected | 27692/0 | 0.7% / 0.2% / 0.6% / 1.0% |
| enabled / visible_to_user | 27692/0 | 98.6% / 62.2% |
| **password** | 27692/0 | **0% true（从无密码节点）** |

### B.4 context_features[]（880，按 event_id 关联）
| 字段 | 在场/空 | 备注 |
|---|---|---|
| feature_id/event_id/event_type/computed_at_wall_millis/collection_source/coarse_orientation | 880/0 | event_id 为外键 |
| estimated_context_category | 880/0 | 7 distinct（C2–C7,UNKNOWN） |
| clickable_count / editable_count / scrollable_count | 880/0 | 0–141 / 0–1 / 0–4 |
| form_like_score / game_like_score / list_like_score | 880/0 | {0.1..0.6} / {0.1,0.8} / {0.1..0.8} |
| **media_like_score** | 880/0 | **恒 0.0（死特征）** |
| node_class_histogram | 880/**114** | 0 节点时空 |
| keyboard_visible_estimated / input_method_visible | 880/0 | - |
| **password_node_seen** | 880/0 | **0% true** |
| task_category/id/name/intuitive_description/sequence/session_id | 880/**279** | 引导事件 601 个有值 |

### B.5 sensor_samples[]（90,132）
`sensor_type`{ACCELEROMETER,GYROSCOPE,MAGNETIC_FIELD}、`timestamp_elapsed_nanos`、`wall_time_estimated_millis`、`x,y,z`、`accuracy`{1,3}。

### B.6 diagnostics{}（59）
`compression=lz4_frame`、`encryption=none`、`redaction_applied=true`、`gated_resume=false`、`sampling_rate_hz=100`、`context_event_count`、`sensor_sample_count`、`touch_event_count`（**恒 0**）。**全 59 批诊断计数与实际数组长度逐一相等（0 失配）。**

### B.7 redaction_summary{}（每 event，880）
**旧构建实测键**：常驻键 `dropped_editable_texts,dropped_password_nodes,redacted_plain_text,replaced_{card,email,id_number,number,phone,token,url}`；条件键（触发才出）`dynamic_long_number`(46 事件)、`dynamic_opaque_token`(106)、`dynamic_payment_card`(6)。**当前构建（drop-all-text）已改为**仅 `dropped_password_nodes,dropped_editable_texts,dropped_text_nodes,dropped_content_descriptions,dropped_window_titles` 五个丢弃计数键——上述 `replaced_*`/`redacted_plain_text`/`dynamic_*` 键均已移除。

### B.8 skip_events[] / touch_events[]：**全 59 批皆空。**

---

## C. 分布
### C.1 按包
| 包 | 批 | context_events | 角色 |
|---|---|---|---|
| com.contextauth | 32 | 361 | 采集器/控制器 + 内建任务 |
| com.xingin.xhs（小红书） | 13 | 187 | 社交信息流 |
| com.tencent.mm（微信） | 13 | 320 | 即时通讯 |
| com.miui.home（桌面） | 1 | 12 | 主屏 |

### C.2 estimated_context_category（事件/特征数）
| 类别 | 数 | 映射专家（推断） |
|---|---|---|
| C5 | 348 | GAME_OR_TILT（点击蓝球，横屏） |
| C2 | 252 | SCROLL_BROWSE（信息流滑动） |
| C3 | 199 | TYPING/FORM_FILLING（抄写，键盘弹出） |
| C6 | 60 | VIDEO_WATCHING（本地视频） |
| C7 | 13 | GAME_OR_TILT/倾斜（手腕转动） |
| UNKNOWN | 6 | — |
| C4 | 2 | （仅 2 样本，欠采） |

### C.3 类别 × 包 交叉表
| 类别 | contextauth | xhs | mm | miui.home |
|---|---|---|---|---|
| C2 | 72 | 168 | 0 | 12 |
| C3 | 107 | 13 | 79 | 0 |
| C4 | 0 | 2 | 0 | 0 |
| C5 | 107 | 0 | 241 | 0 |
| C6 | 60 | 0 | 0 | 0 |
| C7 | 13 | 0 | 0 | 0 |
| UNKNOWN | 2 | 4 | 0 | 0 |

> **强包-类别耦合（重要）**：C6/C7 仅现于 com.contextauth；C2 由 xhs 主导；C5 由微信主导。按包路由会短路 UI 特征，但无法泛化到"另一 App 中的同一活动"。
> **标签泄漏（重要）**：引导任务标签出现在真实应用前台之上（C5 任务叠 微信 241×、C3 任务叠 微信 79×）——测试者在 30s 计时任务中切换了应用 → **本数据集任务标签非干净真值**。

### C.4 event_type
TYPE_WINDOW_CONTENT_CHANGED 502、TYPE_WINDOWS_CHANGED 170、TYPE_WINDOW_STATE_CHANGED 59、**FOREGROUND_SNAPSHOT 59（每批恰 1）**、TYPE_VIEW_SCROLLED 58、TYPE_VIEW_CLICKED 25、TYPE_VIEW_FOCUSED 6、TYPE_VIEW_SELECTED 1。合计 880。

### C.5 sensor_type + 有效 Hz
| sensor | 样本 | 有效 Hz（每批中位，恒定） | accuracy |
|---|---|---|---|
| ACCELEROMETER | 30,384 | 103.3 | 3（恒） |
| GYROSCOPE | 30,373 | 103.3 | 混 {1:9379, 3:20994} |
| MAGNETIC_FIELD | 29,375 | 100.0 | 3（恒） |

### C.6 其他率（n=880）
朝向：portrait 774 / landscape 106（~12% 横屏）；keyboard_visible_estimated=true 350（39.8%）；input_method_visible=true 332（37.7%）；**password_node_seen=true 0（0%）**。

---

## D. UI 树统计（27,692 节点）
| 指标 | 值 |
|---|---|
| 每事件节点数 | min 0 / median 20 / mean 31.5 / max 218 |
| 0 节点事件 | 16/880（1.8%） |
| 深度分布 | mode=5（6403）；0:101…12:448 |
| child_count | 0:16966(61%)…139:8（异常容器） |

**布尔 true 率**：enabled 98.6%、visible_to_user 62.2%、clickable 33.2%、long_clickable 11.6%、scrollable 2.3%、selected 1.0%、checkable 0.7%、focused 0.6%、editable 0.4%、checked 0.2%、**password 0.0%**。

**Top 类名（共 22 distinct）**：TextView 10643(38.4%)、View 3615(13.1%)、FrameLayout 2960(10.7%)、LinearLayout 2297(8.3%)、ImageView 2249(8.1%)、Button 1337(4.8%)、RelativeLayout 1112、ViewGroup 1089、RecyclerView 749、ActionBar$Tab 264、ViewPager 198、CheckBox 176、ProgressBar 170、**EditText 108**、VideoView 49、Scroll/HorizontalScrollView 48/66。

**文本类字段覆盖（旧构建实测）**：text(原始) 10,651（38.5%，长 1/中位 6/max 96）、viewIdResourceName 13,033（47.1%）、content_desc_redacted 4,541（16.4%）、text_redacted 108（0.4%）。**当前构建下 `text`/`content_desc_redacted`/`text_redacted` 恒 `null`**，仅 `viewIdResourceName`（编译期资源 ID）继续输出，文本覆盖改由 `has_text`/`has_content_description` 布尔表达。

**bounds_grid 分辨率（inference）**：剔除哨兵后有效范围 left[-37..283]/top[-45..323]/right[-298..175]/bottom[-104..234]，modal right=60、p95 right≈63、bottom≈133 → 粗归一网格约 **176(W)×235(H) 格**（降采样屏坐标）。
- **异常（confirmed）**：**93/27,692（0.3%）节点**某坐标含哨兵值 **±89,478,485**（越界/无界 a11y 节点，特征工程前须过滤）。

**redaction_summary 聚合（旧构建实测，旧键）**：redacted_plain_text **4,597**、dropped_editable_texts **108**、dropped_password_nodes **0**、replaced_number 42、dynamic_opaque_token 106、dynamic_long_number 48、dynamic_payment_card 6；replaced_{phone,email,id_number,card,token,url} 全 0。**注**：这些 `replaced_*`/`redacted_plain_text`/`dynamic_*` 键已在当前构建移除；当前仅有 `dropped_password_nodes/dropped_editable_texts/dropped_text_nodes/dropped_content_descriptions/dropped_window_titles`。
- **文本通道发现（旧构建实测，confirmed；当前已解决）**：在旧的保留文本构建下，`text` 字段对只读节点**原样输出**。保留样例含完整 UGC：`'拉💩'`、`'关注'`、`'我已反省！我已认错！我已投降！'`、`'天天戴头盔🪖出门，唯独今天出门办事不方便…'`、`'共 30 条评论'`、`'小红薯<NUM>C520'`、`'1小时前 北京 回复'`（时间戳+**地理位置"北京"**）。彼时仅结构化 PII 被模板化（计数全 0），自由文本评论/用户名/粗位置在 `text` 通道穿透，与当时步骤(2) 脱敏意图相悖，构成隐私风险。**当前构建已由 drop-all-text 解决此问题**：`text` 恒 `null`，不再有任何 UGC 残留，仅保留 `has_text` 存在标志；该"`text` 漏 UGC"风险已 CLOSED。

---

## E. 逐类别特征画像 —— MoE 可行性（关键分析）
| 类别 | n | clickable(均/中) | editable | scrollable | form | game | list | media | kbd% | nodes(中) | 主导包 | 朝向 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **C2** | 252 | 29.0/19 | 0.0 | 1.9/1 | 0.1 | 0.1 | 0.8/1 | 0.0 | 0% | 70 | xhs(168) | portrait |
| **C3** | 199 | 2.6/2 | 0.5/1 | 0.6/1 | 0.16 | 0.1 | 0.5/1 | 0.0 | **86%** | 19 | contextauth/mm | portrait |
| **C4** | 2 | 13.0 | 0.0 | 0.0 | 0.1 | 0.1 | 0.1 | 0.0 | 0% | 26 | xhs(2) | portrait |
| **C5** | 348 | 2.8/0 | 0.0 | 0.07 | 0.1 | **0.8/1** | 0.1 | 0.0 | 51% | 2 | mm(241) | portrait+59 land |
| **C6** | 60 | 5.4/6 | 0.0 | 0.27 | 0.1 | 0.1 | 0.3 | 0.0 | 0% | 29 | contextauth | **landscape 45** |
| **C7** | 13 | 4.0/4 | 0.0 | 1.0/1 | 0.1 | 0.1 | 0.8/1 | 0.0 | 0% | 31 | contextauth | portrait |
| **UNK** | 6 | 0.0 | 0.0 | 0.0 | 0.1 | 0.1 | 0.1 | 0.0 | 0% | 0 | xhs/contextauth | portrait |

**逐类主导节点类**：C2 TextView/FrameLayout/LinearLayout/ImageView/ViewGroup（密信息流 ~70 节点）；C3 TextView/View/**EditText(全 107 个 EditText 在此)**/Button（input_method_visible=154，键盘 86%）；C5 稀疏（中位 **2 节点**）TextView/View/Button；C6 **VideoView(49)/CheckBox(176)**，多横屏（45/60）；C7 TextView/View/Button/**ProgressBar(13)**。

**逐特征判别力（类间方差/类内方差，F-like）**：game_like_score ∞（类内≈0，确定性规则非学习梯度）；list_like_score 2.35（有用）；scrollable_count 1.42（中等）；editable_count 0.98（弱，仅分 C3）；clickable_count 0.81（C2 内高方差稀释）；**form_like_score 0.11（近无用）**；**media_like_score N/A（恒 0，无信号区分视频）**。

**可分性（z 标准化类均向量的欧氏距离）：**
```
        C2     C3     C4     C5     C6     C7    UNK
  C2   0.00   2.73   2.80   3.53   2.45   1.55   3.07
  C3   2.73   0.00   2.23   2.91   1.90   2.04   2.17
  C4   2.80   2.23   0.00   2.12   0.71   2.25   0.67
  C5   3.53   2.91   2.12   0.00   2.10   2.90   2.06
  C6   2.45   1.90   0.71   2.10   0.00   1.62   0.65
  C7   1.55   2.04   2.25   2.90   1.62   0.00   2.21
  UNK  3.07   2.17   0.67   2.06   0.65   2.21   0.00
```
- **最易混：C6↔UNKNOWN(0.65)、C4↔UNKNOWN(0.67)** —— 视频(C6)在这些特征上几乎与垃圾桶不可分（因 media 死）。**最可分：C5(游戏)、C2(浏览)**——但 C5 几乎全靠二值 `game_like_score`、C2 靠原始 `clickable_count`。
- **最近质心自洽率仅 70.1%**（617/880）。主要混淆：C3→UNKNOWN(89)、C2→C7(87)、C6→UNKNOWN(44)、C5→C7(19)。

> **MoE 可行性结论（inference）**：①7 个标量多为量化规则输出（form/game/list∈{0.1..0.8}；media≡0），仅训这些等于记忆规则并继承其盲点；②类别在这些特征上**非干净可分**（C6/C4 塌进 UNKNOWN；C5↔C7、C2↔C7 重叠）；③真正判别信号在 **`node_class_histogram`（VideoView→C6、EditText→C3）+ 包名**，远强于标量分；④因 §F 中 601 引导事件的 `estimated==task` 100%，这些行的高表观可分性部分是循环的。

---

## F. 标签分析
- 事件级 `task_category` 有值 **601** / null **279**；批级 34/59 有值（全为 BUILTIN_TASK；25 个 null 全为 THIRD_PARTY_APP）。
- task_category→task_name：C5 "Blue ball tapping"(348)、C3 "Paragraph copy"(180/199)、C6 "Local video playback"(60)、C7 "Wrist rotation"(13)。

**一致矩阵 —— task_category(行)×estimated_context_category(列)，仅有任务标签 601 行：**
| task↓\est→ | C3 | C5 | C6 | C7 | 行计 | 对角一致 |
|---|---|---|---|---|---|---|
| C3 | 180 | 0 | 0 | 0 | 180 | **100%** |
| C5 | 0 | 348 | 0 | 0 | 348 | **100%** |
| C6 | 0 | 0 | 60 | 0 | 60 | **100%** |
| C7 | 0 | 0 | 0 | 13 | 13 | **100%** |

**总体 estimated==task = 601/601 = 100.0%。**
> **关键解读（inference）**：引导批上"估计"类别即注入的任务标签——零分歧。说明估计器在有任务时被任务种子/覆盖（见代码 §2.3 规则首条 `taskCategory != null -> taskCategory.name`）。故一致矩阵**不提供对估计器的独立验证**。估计器真实行为只能在 **279 个未标注(THIRD_PARTY)事件**上观察（产 C2 252、及 C3/C4/UNKNOWN）。**C2(浏览)与 C4 完全无引导真值。** 把 `estimated_context_category` 当**规则输出**而非已验证分类器；仅 C3/C5/C6/C7 四个域有真值。

---

## G. 传感器分析
| 传感器 | 样本 | 有效 Hz（每批中位，min=max 极稳） | 单位(inference) | 各轴范围(min..max, 均±std) |
|---|---|---|---|---|
| ACCELEROMETER | 30,384 | 103.3 | m/s² | x[-16.3..29.0]μ1.15σ3.80；**y[-4.2..12.3]μ7.96**σ3.18；z[-14.6..22.0]μ2.78σ1.90 |
| GYROSCOPE | 30,373 | 103.3 | rad/s | x[-9.4..5.6]μ0.003σ0.54；y[-13.8..17.5]μ0.005σ0.99；z[-6.6..8.5]μ0.009σ0.74 |
| MAGNETIC_FIELD | 29,375 | 100.0 | µT | x[-26.4..36.5]μ0.61σ6.69；y[-40.9..11.8]μ-11.88σ5.94；z[-41.8..25.9]μ-36.63σ5.50 |

- 每批传感器总数：min 245 / median 1620 / mean 1528 / max 1732（最低对应 0–2s 短批）。
- 单位由量级证实：accel **y≈+7.96 m/s²**（接近 g 的大分量）→ 竖屏握持、重力在 +Y，m/s²；gyro 小（rad/s）；mag 数十 µT。**均为 SI 原始单位，非归一。**
- accuracy：accel&mag 恒 3（HIGH）；gyro 混 1（9379）/3（20994）。
- 规律性：**0 批有 >50ms 间隔**——~100Hz 均匀采样（~103Hz 系设备略快于请求）。
- **事件窗对齐（逐 UI 事件切片可行性）**：**758/880** 事件落在其批传感器墙钟窗内（±200ms）；**122** 落外（批首 FOREGROUND_SNAPSHOT/窗口事件 + 16 个 0 节点事件）。事件 ±500ms 窗中位含 **306 样本**（3 传感器→~100/传感器→干净 1 秒三轴 IMU 切片）。**0 批零传感器**，每批均支持 IMU 加窗。

> **inference**：逐事件 IMU 窗（~1s、~100Hz、3 传感器）易抽取，是缺失触摸动力学的天然替代，也是 倾斜/运动 专家（C5 游戏、C7 手腕、C6 视频静止度）最强信号。

---

## H. 面向"MoE 路由 + 持续认证"的数据质量与局限
| 问题 | 状态 | 影响 |
|---|---|---|
| **单设备** | confirmed（1 device_id/59 批） | **无跨设备泛化。** |
| **单一短时段** | confirmed（全程 4.94 分钟，一天 10:50–10:55 UTC） | 时间覆盖极小、无昼夜/行为变化、过拟合高危。 |
| **单用户（很可能）** | inference | **无逐用户生物特征对比**——可建上下文路由，但**无法训练/评估每专家认证器的判别**（无冒充/合法划分，单身份）。 |
| **touch_event_count=0** | confirmed（全 59 诊断+空数组） | **无触摸动力学**（滑动速度/压力/驻留）；认证阶段失最常用行为生物特征，仅余 IMU+UI 结构。 |
| **加密=none** | confirmed | 静态明文；步骤(3)"加密"未在此数据集行使。 |
| **文本未脱敏（旧构建）→ 当前已解决** | 旧构建 confirmed；当前 CLOSED | 旧构建 `text` 留原始 UGC（评论/用户名/地理位置/emoji），有隐私风险。**当前构建（drop-all-text）`text` 恒 `null`、零 UGC**，该风险已由文本端侧丢弃 CLOSED；本就无 text 特征可用，路由/认证改用 `has_text`/UI 结构。 |
| **类别不均衡** | confirmed（C5:348,C2:252,C3:199,C6:60,C7:13,C4:2,UNK:6） | **C4(2)/C7(13) 太小难训/验**；UNKNOWN(6) 是杂类；8 专家仅 ~5 个有数据、仅 4 个有真值。 |
| **包-类别耦合** | confirmed | C6/C7 仅 com.contextauth、C2 多 xhs、C5 多微信 → 路由可作弊于包，活动跨 App 不迁移。 |
| **标签非独立于估计器** | confirmed（§F 100%） | 标注行上无法验证弱标签，监督信号部分循环。 |
| **media 死/form 惰** | confirmed | 7 标量中 2 个近无信息；VIDEO_WATCHING 欠定。 |
| **哨兵坏 bounds** | confirmed（93 节点 ±89,478,485） | 几何特征前须过滤。 |
| **畸形/边缘批** | confirmed | 16 个 0 节点事件；1 批 null 前台组件；0–2s 短批。但**全 59 过 schema 校验、诊断计数全对（0 失配）**。 |
| **缺第 8 专家覆盖** | inference | 8 专家中引导任务仅覆盖 ~4 域（游戏/点击、打字、视频、倾斜）；STATIC_READING、IDLE_HOLDING、TAP_NAVIGATION 无标注样本。 |

**前台多样性（confirmed）**：批级 8 distinct、事件级 9 distinct 前台组件/活动。Top：`com.tencent.mm/.ui.LauncherUI`(306)、`com.contextauth/.ui.MainActivity`(182)、`com.xingin.xhs/…NoteDetailActivity`(100)、`com.contextauth/androidx.compose.ui.window.DialogWrapper`(96)、`com.xingin.xhs/.index.v2.IndexActivityV2`(57)、`com.miui.home/.launcher.Launcher`(47)、`com.tencent.mm/android.widget.FrameLayout`(19)、`…NoteCommentActivity`(13)、`…ChattingMainUI`(6)。界面多样性很低。

**零节点/零传感器**：16/880 事件 0 节点（TYPE_WINDOW_CONTENT_CHANGED 12、TYPE_VIEW_SCROLLED 2、TYPE_WINDOWS_CHANGED 1、TYPE_VIEW_CLICKED 1）；**0/59 批零传感器**。

> **总体（inference）**：本语料是**单受试者管线冒烟测试/试点**，适合验证"采集+特征抽取+上下文路由管线"，但**不足以训练/评估认证阶段**（单用户、单设备、5 分钟、无触摸）。可支持：①4–5 个上下文域的路由 sanity-check；②IMU 窗特征工程；③脱敏覆盖审计。**不支持**可泛化生物认证结论。

---

## I. 精简样例
**I.1 context_event（C2 浏览，小红书，2 节点）** —— **以下为当前构建（drop-all-text）形态**：`redaction_summary` 改为 `dropped_*` 键，节点 `text`/`content_desc_redacted` 恒 `null`、改用 `has_text`/`has_content_description` 存在标志。括注标出旧构建在同一节点上曾输出的文本（已不再采集）。
```json
{"event_id":"486ecd24-…","event_type":"TYPE_WINDOW_CONTENT_CHANGED","app_package_name":"com.xingin.xhs",
 "coarse_orientation":"portrait","foreground_component_name":"com.xingin.xhs/…NoteDetailActivity",
 "input_method_visible":false,
 "window_title_redacted":null,
 "redaction_summary":{"dropped_password_nodes":0,"dropped_editable_texts":0,"dropped_text_nodes":7,"dropped_content_descriptions":6,"dropped_window_titles":0},
 "root_nodes":[
  {"node_id":"2_-2147454604","class_name":"android.widget.FrameLayout","depth":2,"child_count":9,
   "bounds_grid":{"left":0,"top":0,"right":60,"bottom":133},"clickable":false,"visible_to_user":true,
   "password":false,"has_text":false,"has_content_description":false,
   "text":null,"text_redacted":null,"content_desc_redacted":null,
   "viewIdResourceName":"com.xingin.xhs:id/0_resource_name_obfuscated"},
  {"node_id":"5_-2147389001","class_name":"android.widget.TextView","depth":5,"child_count":0,
   "bounds_grid":{"left":4,"top":110,"right":55,"bottom":121},"clickable":true,"visible_to_user":true,
   "password":false,"has_text":true,"has_content_description":false,
   "text":null,"text_redacted":null,"content_desc_redacted":null,
   "viewIdResourceName":null}]}
   // 旧构建曾在第二个节点输出 text:"1小时前 北京 回复"；当前构建仅以 has_text:true 表示"存在文本"，不含内容
```
**I.2 对应 context_feature → C2**
```json
{"feature_id":"1b69b3aa-…","event_id":"486ecd24-…","estimated_context_category":"C2",
 "clickable_count":49,"editable_count":0,"scrollable_count":2,
 "form_like_score":0.1,"game_like_score":0.1,"list_like_score":0.8,"media_like_score":0.0,
 "keyboard_visible_estimated":false,"collection_source":"THIRD_PARTY_APP",
 "node_class_histogram":{"FrameLayout":10,"ImageView":15,"TextView":28,"Button":3,"View":14},
 "task_category":null}
```
**I.3 引导 context_feature → C5（游戏/点击，微信，横屏）**
```json
{"event_type":"TYPE_VIEW_CLICKED","estimated_context_category":"C5",
 "clickable_count":0,"game_like_score":0.8,"list_like_score":0.1,"media_like_score":0.0,
 "coarse_orientation":"landscape","collection_source":"BUILTIN_TASK",
 "task_category":"C5","task_id":"C5","task_name":"Blue ball tapping","task_sequence":5}
```
**I.4 传感器（同批各一）**
```json
[{"sensor_type":"ACCELEROMETER","x":1.034,"y":7.961,"z":2.715,"accuracy":3,"wall_time_estimated_millis":1781175247127},
 {"sensor_type":"GYROSCOPE","x":0.08774,"y":-0.01833,"z":-0.01817,"accuracy":1,"wall_time_estimated_millis":1781175247127},
 {"sensor_type":"MAGNETIC_FIELD","x":0.609,"y":-11.879,"z":-36.629,"accuracy":3,"wall_time_estimated_millis":1781175247127}]
```
**I.5 信封 meta + diagnostics**（旧构建实测样本；当前构建 `rule_hash`=64 个 0 固定常量，节点文本字段恒 `null`）
```json
{"meta":{"compressed_size_bytes":118921,"decompressed_size_bytes":1089763,"schema_validation_result":"ok",
  "envelope":{"algorithm":"LZ4_FRAME+JSON","rule_version":"1","rule_hash":"c6717ff1…","payload_sha256_hex":"af18d3e2…"}},
 "diagnostics":{"compression":"lz4_frame","encryption":"none","redaction_applied":true,"gated_resume":false,
  "sampling_rate_hz":100,"context_event_count":14,"sensor_sample_count":1657,"touch_event_count":0}}
```

---

## 标题级要点（供文档撰写直接引用）
1. **59 批、1 设备、~5 分钟、9 会话、4 引导任务**——单受试者试点，非认证训练语料。压缩 6.26×，全 schema 合法，诊断计数全对，加密=none。
2. **完全无触摸数据**（touch_events 空、touch_event_count=0）且**无密码节点**——计划中的触摸动力学+密码通道缺席；仅 **IMU（干净 ~100Hz 三轴、可按事件切 ~1s）** 与 **UI 结构** 可用。
3. **`text` 漏 UGC 仅为旧构建历史现象，当前已解决**：旧的保留文本构建下 10,651 节点带原始 UGC（评论/用户名/"北京"地理位置/emoji）。**当前构建（drop-all-text）`text`/`content_desc_redacted`/`window_title_redacted` 恒 `null`、零 UGC 残留**，仅留 `has_text`/`has_content_description` 存在标志；本就无 text 通道特征，该隐私风险已 CLOSED。`viewIdResourceName`（编译期资源 ID，非用户数据）仍输出。
4. **弱标签=任务标签（100% 一致）** 于 601 引导事件——`estimated_context_category` 未被独立验证，C2/C4 无真值；当**规则输出**对待。
5. **仅凭 7 标量的 MoE 路由脆弱**：media≡0（死）、form 惰；C5 全靠二值 game 规则；**C6/C4 与 UNKNOWN 几乎不可分**（z 距 0.65–0.67）；最近质心自洽仅 70%。判别力在 **node_class_histogram（VideoView→C6、EditText→C3）+ 包名**——真实路由应消费它们而非仅标量。
6. **严重不均衡**（C4=2、C7=13、UNK=6）与**包-类别耦合**（C6/C7 仅采集器 App）限制泛化；8 专家仅 ~4–5 域有数据。
