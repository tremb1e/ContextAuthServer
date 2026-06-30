# Fix: foreground UI/context capture dropped on Android 11+ (2026-06-11)

## Symptom

On modern devices (Android 11+ / API 30+), every uploaded batch was sensor-only:

- `app_package_name` was `"unknown"`,
- `context_events` was empty (`context_event_count = 0`),
- `context_features` was empty,

even though the per-batch snapshot-merge and the process/app-scope collection
coordinator were already in place. This is the real reason the previously
collected dataset is sensor-only. (It supersedes the earlier "old build /
build != install" hypothesis in the analysis reports under `doc/`.)

## Root cause

`ResearchAccessibilityService.displayRotation()` read screen rotation through
`Context.getDisplay()` (the Kotlin `display` property) on the API >= 30 branch.
On a `Service` context, `Context.getDisplay()` throws
`UnsupportedOperationException` on Android 11+ ("Tried to obtain display from a
Context not associated with one.").

`displayRotation()` was reached from `currentCoarseOrientation()`, which is
called on both capture paths:

- the reactive `onAccessibilityEvent(...)` -> `handleAccessibilityEvent(...)`
  path, and
- the proactive `buildForegroundSnapshotInternal()` path that produces the
  per-batch `FOREGROUND_SNAPSHOT`.

Both call sites were wrapped in silent `runCatching { ... }`, so the exception
was swallowed and **every** reactive context event **and** every foreground
snapshot was discarded. The non-critical `coarse_orientation` field thereby
took down the entire UI/context channel on every API 30+ device.

## Fix

In `android-app/src/main/java/com/contextauth/accessibility/ResearchAccessibilityService.kt`:

1. `displayRotation()` now reads rotation via
   `DisplayManager.getDisplay(Display.DEFAULT_DISPLAY).rotation`. `DisplayManager`
   is valid from any `Context` (including a `Service`) on all supported API
   levels. The body is wrapped in `runCatching { ... }.getOrNull()` so it can
   never throw.
2. `currentCoarseOrientation()` is now resilient: on any failure it returns
   `CoarseOrientation.UNKNOWN` instead of propagating, so a non-critical field
   can never again abort capture.
3. Capture failures are no longer silently swallowed. `onAccessibilityEvent`,
   `buildForegroundSnapshot` (the snapshot builder), and per-node traversal now
   log a warning under tag `ResearchA11yService`.
4. `traverse()` per-node snapshot building is wrapped so a single misbehaving
   node logs and is skipped instead of aborting the whole subtree.

The server, the wire format, and the JSON schema are unchanged.

## Regression test

`android-app/src/test/java/com/contextauth/accessibility/ResearchAccessibilityServiceCaptureTest.kt`
(`@Config(sdk = [34])`, Robolectric) guards the behavior with three tests:

- `captureForegroundSnapshotReturnsNonNullWithValidOrientation` — the real
  connected service's `captureForegroundSnapshot()` returns non-null with a
  valid `coarseOrientation`.
- `orientationNeverThrows` — `currentCoarseOrientation()` always returns one of
  the well-defined orientation values.
- `realConnectedServiceMakesBuildBatchInjectForegroundSnapshot` — end-to-end: a
  real connected service wired into `CollectionCoordinator` (the default snapshot
  provider) makes `buildBatch(...)` inject exactly one `FOREGROUND_SNAPSHOT`
  context event with a matching `context_feature`, i.e. the
  `context_events`/`context_features` channels that were `0` on device are now
  populated.

Reproduction confirmed (red→green): Robolectric is pinned to `@Config(sdk = [34])`
(>= 30), which exercises the same throwing branch as a real API 30+ device.
Reverting only the `displayRotation()` fix makes `captureForegroundSnapshot()`
return null and `currentCoarseOrientation()` throw the same
`UnsupportedOperationException`, failing these tests; with the fix they pass. The
full `:android-app` unit suite is green (60 tests, 0 failures).

## How to verify on a real device

1. Install the debug APK and grant consent.
2. Enable the Accessibility service: Settings -> Accessibility -> ContextAuthLab
   -> enable, then return to the app. Also allow battery-optimization exemption
   and notification permission so automatic collection starts.
3. Use any app (the launcher, a browser, a chat app) for a few batch intervals.
4. Inspect a stored batch under
   `data/paper/devices/{device_id}/{date}/{batch_id}.json` (or the server logs)
   and confirm:
   - `app_package_name` is the real foreground package (not `"unknown"`),
   - `context_events` contains at least one entry, typically with
     `event_type = "FOREGROUND_SNAPSHOT"` and populated `root_nodes`,
   - `context_features` is non-empty and 1:1 with `context_events`, with an
     `estimated_context_category`.
5. Optionally watch logcat for tag `ResearchA11yService`; capture failures now
   surface as warnings instead of being silent.

A batch is still legitimately sensor-only only when the Accessibility service is
disconnected or not yet connected (no snapshot is produced in that case). No
batch-dropping rule was added; batches are never dropped for a missing
package/UI.

## Scope limits

This change restores UI/context **capture** only. It does **not** add:

- multi-user identity labels or genuine/impostor labels,
- MoE routing, expert models, or any authentication training/inference code.

Those remain unimplemented; the analysis docs under `doc/` describe that future
scope. This fix does not improve authentication accuracy (there is no
authentication model yet); it only ensures the MoE-routing research design's
required inputs (foreground package + redacted UI features) are actually
recorded per batch.
