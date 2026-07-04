# ContextAuth Data Contract (Reconnaissance)

> **[2026-07-03/07-04 taxonomy-evolution note]** This contract was extracted under the earlier `C0..C6` paper taxonomy. The task/scene taxonomy has since evolved to the canonical **7 classes `I0..I6`**: the ingest contract now validates against `TASK_CATEGORIES = CANONICAL{I0..I6} ∪ LEGACY{I7, C0..C6}` — a backward-compatible union that never rejects old APKs / on-disk data; old `I7` wrist → new `I6`, and the old spatial-capture `I6` (Scan/frame/capture) was deleted. **The body below is unchanged and reflects the contract as written at the time.** For the current state see `docs/ContextAuthServer_服务端说明.md` (§2.1–§2.3, §8) and `research/README.md`.

Authoritative on-wire / on-disk contract extracted **verbatim** from real source. Research loaders MUST parse exactly these snake_case keys.

Source of truth:
- App serializer: `ContextAuthlab/android-app/src/main/java/com/contextauth/core/JsonCodec.kt`
- App models: `ContextAuthlab/android-app/src/main/java/com/contextauth/core/Models.kt`
- Server validation: `ContextAuthServer/app/schemas.py`
- Storage: `ContextAuthServer/app/storage.py`
- Integrity/ingest: `ContextAuthServer/app/integrity.py`, `ContextAuthServer/app/main.py`

---

## 0. Wire pipeline (integrity + ingest)

Envelope carries a compressed, base64'd batch JSON. **Algorithm = `LZ4_FRAME+JSON`** (`JsonCodec.ALGORITHM`; server `Envelope.algorithm` is `Literal["LZ4_FRAME+JSON"]`).

Producer (`JsonCodec.buildEnvelopeWithMetrics`):
1. `jsonBytes = batchToJson(...).toByteArray(UTF-8)`
2. `compressed = lz4Frame(jsonBytes)` (`LZ4FrameOutputStream`)
3. `payload_sha256_hex = SHA-256(compressed)` — **SHA is over the COMPRESSED bytes**, lowercase hex.
4. `payload_base64 = Base64(compressed)`.

Consumer (`main.py::ingest`), strict order:
1. `Envelope.model_validate_json(raw_body)` → else reject `invalid_envelope` (400).
2. `compressed_bytes = decode_base64(payload_base64)` (strict/validate=True) → else `invalid_base64`.
3. `verify_sha256(compressed_bytes, payload_sha256_hex)` (hmac.compare_digest over **compressed** bytes) → else `payload_hash_mismatch`.
4. `plaintext_bytes = lz4.frame.decompress(compressed_bytes)` → else `corrupted_lz4_payload`.
5. `json.loads(...)` must be a dict → else `invalid_json`.
6. `Batch.model_validate(...)` → on failure **quarantine** `schema_validation_failed` (400).
7. Cross-check `batch.device_id == envelope.device_id` (`envelope_batch_device_id_mismatch`) and `batch.batch_id == envelope.batch_id` (`envelope_batch_id_mismatch`) → quarantine.
8. `STORE.store(...)`; duplicate w/ different bytes → 409 `duplicate_batch_id_conflict`; disk full → 507.

Success response: `{"status":"ok","device_id_prefix":<device_id[:8]>,"batch_id":...,"stored":true}`.

No decryption anywhere. `ingest_decrypt_seconds` metric is an explicit no-op ("this prototype performs no decryption"). **encryption = none.**

---

## (a) PayloadEnvelope — the 8-field envelope

`JsonCodec.envelopeToJson` emits exactly these 8 keys (server `Envelope`, `extra="forbid"`):

| key | type | validation |
|---|---|---|
| `algorithm` | str | `== "LZ4_FRAME+JSON"` |
| `payload_base64` | str | `min_length=1` |
| `payload_sha256_hex` | str | `^[a-f0-9]{64}$` (SHA-256 of compressed bytes) |
| `device_id` | str | `^[a-f0-9]{64}$` (64-hex; salted device hash, no PII) |
| `batch_id` | str | must parse as `uuid.UUID` |
| `rule_version` | str | free string (app default `"1"`) |
| `rule_hash` | str | `^[a-f0-9]{64}$` (baseline = 64 zeros) |
| `created_at_wall_millis` | int | `>= 0` (set to `batch.startedAtWallMillis`) |

**The 8 envelope keys:** `algorithm`, `payload_base64`, `payload_sha256_hex`, `device_id`, `batch_id`, `rule_version`, `rule_hash`, `created_at_wall_millis`.

---

## (b) Batch top-level fields

Emitted by `JsonCodec.batchToJson`; validated by `schemas.Batch` (`extra="allow"`).

| key | type | Optional/null rules |
|---|---|---|
| `batch_id` | str | required; must be UUID |
| `device_id` | str | required; 64-hex; must equal envelope |
| `session_id` | str | required, `min_length=1` |
| `record_type` | str | **`Literal["collection"]`** (const `"collection"`) |
| `collection_source` | str | **`Literal["BUILTIN_TASK","THIRD_PARTY_APP"]`** (`CollectionSource.name`) |
| `app_package_name` | str | required (foreground pkg; falls back to `"unknown"`) |
| `foreground_activity_class_name` | str \| None | optional |
| `foreground_component_name` | str \| None | optional |
| `sampling_rate_hz` | int | `> 0` (`SamplingConfig.SAMPLING_RATE_HZ`) |
| `batch_duration_seconds` | int | `>= 0` (`(ended-started)/1000`, floored, non-neg) |
| `task_sequence` | int \| None | `TaskCategory.ordinal` (0..6) |
| `task_id` | str \| None | `TaskCategory.name` (C0..C6) |
| `task_name` | str \| None | `taskNameEn` |
| `task_intuitive_description` | str \| None | `intuitiveDescriptionEn` |
| `task_category` | str \| None | `TaskCategory.name` (C0..C6) |
| `task_session_id` | str \| None | |
| `task_started_at_wall_millis` | int \| None | |
| `task_elapsed_seconds_at_batch_end` | int \| None | |
| `app_version` | str | `BuildConfig.VERSION_NAME` |
| `rule_version` | str | effective rule version |
| `rule_hash` | str | effective rule hash |
| `consent_version` | str | const `"1"` |
| `started_at_wall_millis` | int | `>= 0` |
| `ended_at_wall_millis` | int | `>= 0`; `started <= ended` enforced |
| `base_elapsed_nanos` | int | `>= 0` (elapsedRealtimeNanos base for sensor `timestamp_elapsed_nanos`) |
| `sensor_samples` | list | see (c) |
| `touch_events` | list | see (d) |
| `context_events` | list | see (e) |
| `context_features` | list | see (f) |
| `skip_events` | list[dict] | passthrough `list<Map>` |
| `diagnostics` | object | see (g) |

**Task-label contract (`validate_task_label_contract`):**
- If `collection_source == "BUILTIN_TASK"`: all 8 `task_*` fields (`task_id, task_sequence, task_name, task_intuitive_description, task_category, task_session_id, task_started_at_wall_millis, task_elapsed_seconds_at_batch_end`) must be non-null; `task_category ∈ C0..C6`; `task_id == task_category`; `task_sequence == int(task_id[1:])`.
- If `THIRD_PARTY_APP`: **all** those `task_*` fields must be null.
- Every `context_features[*]` must match batch on `collection_source, task_category, task_id, task_sequence, task_name, task_intuitive_description, task_session_id`, and each `feature.event_id` must exist in `context_events`.
- Diagnostics counts must equal actual list lengths; `diagnostics.sampling_rate_hz` (if present) must equal top-level `sampling_rate_hz`.

`TASK_CATEGORIES = {C0,C1,C2,C3,C4,C5,C6}` (both files). Ordinals: C0=0 … C6=6.
EN labels: C0 Quiescent viewing/Hold and read; C1 Keyboard text entry/Paragraph copy; C2 Continuous scrolling/Feed browsing; C3 Discrete navigation/Menu navigation; C4 Multi-control operation/Simulated phone settings; C5 Media playback/Local video playback; C6 Canvas high motion/Wrist rotation.

> **[现状勘注 2026-07-04]** 上面两行是历史（`C0..C6`）契约原文。当前正典为 **7 类 `I0..I6`**，`app/schemas.py` 以 `TASK_CATEGORIES = CANONICAL_TASK_CATEGORIES{I0..I6} ∪ LEGACY_TASK_CATEGORIES{I7, C0..C6}` 并集校验；旧 `I7` 手腕转动 → 新 `I6`，旧空间采集 `I6`（Scan/frame/capture）已删除，`C0..C6` 仅作 legacy 兼容标识保留。逐条映射与 EN 正典文案见 `docs/ContextAuthServer_服务端说明.md` §2.1–§2.3。

---

## (c) sensor_samples[] (`sensorJson` / `schemas.SensorSample`, `extra="forbid"`)

| key | type | notes |
|---|---|---|
| `sensor_type` | str | **`Literal["ACCELEROMETER","GYROSCOPE","MAGNETIC_FIELD"]`** |
| `timestamp_elapsed_nanos` | int | `>= 0` (relative to `base_elapsed_nanos`) |
| `wall_time_estimated_millis` | int | `>= 0` |
| `x` | float | serialized `Float.toDouble()` |
| `y` | float | |
| `z` | float | |
| `accuracy` | int \| None | optional on server; app always sends int |

---

## (d) touch_events[] (`touchJson` / `schemas.TouchEvent`, `extra="forbid"`)

| key | type | notes |
|---|---|---|
| `event_id` | str | UUID |
| `event_type` | str | `Literal[TOUCH_INTERACTION_START, TOUCH_INTERACTION_END, TOUCH_DOWN, TOUCH_UP, TOUCH_POINTER_DOWN, TOUCH_POINTER_UP, TOUCH_CANCEL]` |
| `event_time_uptime_millis` | int | `>= 0` |
| `event_time_wall_millis` | int | `>= 0` |
| `collected_at_wall_millis` | int | `>= 0` |

Touch events carry **no coordinates** — timing/type only.

---

## (e) context_events[] + NodeSnapshot

`eventJson` / `schemas.ContextEvent` (`extra="allow"`):

| key | type | notes |
|---|---|---|
| `event_id` | str | |
| `event_type` | str | |
| `event_time_wall_millis` | int | `>= 0` |
| `app_package_name` | str \| None | |
| `foreground_activity_class_name` | str \| None | |
| `foreground_component_name` | str \| None | |
| `input_method_visible` | bool | default False |
| `coarse_orientation` | str | `Literal[portrait, landscape, portrait_reverse, landscape_reverse, unknown]` (**LEAKAGE**, see (f)) |
| `window_title_redacted` | **null** | text field — app hard-codes `null`; server `str \| None` |
| `root_nodes` | list[NodeSnapshot] | |
| `redaction_summary` | dict[str,int] | |

**NodeSnapshot** (`nodeJson` / `schemas.NodeSnapshot`, `extra="allow"`):

| key | type | notes |
|---|---|---|
| `node_id` | str | |
| `class_name` | str \| None | |
| `viewIdResourceName` | str \| None | **LEAKAGE** (camelCase kept intentionally) |
| `bounds_grid` | object | `{left,top,right,bottom}` ints (`schemas.BoundsGrid`, `extra="forbid"`); server `bounds_grid: BoundsGrid \| None` |
| `clickable` | bool | |
| `editable` | bool | |
| `scrollable` | bool | |
| `checkable` | bool | |
| `checked` | bool | |
| `enabled` | bool | default True |
| `focused` | bool | |
| `selected` | bool | |
| `visible_to_user` | bool | default True |
| `long_clickable` | bool | |
| `password` | bool | **server rejects any node with `password == true`** (`password_node_must_be_dropped`) |
| `input_type_category` | str \| None | app: `"text"` if editable else `null` |
| `child_count` | int | |
| `has_text` | bool | **presence-only**; app-only key (server `extra="allow"`) |
| `has_content_description` | bool | **presence-only**; app-only key |
| `text` | **null** | always null (text dropped on-device); server rejects non-null editable text |
| `text_redacted` | **null** | always null |
| `content_desc_redacted` | **null** | always null |
| `actions_summary` | list[str] | |
| `depth` | int | |

Privacy invariant: text/content is **never** transmitted — only booleans `has_text` / `has_content_description` and the three constant-`null` text keys survive.

---

## (f) context_features[] + LEAKAGE columns

`featureJson` / `schemas.ContextFeature` (`extra="allow"`):

| key | type | notes |
|---|---|---|
| `feature_id` | str | |
| `event_id` | str | must reference a `context_events[*].event_id` |
| `computed_at_wall_millis` | int | `>= 0` |
| `collection_source` | str | `Literal["BUILTIN_TASK","THIRD_PARTY_APP"]`; must match batch |
| `task_sequence` | int \| None | must match batch |
| `task_id` | str \| None | must match batch |
| `task_name` | str \| None | must match batch |
| `task_intuitive_description` | str \| None | must match batch |
| `task_category` | str \| None | must be in C0..C6 if set; must match batch |
| `task_session_id` | str \| None | must match batch |
| `input_method_visible` | bool | |
| `keyboard_visible_estimated` | bool \| None | app: `input_method_visible OR editable_count>0` |
| `editable_count` | int | |
| `scrollable_count` | int | |
| `clickable_count` | int | |
| `password_node_seen` | bool | |
| `media_like_score` | float | |
| `list_like_score` | float | |
| `form_like_score` | float | |
| `game_like_score` | float | **LEAKAGE** |
| `node_class_histogram` | dict[str,int] | |
| `event_type` | str \| None | |
| `coarse_orientation` | str \| None | `Literal[portrait, landscape, portrait_reverse, landscape_reverse, unknown]` — **LEAKAGE** |
| `estimated_context_category` | str | default `"UNKNOWN"` — **LEAKAGE** (server-side derived label) |

**LEAKAGE COLUMNS (must be excluded/quarantined for leakage-free experiments):**
1. `estimated_context_category` (context_features)
2. `game_like_score` (context_features)
3. `viewIdResourceName` (NodeSnapshot, in context_events.root_nodes)
4. `coarse_orientation` (present on **both** context_events and context_features)

---

## (g) diagnostics (`BatchDiagnostics`, `extra="allow"`)

Emitted by `batchToJson` "diagnostics" map:

| key | type | notes |
|---|---|---|
| `sensor_sample_count` | int | must equal `len(sensor_samples)` |
| `context_event_count` | int | must equal `len(context_events)` |
| `touch_event_count` | int | must equal `len(touch_events)` |
| `sampling_rate_hz` | int \| None | if present, must equal top-level `sampling_rate_hz` |
| `redaction_applied` | bool | **`Literal[True]`** (app hard-codes `true`) |
| `compression` | str | **`Literal["lz4_frame"]`** |
| `encryption` | str | **`Literal["none"]`** |
| `gated_resume` | bool | app-only extra key (server `extra="allow"`) |

---

## (h) On-disk storage + index formats (`storage.py`)

Data root `SETTINGS.data_dir`; subdirs: `devices/`, `index/`, `quarantine/`.
Date dir = `YYYY-MM-DD` (UTC/`gmtime`) from `batch.started_at_wall_millis`.

**Stored batch (accepted):**
- Batch JSON: `devices/{device_id}/{date}/{batch_id}.json`
  - content = `json.dumps(plaintext, ensure_ascii=False, sort_keys=True)` (the full decompressed batch object).
  - Idempotent: same `batch_id` + identical bytes → reused; different bytes → `DuplicateBatchConflict`.
- Meta: `devices/{device_id}/{date}/{batch_id}.meta.json`:
  `{request_id, ingested_at_wall_millis, envelope (model_dump excluding payload_base64), compressed_payload_omitted:true, compressed_size_bytes, decompressed_size_bytes, schema_validation_result:"ok", batch_file}`.
- **by_category symlink** (only `BUILTIN_TASK` + non-null `task_category`):
  `devices/{device_id}/by_category/{task_category}/{date}/{batch_id}.json` → relative symlink to the real batch file. On filesystems w/o symlink support, falls back to a pointer JSON `{"target": "<abs batch path>"}`.

**Index JSONL** (`index/*.jsonl`, each line `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",",":"))`):
- `index/devices.jsonl`: `{ts, device_id, device_id_prefix}` (prefix = `device_id[:8]`).
- `index/batches.jsonl`: `{ts, device_id, device_id_prefix, batch_id, collection_source, task_category, path}`.
- `index/errors.jsonl`: `{ts, request_id, reason, device_id_prefix, batch_id, details}`.

**Quarantine** (schema/mismatch failures): `quarantine/{device_id}/{date}/{batch_id}.json`
  content = `{"reason", "payload_summary"}` where `payload_summary` = `{payload_sha256, payload_type:"json", top_level_keys:<sorted keys[:50]>}` (or bytes/unavailable variants). No raw payload retained; an `errors.jsonl` row is also appended with `quarantine_path`.

Note: `device_id` is a 64-hex salted hash (salt `Continuous_Authentication`); the only user identifier persisted is this hash + its 8-char prefix. No plaintext PII, no encryption.
