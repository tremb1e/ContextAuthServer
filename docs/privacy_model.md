# Privacy Model

## Collected Fields

- Motion sensors: sensor type, elapsed timestamp, server-offset wall time estimate, x/y/z values, and accuracy.
- Touch timing: global screen touch interaction start/end timestamps (`uptime` and wall-clock estimate) while collection is active. Touch coordinates, paths, pressure, and contact size are not collected.
- UI context: Accessibility event type, plaintext foreground `app_package_name`, foreground Activity/ComponentName, input-method visibility, coarse orientation, structure-only node fields (no text), `viewIdResourceName`, the boolean presence flags `has_text`/`has_content_description`, and derived context features. These come from two paths that share the same structural sanitizer: reactive Accessibility events, and a proactive foreground snapshot (`FOREGROUND_SNAPSHOT`) taken at each batch flush from the current foreground window. The snapshot exposes no additional raw data; it walks the same text-free node tree under the same depth/node limits.
- Diagnostics: sample counts, redaction status, compression type, queue/upload metadata, and fixed baseline rule lineage constants.

## Not Collected

- IMEI, serial, MAC, MediaDrm ID, or other non-resettable hardware identifiers.
- Screenshots, screen recording, raw keystrokes, automatic input, automatic clicks, gestures, or remote control actions.
- Touch trajectories, touch coordinates, pressure, contact size, or pointer paths.
- Password nodes or their descendants.
- Any displayed or entered text content: node text, input-field text, content-descriptions, and window/event titles are all dropped on-device. Only the content-free `has_text`/`has_content_description` presence flags survive.
- Input method dynamics such as per-key timestamps, key intervals, key hold duration, or per-character text-change events.

## Device ID And Sessions

Android computes:

```text
device_id = lowercase_hex(HMAC-SHA256(
  key = serverStudySalt,
  message = Settings.Secure.ANDROID_ID
))
```

The default study salt is `Continuous_Authentication`. Hardware identifiers are excluded because they are not resettable by the participant and would create unnecessary re-identification risk.

Every uploaded batch also carries a non-empty `session_id`. For built-in tasks, `session_id` equals `task_session_id`; for foreground app collection, it is a collection-session UUID shared by batches from the same continuous collection period.

## Text Handling

Dropping all text on-device is the primary protection. The app never collects or
uploads displayed or entered text: node text, input-field content,
content-descriptions, and window/event titles are all discarded at the source.
Node text and content-description are read only to set the boolean
`has_text`/`has_content_description` presence flags and are then thrown away; the
serialized `text`, `text_redacted`, `content_desc_redacted`, and
`window_title_redacted` keys are always `null`. Password nodes and their subtrees
are dropped entirely. There is no regex/placeholder redaction, no retained
"non-editable component text", and no `<EMAIL>`/`<PHONE>`/`<TEXT_REDACTED>`-style
output anymore. Foreground app package names, Activity/ComponentName, and
`viewIdResourceName` are uploaded in plaintext by design (they are
compile-time/system identifiers, not user content). See `docs/redaction_rules.md`
for the authoritative model.

The cloud "desensitization rules" / rule-update mechanism is removed: the app no
longer fetches `/api/v1/rules`, there is no `RuleUpdateClient` or
`RedactionPolicy`, and no in-app "check rules" UI or rule version/hash display.
`rule_version` (`"1"`) and `rule_hash` (64 zeros) are emitted as fixed baseline
constants only because the unmodified server schema requires those fields.

The proactive foreground snapshot does not change this posture. It runs the
identical text-free node walk as reactive events: password nodes and their
subtrees are dropped and no text is read for serialization. The only field note
is that `window_title_redacted` is `null` on every path. When the Accessibility
service is disconnected, no snapshot is taken at all.

The Accessibility service catches framework/OEM event exceptions and only
traverses UI windows while collection is active, reducing the chance of system
service faults without changing the collected field set.

The server no longer performs the former secondary sensitive-string/raw-UI scan.
It still validates the envelope, compressed payload hash, LZ4/JSON shape,
Pydantic schema, task contract, the now-trivial editable-text/`text`-is-null
invariant, password-node absence, `diagnostics.redaction_applied: true`, matching
diagnostics counts, and context-feature references to same-batch context events.

## Storage

Server stores data under `data/paper/devices/{device_id}/`. `device_id` is regex validated before use in paths. Path traversal is rejected by validation and safe path resolution.
