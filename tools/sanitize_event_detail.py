#!/usr/bin/env python3
"""One-shot sanitizer for on-disk ``event_detail`` text telemetry (SRV-1-C, 2026-07-05).

Rewrites stored batch JSON in place so the corpus satisfies the ``event_detail``
privacy red-line enforced by :class:`app.schemas.ContextEvent` (SRV-1-B):

* every ``event_detail`` loses the six text-telemetry keys
  (``before_text_length``, ``text_total_length``, ``content_description_length``,
  ``text_entry_count``, ``added_count``, ``removed_count``);
* the three text events (``TYPE_VIEW_TEXT_CHANGED`` /
  ``TYPE_VIEW_TEXT_SELECTION_CHANGED`` /
  ``TYPE_VIEW_TEXT_TRAVERSED_AT_MOVEMENT_GRANULARITY``) additionally lose the four
  index keys (``from_index``, ``to_index``, ``item_count``, ``current_item_index``)
  and get ``event_time_wall_millis`` floored to the whole second — matching the
  v1.1.2 on-device behaviour and removing the per-keystroke timing side channel.

Deleted values are NOT backed up. Each rewritten batch is re-validated with the
new ``Batch.model_validate``. Each batch's ``.meta.json`` gains ``sanitized_at``
(UTC ISO) and ``sanitizer_version``; everything else in the meta, and the
``index/*.jsonl`` files, are left untouched. ``by_category`` symlinks resolve to
the canonical date-dir file, so each underlying batch is processed exactly once.

Run ``--dry-run`` first to print the statistics, then run for real.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Repo root on sys.path so ``app.schemas`` imports without an install step.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.schemas import (  # noqa: E402  (import after sys.path shim)
    EVENT_DETAIL_FORBIDDEN_TELEMETRY_KEYS,
    EVENT_DETAIL_TEXT_INDEX_KEYS,
    TEXT_TELEMETRY_EVENT_TYPES,
    Batch,
)

SANITIZER_VERSION = "1.0.0"


def iter_batch_files(data_root: Path) -> list[Path]:
    """Return the canonical (symlink-resolved, de-duplicated) batch JSON files.

    ``devices/<id>/by_category/<cat>/<date>/<uuid>.json`` are symlinks to the
    canonical ``devices/<id>/<date>/<uuid>.json``; resolving + de-duplicating
    keeps each underlying batch once.
    """
    devices = data_root / "devices"
    if not devices.is_dir():
        raise SystemExit(f"no devices/ directory under --data-root: {data_root}")
    seen: dict[Path, Path] = {}
    for path in devices.rglob("*.json"):
        if path.name.endswith(".meta.json"):
            continue
        # Skip by_category/ (symlinks to the canonical date-dir file); rewriting
        # via a symlink would look for the meta next to the link, which is absent.
        if "by_category" in path.parts:
            continue
        seen.setdefault(path.resolve(), path)
    return [seen[key] for key in sorted(seen)]


def sanitize_batch(obj: dict[str, Any], counts: Counter) -> bool:
    """Mutate a batch dict in place; update ``counts``; return whether it changed."""
    changed = False
    for event in obj.get("context_events", []):
        detail = event.get("event_detail")
        if not isinstance(detail, dict):
            continue
        counts["events_with_event_detail"] += 1
        forbidden = EVENT_DETAIL_FORBIDDEN_TELEMETRY_KEYS & detail.keys()
        if forbidden:
            for key in forbidden:
                del detail[key]
            counts["events_forbidden_keys_stripped"] += 1
            counts["forbidden_keys_removed"] += len(forbidden)
            changed = True
        if event.get("event_type") in TEXT_TELEMETRY_EVENT_TYPES:
            counts["text_events_seen"] += 1
            index_keys = EVENT_DETAIL_TEXT_INDEX_KEYS & detail.keys()
            if index_keys:
                for key in index_keys:
                    del detail[key]
                counts["text_index_keys_removed"] += len(index_keys)
                changed = True
            wall = event.get("event_time_wall_millis")
            if isinstance(wall, int) and wall % 1000 != 0:
                event["event_time_wall_millis"] = (wall // 1000) * 1000
                counts["timestamps_floored"] += 1
                changed = True
    return changed


def stamp_meta(meta_path: Path, stamped_at: str) -> None:
    """Append ``sanitized_at`` / ``sanitizer_version`` to a batch's meta JSON."""
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["sanitized_at"] = stamped_at
    meta["sanitizer_version"] = SANITIZER_VERSION
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Sanitize on-disk event_detail text telemetry (SRV-1-C).")
    parser.add_argument(
        "--data-root",
        type=Path,
        default=_REPO_ROOT / "deploy" / "data" / "paper",
        help="Storage root holding devices/ and index/ (default: deploy/data/paper).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report statistics without writing any file.")
    args = parser.parse_args()

    files = iter_batch_files(args.data_root)
    counts: Counter = Counter()
    batches_changed = 0
    validated_ok = 0
    validation_failures: list[tuple[str, str]] = []
    stamped_at = datetime.now(timezone.utc).isoformat()

    for path in files:
        obj = json.loads(path.read_text(encoding="utf-8"))
        changed = sanitize_batch(obj, counts)
        # Re-validate the cleaned object against the (new) ingest contract.
        try:
            Batch.model_validate(obj)
            validated_ok += 1
        except Exception as exc:  # pydantic.ValidationError or otherwise
            validation_failures.append((path.name, str(exc).splitlines()[0][:200]))
        if changed:
            batches_changed += 1
            if not args.dry_run:
                path.write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                stamp_meta(path.with_name(path.stem + ".meta.json"), stamped_at)

    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(f"[{mode}] data-root: {args.data_root}")
    print(f"  batch files scanned          : {len(files)}")
    print(f"  batches changed              : {batches_changed}")
    print(f"  events with event_detail     : {counts['events_with_event_detail']}")
    print(f"  events forbidden-key stripped: {counts['events_forbidden_keys_stripped']}")
    print(f"  forbidden keys removed (sum) : {counts['forbidden_keys_removed']}")
    print(f"  text events seen             : {counts['text_events_seen']}")
    print(f"  text index keys removed (sum): {counts['text_index_keys_removed']}")
    print(f"  timestamps floored to 1s     : {counts['timestamps_floored']}")
    print(f"  re-validated OK (new schema) : {validated_ok}/{len(files)}")
    if validation_failures:
        print(f"  VALIDATION FAILURES ({len(validation_failures)}):")
        for name, err in validation_failures[:20]:
            print(f"    - {name}: {err}")
        return 1
    if not args.dry_run and batches_changed:
        print(f"  meta stamped: sanitized_at={stamped_at} sanitizer_version={SANITIZER_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
