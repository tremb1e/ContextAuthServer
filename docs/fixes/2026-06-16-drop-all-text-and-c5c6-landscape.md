# Fix: drop all text on-device, remove desensitization rules, and fix C5/C6 landscape relayout (2026-06-16)

This note covers three related changes that landed together. The server, the
wire format envelope, the JSON schema field set, and the 48-test unit suite are
all green; server payload validation passes.

## 1. Drop all displayed/entered text on-device (privacy change)

### Before

The app collected accessible UI node text and applied a "redaction with
placeholders" model: a regex engine replaced fixed-format sensitive substrings
(emails, phones, URLs, cards, ID numbers, tokens, long numbers) with
`<EMAIL>`/`<PHONE>`/`<URL>`/`<CARD>`/`<ID_NUM>`/`<TOKEN>`/`<NUM>` and then
**retained** the remaining non-editable visible component text (button labels,
list text, etc.). Editable text was dropped as `<EDITABLE_TEXT_DROPPED>`;
content-descriptions and window titles were folded to `<TEXT_REDACTED>`. The
asymmetric "retain visible text" path meant free-form user text (comments, user
names, coarse geolocation, emoji) could pass through the node `text` channel.

### After

All displayed and entered **text** content is now dropped on-device. The app no
longer collects or uploads any node text, input-field content,
content-descriptions, or window/event titles. The `RedactionEngine` is now a
structural sanitizer, not a text redactor:

- Node `text` and content-description are read **only** to compute two boolean
  presence flags, then discarded. The raw characters are never stored on the
  resulting node and never serialized.
- Password nodes are dropped entirely and their subtree is skipped (unchanged).
- Window/event titles are treated as text and dropped.

The following serialized keys are now **always `null`** (the keys are retained
only for stored-schema/server compatibility; the server models them as optional
`str | None`):

- node-level `text`, `text_redacted`, `content_desc_redacted`
- context-event-level `window_title_redacted`

### New presence flags

Each serialized node now carries two content-free booleans:

- `has_text` — the node had non-blank text.
- `has_content_description` — the node had non-blank content-description.

These preserve a coarse "text was present" signal without carrying any
characters, length, or category.

### `redaction_summary` counters

The per-event `redaction_summary` now contains only drop counters; the old
`replaced_email`/`replaced_phone`/`replaced_*` and `redacted_plain_text`/
`dynamic_*` pattern-hit counters are gone:

- `dropped_password_nodes`
- `dropped_editable_texts`
- `dropped_text_nodes`
- `dropped_content_descriptions`
- `dropped_window_titles`

### Still collected per node (structure/metadata only, zero text)

`node_id`, `class_name`, `viewIdResourceName` (a compile-time developer-assigned
resource id — never user data), `bounds_grid`, `depth`, `child_count`; the state
flags `clickable`, `editable`, `scrollable`, `checkable`, `checked`, `enabled`,
`focused`, `selected`, `visible_to_user`, `long_clickable`, `password`;
`input_type_category` (`"text"` when editable, else `null`); `actions_summary`;
and the new `has_text` / `has_content_description` presence flags. Foreground app
package name, Activity/ComponentName, and `viewIdResourceName` are still uploaded
in plaintext by design.

`docs/redaction_rules.md` is the authoritative text-handling document for this
model; `docs/data_schema.md` and `docs/privacy_model.md` describe the field-level
effects.

## 2. Remove the desensitization-rules engine and cloud rule update

The cloud "desensitization rules" / rule-update mechanism is removed from the
app:

- The app no longer fetches `/api/v1/rules`. There is no `RuleUpdateClient`, no
  `RedactionPolicy`/`RedactionPatternRule`, no `max_text_length`, and no
  `default_text_action` on the client.
- There is no in-app "检查规则 / Check Rules" UI and no rule version/hash display.
- Package-level skip/blocklist behavior was already removed; with all text
  dropped, per-package text policy is moot anyway.

`rule_version` (`"1"`) and `rule_hash` (64 zeros) are still emitted in the upload
payload and envelope, but only as fixed baseline constants required by the
unmodified server schema. They do not represent any text-redaction policy.

The server is untouched. Its `GET /api/v1/rules` endpoint still exists and still
serves a rule payload from `SERVER_RULES_FILE`, but no app component consumes it;
it has no effect on what the app collects.

## 3. Fix C5/C6 fullscreen-landscape stale-bounds relayout

### Symptom

In the C5 (fullscreen landscape blue-ball tapping) and C6 (fullscreen landscape
local video) tasks, the fullscreen landscape `Dialog` kept stale **portrait**
bounds after entering the task: the surface was laid out at portrait dimensions
and only snapped to the correct fullscreen landscape size once the user touched
near the × (close) control, which forced a relayout.

### Fix

The dialog window now re-asserts its fullscreen layout on **every configuration
change**, so the fullscreen landscape bounds are applied immediately on entry and
on any orientation/configuration change instead of lazily on first touch.

## Scope limits

This change does not add multi-user identity labels, genuine/impostor labels,
MoE routing, expert models, or any authentication training/inference code. Those
remain unimplemented; the analysis docs under `doc/` describe that future scope.
Sensor sampling, touch interaction start/end timing, the C0–C7 task categories,
`context_features` (counts/scores/histogram/orientation/estimated category),
LZ4+JSON compression, and HTTPS upload are unchanged.

## Note on the existing analysis dataset

The `data/testdata/2026-06-11` measurements in the analysis docs (for example
"10,651 nodes (38.5%) retained `text`") were collected by the previous
text-retaining build. Under this change `text` is always `null` and only the
`has_text` / `has_content_description` presence flags survive, so the earlier
`text`-channel UGC-leak concern (previously flagged P0) no longer applies. The
analysis docs have been reframed accordingly.
