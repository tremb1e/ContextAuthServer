# Storage Layout

```text
data/paper/
  server_study_salt.txt
  devices/
    {device_id}/
      2026-05-18/
        {batch_id}.json
        {batch_id}.meta.json
      by_category/
        {task_category}/
          2026-05-18/
            {batch_id}.json -> ../../../../2026-05-18/{batch_id}.json
  index/
    devices.jsonl
    batches.jsonl
    errors.jsonl
  quarantine/
    {device_id}/
      2026-05-18/
        {batch_id}.json
```

`{batch_id}.json` stores decompressed, text-free batch JSON. `{batch_id}.meta.json` stores envelope metadata without `payload_base64`, ingest time, sizes, and schema validation result.

If the same `device_id + batch_id` payload is replayed byte-for-byte, ingest is
idempotent and does not append duplicate indexes. A conflicting duplicate batch
ID is rejected without overwriting the stored batch. `by_category` entries are
relative symlinks when the filesystem supports symlinks, so moving the data
root keeps category indexes usable.

Quarantine files do not store rejected plaintext verbatim. They store `reason`, payload SHA-256, payload type, and top-level JSON keys so failure analysis cannot leak suspicious raw text through error paths.

Schema validation rejects mismatched diagnostics counts, context features whose
event IDs do not refer to `context_events` in the same batch, and feature
source/task metadata that diverges from the enclosing batch.

All device paths are guarded by the `^[a-f0-9]{64}$` device ID regex and safe path resolution.
