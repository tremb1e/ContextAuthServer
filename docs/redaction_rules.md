# Text Handling (Drop-All-Text Model)

This is the authoritative privacy/text-handling document. It supersedes the
earlier "redaction with placeholders" / cloud "desensitization rules" model
(placeholders such as `<EMAIL>`/`<PHONE>`/`<URL>` and a `/api/v1/rules`
rule-update engine). None of that exists anymore.

## Core rule: all displayed/entered text is dropped on-device

The app never collects or uploads any displayed or entered **text** content.
There is no on-device or server-side regex/placeholder redaction, and there is
no "check rules" UI, no rule version/hash display, and no cloud rule fetch. The
app drops text at the source and keeps only structure/metadata.

Specifically, the on-device `RedactionEngine` (now a structural sanitizer, not a
text redactor):

- **Password node:** dropped entirely and never serialized; its whole subtree is
  skipped.
- **Node text and content-description:** read *only* to compute boolean presence
  flags (`has_text`, `has_content_description`), then discarded. The raw
  characters are never stored on the resulting node and never serialized. This
  applies identically to editable and non-editable nodes; there is no
  "retain non-editable visible text" path anymore.
- **Window/event titles:** treated as text and permanently dropped on-device.

As a result, the following serialized keys are **always `null`** (the keys are
retained only for stored-schema/server compatibility; the server models them as
optional `str | None`):

- node-level `text`, `text_redacted`, `content_desc_redacted`
- context-event-level `window_title_redacted`

## Presence flags (new)

To preserve a coarse, content-free signal that text was present, each serialized
node carries two booleans:

- `has_text` — the node had non-blank text.
- `has_content_description` — the node had non-blank content-description.

These are presence-only; they carry no characters, length, or category of the
underlying text.

## `redaction_summary` counters

Each context event still carries a `redaction_summary`, but it now contains only
drop counters (no `replaced_email`/`replaced_phone`/`replaced_*` pattern-hit
counters, which are gone):

- `dropped_password_nodes`
- `dropped_editable_texts`
- `dropped_text_nodes`
- `dropped_content_descriptions`
- `dropped_window_titles`

These are aggregate counts only and contain no content.

## What is still collected per node (structure/metadata, zero text)

- `node_id`, `class_name` (component type), `viewIdResourceName` (the
  developer-assigned resource id — compile-time, never user data), `bounds_grid`
  (coarse position), `depth`, `child_count`.
- State flags: `clickable`, `editable`, `scrollable`, `checkable`, `checked`,
  `enabled`, `focused`, `selected`, `visible_to_user`, `long_clickable`,
  `password`.
- `input_type_category` (`"text"` when the node is editable, otherwise `null`),
  `actions_summary`.
- The two presence flags `has_text` / `has_content_description` described above.

`viewIdResourceName` is uploaded in plaintext by design: it is a compile-time
developer identifier, not user-entered or displayed data. Foreground app package
name and Activity/ComponentName are likewise plaintext by design.

## Cloud rule-update mechanism removed

- The app no longer fetches `/api/v1/rules`. There is no `RuleUpdateClient`, no
  `RedactionPolicy`/`RedactionPatternRule`, no `max_text_length`, and no
  `default_text_action` on the client.
- Package-level skip/blocklist behavior was already removed; the app no longer
  drops whole apps by package name. (Because all text is dropped regardless,
  per-package text policy would be moot anyway.)
- `rule_version` (`"1"`) and `rule_hash` (64 zeros) are still emitted in the
  upload payload and envelope, but only as fixed baseline constants required by
  the unmodified server schema. They are not derived from any active rule set and
  do not represent a text-redaction policy.

## Server side

The server is unchanged by this work. It still validates the envelope, the
compressed-payload hash, the LZ4/JSON shape, and the Pydantic schema. The
schema-level privacy invariants it enforces continue to hold trivially under the
drop-all-text model:

- editable nodes must not contain raw `text` (now always `null`),
- password nodes must be absent,
- `diagnostics.redaction_applied: true` must be present,
- diagnostics counts must match the payload arrays,
- context features must reference context events in the same batch.

The server's `GET /api/v1/rules` endpoint still exists and still serves a rule
payload from `SERVER_RULES_FILE` (the server was not modified), but no app
component consumes it. It has no effect on what the app collects.
