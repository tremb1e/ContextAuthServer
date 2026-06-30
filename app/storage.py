from __future__ import annotations

import json
import os
import shutil
import time
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import SETTINGS
from .schemas import Batch, Envelope


def now_ms() -> int:
    return int(time.time() * 1000)


def _date_dir(wall_millis: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(wall_millis / 1000))


def _safe_join(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts).resolve()
    root_resolved = root.resolve()
    if root_resolved != candidate and root_resolved not in candidate.parents:
        raise ValueError("path_traversal")
    return candidate


@dataclass
class StoredBatch:
    batch_path: Path
    meta_path: Path
    category_link: Path | None


class DuplicateBatchConflict(OSError):
    pass


class DiskStore:
    def __init__(self, data_dir: Path = SETTINGS.data_dir, min_free_bytes: int = SETTINGS.min_free_bytes):
        self.data_dir = data_dir.resolve()
        self.min_free_bytes = min_free_bytes
        self.devices_dir = self.data_dir / "devices"
        self.index_dir = self.data_dir / "index"
        self.quarantine_dir = self.data_dir / "quarantine"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        try:
            for path in [self.data_dir, self.devices_dir, self.index_dir, self.quarantine_dir]:
                path.mkdir(parents=True, exist_ok=True)
            for name in ["devices.jsonl", "batches.jsonl", "errors.jsonl"]:
                (_safe_join(self.index_dir, name)).touch(exist_ok=True)
        except PermissionError as exc:
            raise PermissionError(
                f"server data directory is not writable: {self.data_dir}; "
                "check the host bind-mount owner or enable the container permission fixer"
            ) from exc

    def assert_ready(self) -> None:
        self._ensure_dirs()
        self.assert_space_available()
        for path in [self.data_dir, self.devices_dir, self.index_dir, self.quarantine_dir]:
            if not os.access(path, os.W_OK | os.X_OK):
                raise PermissionError(f"server storage path is not writable: {path}")
        for name in ["devices.jsonl", "batches.jsonl", "errors.jsonl"]:
            index_path = _safe_join(self.index_dir, name)
            if not os.access(index_path, os.W_OK):
                raise PermissionError(f"server index file is not writable: {index_path}")

    def assert_space_available(self) -> None:
        free = shutil.disk_usage(self.data_dir).free
        if free < self.min_free_bytes:
            raise OSError("disk_space_below_threshold")

    def append_error(self, reason: str, envelope: Envelope | None, request_id: str, details: dict[str, Any] | None = None) -> None:
        record = {
            "ts": now_ms(),
            "request_id": request_id,
            "reason": reason,
            "device_id_prefix": envelope.device_id[:8] if envelope else None,
            "batch_id": envelope.batch_id if envelope else None,
            "details": details or {},
        }
        self._append_jsonl(self.index_dir / "errors.jsonl", record)

    def quarantine(self, envelope: Envelope | None, plaintext: dict[str, Any] | bytes | None, reason: str, request_id: str) -> Path:
        device_id = envelope.device_id if envelope else "unknown"
        batch_id = envelope.batch_id if envelope else f"unknown-{request_id}"
        date = _date_dir(envelope.created_at_wall_millis if envelope else now_ms())
        target_dir = _safe_join(self.quarantine_dir, device_id, date)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = _safe_join(target_dir, f"{batch_id}.json")
        if isinstance(plaintext, bytes):
            payload_summary: Any = {"payload_sha256": hashlib.sha256(plaintext).hexdigest(), "payload_type": "bytes"}
        elif plaintext is None:
            payload_summary = {"payload_type": "unavailable"}
        else:
            payload_summary = {
                "payload_sha256": hashlib.sha256(
                    json.dumps(plaintext, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "payload_type": "json",
                "top_level_keys": sorted(plaintext.keys())[:50],
            }
        path.write_text(json.dumps({"reason": reason, "payload_summary": payload_summary}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        self.append_error(reason, envelope, request_id, {"quarantine_path": str(path)})
        return path

    def store(self, envelope: Envelope, batch: Batch, plaintext: dict[str, Any], request_id: str, compressed_size: int, decompressed_size: int) -> StoredBatch:
        self.assert_space_available()
        date = _date_dir(batch.started_at_wall_millis)
        batch_dir = _safe_join(self.devices_dir, envelope.device_id, date)
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch_path = _safe_join(batch_dir, f"{envelope.batch_id}.json")
        meta_path = _safe_join(batch_dir, f"{envelope.batch_id}.meta.json")
        batch_text = json.dumps(plaintext, ensure_ascii=False, sort_keys=True)
        if batch_path.exists():
            existing = batch_path.read_text(encoding="utf-8")
            if existing != batch_text:
                raise DuplicateBatchConflict("duplicate_batch_id_conflict")
            return StoredBatch(
                batch_path=batch_path,
                meta_path=meta_path,
                category_link=self._category_link(envelope.device_id, batch.task_category, date, envelope.batch_id)
                    if batch.collection_source == "BUILTIN_TASK" and batch.task_category
                    else None,
            )
        batch_path.write_text(batch_text, encoding="utf-8")

        meta = {
            "request_id": request_id,
            "ingested_at_wall_millis": now_ms(),
            "envelope": envelope.model_dump(exclude={"payload_base64"}),
            "compressed_payload_omitted": True,
            "compressed_size_bytes": compressed_size,
            "decompressed_size_bytes": decompressed_size,
            "schema_validation_result": "ok",
            "batch_file": str(batch_path),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, sort_keys=True), encoding="utf-8")

        self._append_jsonl(
            self.index_dir / "devices.jsonl",
            {"ts": now_ms(), "device_id": envelope.device_id, "device_id_prefix": envelope.device_id[:8]},
        )
        self._append_jsonl(
            self.index_dir / "batches.jsonl",
            {
                "ts": now_ms(),
                "device_id": envelope.device_id,
                "device_id_prefix": envelope.device_id[:8],
                "batch_id": envelope.batch_id,
                "collection_source": batch.collection_source,
                "task_category": batch.task_category,
                "path": str(batch_path),
            },
        )

        category_link = None
        if batch.collection_source == "BUILTIN_TASK" and batch.task_category:
            category_dir = self._category_dir(envelope.device_id, batch.task_category, date)
            category_dir.mkdir(parents=True, exist_ok=True)
            category_link = self._category_link(envelope.device_id, batch.task_category, date, envelope.batch_id)
            if category_link.exists() or category_link.is_symlink():
                category_link.unlink()
            try:
                os.symlink(os.path.relpath(batch_path, category_dir), category_link)
            except OSError:
                # Fall back to a tiny pointer JSON on filesystems without symlink support.
                category_link.write_text(json.dumps({"target": str(batch_path)}, sort_keys=True), encoding="utf-8")

        return StoredBatch(batch_path=batch_path, meta_path=meta_path, category_link=category_link)

    def _category_dir(self, device_id: str, task_category: str, date: str) -> Path:
        return _safe_join(self.devices_dir, device_id, "by_category", task_category, date)

    def _category_link(self, device_id: str, task_category: str, date: str, batch_id: str) -> Path:
        # Do not resolve the final path here: when the link already exists,
        # Path.resolve() follows it to the target batch outside by_category.
        return self._category_dir(device_id, task_category, date) / f"{batch_id}.json"

    @staticmethod
    def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


STORE = DiskStore()
