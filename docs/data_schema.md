# Data Schema

## Config Response

`GET /api/v1/config` keeps `serverTimeMillis` as the stable top-level HTTP
midpoint clock-sync value. `timeSync` is advisory metadata for clients that want
to display or use regional NTP fallbacks.

```json
{
  "serverStudySalt": "Continuous_Authentication",
  "rulesVersion": "1",
  "serverTimeMillis": 1710000000000,
  "timeSync": {
    "method": "HTTP_MIDPOINT",
    "region": "CN",
    "serverTimeField": "serverTimeMillis",
    "recommendedNtpServers": [
      "ntp.aliyun.com",
      "ntp.tencent.com",
      "0.cn.pool.ntp.org",
      "1.cn.pool.ntp.org",
      "2.cn.pool.ntp.org",
      "3.cn.pool.ntp.org"
    ],
    "maxAcceptableRttMillis": 3000
  }
}
```

## Payload Envelope

```json
{
  "algorithm": "LZ4_FRAME+JSON",
  "payload_base64": "<base64 LZ4 frame bytes>",
  "payload_sha256_hex": "<sha256 compressed bytes>",
  "device_id": "64-char-lowercase-hex",
  "batch_id": "uuid",
  "rule_version": "1",
  "rule_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "created_at_wall_millis": 1710000000000
}
```

`rule_version` is the fixed constant `"1"` and `rule_hash` is 64 zeros. They are
emitted only because the unmodified server schema requires these fields; they are
baseline constants, not a text-redaction policy. The app no longer fetches or
applies any cloud "desensitization rules" (see `docs/redaction_rules.md`).

## Batch

The decompressed payload is UTF-8 JSON:

```json
{
  "batch_id": "uuid",
  "device_id": "64-char-lowercase-hex",
  "session_id": "non-empty collection-or-task-session-id",
  "record_type": "collection",
  "collection_source": "BUILTIN_TASK",
  "app_package_name": "com.example.target",
  "foreground_activity_class_name": "com.example.target.MainActivity",
  "foreground_component_name": "com.example.target/.MainActivity",
  "sampling_rate_hz": 100,
  "batch_duration_seconds": 5,
  "task_sequence": 4,
  "task_id": "C4",
  "task_name": "Simulated phone settings",
  "task_intuitive_description": "Multi-control operation",
  "task_category": "C4",
  "task_session_id": "uuid",
  "task_started_at_wall_millis": 1710000000000,
  "task_elapsed_seconds_at_batch_end": 5,
  "app_version": "1.0.0",
  "rule_version": "1",
  "rule_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "consent_version": "1",
  "started_at_wall_millis": 1710000000000,
  "ended_at_wall_millis": 1710000005000,
  "base_elapsed_nanos": 123456789,
  "sensor_samples": [],
  "touch_events": [],
  "context_events": [],
  "context_features": [],
  "skip_events": [],
  "diagnostics": {
    "sensor_sample_count": 0,
    "context_event_count": 0,
    "touch_event_count": 0,
    "redaction_applied": true,
    "compression": "lz4_frame",
    "encryption": "none",
    "sampling_rate_hz": 100
  }
}
```

`session_id` is always populated by the Android app. For built-in tasks it equals `task_session_id`; for foreground app collection it is a collection session UUID so batches from the same continuous collection period can be grouped.

`BUILTIN_TASK` requires non-null `task_sequence`, `task_id`, `task_name`, `task_intuitive_description`, `task_category`, `task_session_id`, `task_started_at_wall_millis`, and `task_elapsed_seconds_at_batch_end`. `task_id` and `task_category` use `C0` through `C7`; `task_sequence` is the numeric part. Current task labels are stable English research labels in the payload, while the app UI localizes labels to Chinese or English according to system language. `THIRD_PARTY_APP` requires task-specific fields other than `session_id` to be null.

Server validation also checks that diagnostic sample/event counts match the actual arrays, that diagnostics `sampling_rate_hz` matches the batch sampling rate when present, and that each context feature uses the same source/task metadata as the enclosing batch. A context feature `event_id` must reference an event in the same batch's `context_events`.

## Sensor Sample

```json
{
  "sensor_type": "MAGNETIC_FIELD",
  "timestamp_elapsed_nanos": 123456789,
  "wall_time_estimated_millis": 1710000000000,
  "x": 0.0,
  "y": 0.0,
  "z": 0.0,
  "accuracy": 3
}
```

## Touch Event

Touch events are emitted by the AccessibilityService for global screen touch interactions while collection is active. The service returns before UI-window traversal when collection is not active, which avoids unnecessary Accessibility workload while preserving the enabled-service state. Touch events contain detailed timing and intentionally omit coordinates, trajectories, pressure, and contact size.

```json
{
  "event_id": "uuid",
  "event_type": "TOUCH_INTERACTION_START",
  "event_time_uptime_millis": 123456789,
  "event_time_wall_millis": 1710000000123,
  "collected_at_wall_millis": 1710000000124
}
```

Current global `event_type` values are `TOUCH_INTERACTION_START` and `TOUCH_INTERACTION_END`. The server also accepts legacy in-app timing values (`TOUCH_DOWN`, `TOUCH_UP`, `TOUCH_POINTER_DOWN`, `TOUCH_POINTER_UP`, and `TOUCH_CANCEL`) for previously generated payloads.

## Context Event And Node Snapshot

Context events contain event metadata, foreground context, input-method visibility, coarse orientation, a drop-counter summary, and `root_nodes`. Node snapshots contain structure/metadata only (zero text content): class, `viewIdResourceName`, `bounds_grid`, booleans such as clickable/long-clickable/editable/scrollable/visible/enabled/focused/selected/checkable/checked/password, `child_count`, `input_type_category`, `actions_summary`, `depth`, and the boolean presence flags `has_text`/`has_content_description`. The `text`, `text_redacted`, and `content_desc_redacted` keys are retained for stored-schema/server stability but are always `null`. The old `package_name_hash` and `view_id_hash` fields are not emitted.

```json
{
  "event_id": "uuid",
  "event_type": "TYPE_WINDOW_CONTENT_CHANGED",
  "event_time_wall_millis": 1710000000123,
  "app_package_name": "com.example.target",
  "foreground_activity_class_name": "com.example.target.MainActivity",
  "foreground_component_name": "com.example.target/.MainActivity",
  "input_method_visible": false,
  "coarse_orientation": "portrait",
  "window_title_redacted": null,
  "root_nodes": [
    {
      "node_id": "node-1",
      "class_name": "android.widget.Button",
      "viewIdResourceName": "com.example.target:id/confirm",
      "bounds_grid": {"left": 0, "top": 7, "right": 4, "bottom": 8},
      "clickable": true,
      "editable": false,
      "scrollable": false,
      "checkable": false,
      "checked": false,
      "enabled": true,
      "focused": false,
      "selected": false,
      "visible_to_user": true,
      "long_clickable": false,
      "password": false,
      "input_type_category": null,
      "child_count": 0,
      "has_text": true,
      "has_content_description": false,
      "text": null,
      "text_redacted": null,
      "content_desc_redacted": null,
      "actions_summary": ["CLICK"],
      "depth": 0
    }
  ],
  "redaction_summary": {
    "dropped_password_nodes": 0,
    "dropped_editable_texts": 0,
    "dropped_text_nodes": 1,
    "dropped_content_descriptions": 0,
    "dropped_window_titles": 0
  }
}
```

`event_type` carries either a reactive Accessibility event name (for example `TYPE_WINDOW_CONTENT_CHANGED`) or the value `FOREGROUND_SNAPSHOT`. `FOREGROUND_SNAPSHOT` events are produced by a proactive foreground snapshot that the collector pulls from the current foreground window at each batch flush; one such event is merged into every batch while the Accessibility service is connected, so each batch carries the foreground app package name plus its structure-only UI nodes even when no reactive Accessibility events fired during the window. The snapshot is built from the same structural node walk as reactive events, and `window_title_redacted` is `null` on every path (titles are text and are dropped on-device). When the Accessibility service is disconnected, no snapshot is emitted.

`app_package_name` is the plaintext foreground app package name being collected, not the ContextAuthLab package. Because the proactive snapshot supplies the foreground package on every batch while the service is connected, the top-level batch `app_package_name` is reliably the real foreground package and is not `"unknown"`; it falls back to `"unknown"` only when no event in the batch ever resolved a foreground window. `foreground_activity_class_name` and `foreground_component_name` are best-effort values derived from Accessibility window-state events and active application windows. `input_method_visible` is true when Accessibility reports an input-method window. `coarse_orientation` is captured when the Accessibility event or snapshot is processed and may be `portrait`, `landscape`, `portrait_reverse`, `landscape_reverse`, or `unknown`; it is also copied into derived context features. The app does not emit per-character text-change events, per-key timestamps, key intervals, key hold durations, touch coordinates, or touch trajectories.

All displayed/entered text is dropped on-device. Per node, `text`, `text_redacted`, and `content_desc_redacted` are always `null`; node text and content-description are read only to set the boolean `has_text`/`has_content_description` presence flags and are then discarded. Password nodes are omitted entirely (their subtree is skipped). There is no regex/placeholder redaction and no retained component text; `redaction_summary` reports only drop counters (`dropped_password_nodes`, `dropped_editable_texts`, `dropped_text_nodes`, `dropped_content_descriptions`, `dropped_window_titles`). See `docs/redaction_rules.md` for the authoritative text-handling model.

Server ingest does not perform any secondary raw-field/sensitive-text scan. Pydantic schema validation remains active and is satisfied trivially under the drop-all-text model: `viewIdResourceName` is allowed, the now-always-null `text`/`text_redacted`/`content_desc_redacted` keys are accepted, password nodes must be absent, valid batches must include `diagnostics.redaction_applied: true`, diagnostics counts must match the payload arrays, and context features must reference context events in the same batch.

## Context Feature

Features include counts and heuristic scores such as `editable_count`, `scrollable_count`, `clickable_count`, `password_node_seen`, `media_like_score`, `form_like_score`, `game_like_score`, `node_class_histogram`, `input_method_visible`, backwards-compatible `keyboard_visible_estimated`, `coarse_orientation`, nominal task fields (`task_sequence`, `task_id`, `task_name`, `task_intuitive_description`, `task_category`), an `event_type` mirror (which may be `FOREGROUND_SNAPSHOT`), and independent `estimated_context_category`. One feature is derived per context event, including the proactive `FOREGROUND_SNAPSHOT` event, so `context_features` stays 1:1 with `context_events` and is non-empty whenever the snapshot is present.

## Server Rules Endpoint (not consumed by the app)

`GET /api/v1/rules` still exists on the unmodified server and returns a
schema-backed rule payload initialized from `SERVER_RULES_FILE`. **The app no
longer fetches or applies this endpoint**, and there is no longer any in-app
text redaction it could configure (all text is dropped on-device; see
`docs/redaction_rules.md`). The endpoint is documented here only for server
completeness; it has no effect on collected data.

`rule_version` and `rule_hash` in the upload payload/envelope are the fixed
constants `"1"` and 64 zeros, emitted to satisfy the unmodified server schema.
Ingest stores them as opaque lineage metadata and does not require them to match
the server's rule file.

The server-side payload shape is unchanged:

```json
{
  "version": "1",
  "updated_at": "2026-05-21T00:00:00Z",
  "rules": [{"id": "email", "target": "text", "action": "REDACT", "pattern": "...", "replacement": "<EMAIL>"}],
  "package_blocklist": [],
  "max_text_length": 128,
  "default_text_action": "REDACT",
  "rule_hash": "sha256-hex"
}
```
