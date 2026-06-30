# Server API

## `GET /health`

Returns `{"status":"ok"}`.

## `GET /ready`

Checks that the server can create/use the data directory, index files, and
quarantine directory and that the minimum free-space threshold is satisfied.
Returns `{"status":"ready"}` or HTTP 503.

## `GET /api/v1/config`

Returns stable `serverStudySalt`, `rulesVersion`, and top-level `serverTimeMillis`.
`serverTimeMillis` remains the backwards-compatible HTTP midpoint clock-sync field.

The response also includes advisory clock-sync metadata. Defaults are China-region
NTP hosts and can be overridden with `TIME_SYNC_NTP_SERVERS`:

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

## `GET /api/v1/rules`

Returns the current rule payload and `rule_hash`. The server reads the payload
from `SERVER_RULES_FILE` at startup, creating that file from the packaged
`app/default_rules.json` when it is missing.

**The Android app no longer fetches or applies this endpoint.** All displayed and
entered text is dropped on-device (see `docs/redaction_rules.md`), so there is no
in-app text redaction for these rules to configure. The endpoint remains on the
unmodified server for completeness only; it has no effect on collected data. The
`rule_version`/`rule_hash` carried in upload payloads are the fixed constants
`"1"` and 64 zeros and are not derived from this endpoint.

Current server-side payload (unchanged):

```json
{
  "version": "1",
  "updated_at": "2026-05-21T00:00:00Z",
  "rules": [
    {"id": "email", "target": "text", "action": "REDACT", "pattern": "...", "replacement": "<EMAIL>"},
    {"id": "phone_cn", "target": "text", "action": "REDACT", "pattern": "...", "replacement": "<PHONE>"},
    {"id": "url", "target": "text", "action": "REDACT", "pattern": "...", "replacement": "<URL>"},
    {"id": "id_number_cn", "target": "text", "action": "REDACT", "pattern": "...", "replacement": "<ID_NUM>"},
    {"id": "payment_card", "target": "text", "action": "REDACT", "pattern": "...", "replacement": "<CARD>"},
    {"id": "opaque_token", "target": "text", "action": "REDACT", "pattern": "...", "replacement": "<TOKEN>"},
    {"id": "long_number", "target": "text", "action": "REDACT", "pattern": "...", "replacement": "<NUM>"}
  ],
  "package_blocklist": [],
  "max_text_length": 128,
  "default_text_action": "REDACT",
  "rule_hash": "sha256-hex"
}
```

## `POST /api/v1/ingest`

Accepts LZ4 frame JSON envelope. Valid requests are stored on disk and return `status`, `batch_id`, `stored`, and `device_id_prefix` without exposing the full device ID or server filesystem path. Replaying the exact same `device_id + batch_id` payload is idempotent; a conflicting duplicate `batch_id` returns `409 duplicate_batch_id_conflict` without overwriting the original batch. Payload SHA-256 mismatches, invalid algorithm, bad IDs, corrupt LZ4, schema failures, and task-label contract failures are rejected or quarantined. `rule_version` (`"1"`) and `rule_hash` (64 zeros) are fixed baseline constants emitted to satisfy the schema; they are stored as opaque lineage metadata and are not required to match the server's current rule file.

The server expects Accessibility-derived UI values to use the current schema: top-level and event-level plaintext `app_package_name`, optional `foreground_activity_class_name` and `foreground_component_name`, event-level `input_method_visible` and `coarse_orientation`, node-level `viewIdResourceName`, structure/state booleans, and the boolean `has_text`/`has_content_description` presence flags. All displayed/entered text is dropped on-device, so node `text`/`text_redacted`/`content_desc_redacted` and event `window_title_redacted` are always `null`. Touch events may include only event IDs, event type, uptime timestamp, wall-clock timestamp, and collection timestamp; coordinates, paths, pressure, and size are not schema fields. Current global touch event types are `TOUCH_INTERACTION_START` and `TOUCH_INTERACTION_END`; legacy in-app timing types remain accepted for older payloads. Raw input-field text must not be present; under the drop-all-text model `text` is null on every node, which trivially satisfies the schema's editable-text invariant. Password nodes must be dropped before upload. Batches with `diagnostics.redaction_applied` other than `true` fail schema validation. Diagnostics counts must match the payload arrays, and context features must reference context events in the same batch while carrying matching source/task metadata. The former secondary server scan for raw UI field names, prose in `*_redacted`, and obvious sensitive strings has been removed.

## `GET /metrics`

Prometheus exposition format. Does not include `device_id` or `batch_id`.
