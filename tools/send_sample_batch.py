#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import request

import lz4.frame


TASK_CATEGORIES = [
    "C0",
    "C1",
    "C2",
    "C3",
    "C4",
    "C5",
    "C6",
    "C7",
]

TASK_META = {
    "C0": ("Still timer", "Quiet hold"),
    "C1": ("Research protocol reading", "Static reading"),
    "C2": ("Research information feed", "Single-finger feed"),
    "C3": ("Paragraph copy", "Text entry"),
    "C4": ("Simulated phone settings", "Multi-control operation"),
    "C5": ("Blue ball tapping", "Landscape touch challenge"),
    "C6": ("Local video playback", "Video watching"),
    "C7": ("Wrist rotation", "Explicit wrist rotation"),
}


def get_json(url: str) -> dict[str, Any]:
    with request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def compute_device_id(server_study_salt: str, fake_android_id: str) -> str:
    return hmac.new(
        server_study_salt.encode("utf-8"),
        fake_android_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def make_batch(
    device_id: str,
    task_category: str | None,
    index: int,
    session_id: str,
    task_session_id: str | None,
    task_started_at: int | None,
) -> dict[str, Any]:
    batch_id = str(uuid.uuid4())
    now = int(time.time() * 1000)
    is_builtin = task_category is not None
    task_name, task_intuition = TASK_META.get(task_category or "", (None, None))
    context_event_id = str(uuid.uuid4())
    return {
        "batch_id": batch_id,
        "device_id": device_id,
        "session_id": task_session_id if is_builtin else session_id,
        "record_type": "collection",
        "collection_source": "BUILTIN_TASK" if is_builtin else "THIRD_PARTY_APP",
        "app_package_name": "com.example.target",
        "foreground_activity_class_name": "com.example.target.MainActivity",
        "foreground_component_name": "com.example.target/.MainActivity",
        "sampling_rate_hz": 100,
        "batch_duration_seconds": 5,
        "task_sequence": int(task_category[1:]) if is_builtin else None,
        "task_id": task_category if is_builtin else None,
        "task_name": task_name if is_builtin else None,
        "task_intuitive_description": task_intuition if is_builtin else None,
        "task_category": task_category,
        "task_session_id": task_session_id if is_builtin else None,
        "task_started_at_wall_millis": task_started_at if is_builtin else None,
        "task_elapsed_seconds_at_batch_end": (index + 1) * 5 if is_builtin else None,
        "app_version": "1.0.0",
        "rule_version": "1",
        "rule_hash": "b" * 64,
        "consent_version": "1",
        "started_at_wall_millis": now - 5000,
        "ended_at_wall_millis": now,
        "base_elapsed_nanos": 123456789 + index,
        "sensor_samples": [
            {
                "sensor_type": "ACCELEROMETER",
                "timestamp_elapsed_nanos": 123456790 + index,
                "wall_time_estimated_millis": now - 4500,
                "x": 0.1,
                "y": 0.2,
                "z": 9.8,
                "accuracy": 3,
            }
        ],
        "touch_events": [
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "TOUCH_INTERACTION_START",
                "event_time_uptime_millis": 123456790 + index,
                "event_time_wall_millis": now - 4300,
                "collected_at_wall_millis": now - 4299,
            },
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "TOUCH_INTERACTION_END",
                "event_time_uptime_millis": 123456890 + index,
                "event_time_wall_millis": now - 4200,
                "collected_at_wall_millis": now - 4199,
            },
        ],
        "context_events": [
            {
                "event_id": context_event_id,
                "event_type": "TYPE_WINDOW_CONTENT_CHANGED",
                "event_time_wall_millis": now - 4000,
                "app_package_name": "com.example.target",
                "foreground_activity_class_name": "com.example.target.MainActivity",
                "foreground_component_name": "com.example.target/.MainActivity",
                "input_method_visible": False,
                "coarse_orientation": "portrait",
                "window_title_redacted": "<DROPPED>",
                "root_nodes": [
                    {
                        "node_id": "node-1",
                        "class_name": "android.widget.Button",
                        "viewIdResourceName": "com.example.target:id/confirm",
                        "text": "确认",
                        "text_redacted": None,
                        "content_desc_redacted": None,
                        "clickable": True,
                        "editable": False,
                        "scrollable": False,
                        "password": False,
                        "child_count": 0,
                        "actions_summary": ["CLICK"],
                        "depth": 0,
                    }
                ],
                "redaction_summary": {
                    "dropped_password_nodes": 0,
                    "dropped_editable_texts": 0,
                    "replaced_email": 0,
                    "replaced_phone": 0,
                    "replaced_url": 0,
                    "replaced_number": 0,
                    "replaced_card": 0,
                    "replaced_id_number": 0,
                },
            }
        ],
        "context_features": [
            {
                "feature_id": str(uuid.uuid4()),
                "event_id": context_event_id,
                "computed_at_wall_millis": now - 3500,
                "collection_source": "BUILTIN_TASK" if is_builtin else "THIRD_PARTY_APP",
                "task_sequence": int(task_category[1:]) if is_builtin else None,
                "task_id": task_category if is_builtin else None,
                "task_name": task_name if is_builtin else None,
                "task_intuitive_description": task_intuition if is_builtin else None,
                "task_category": task_category,
                "task_session_id": task_session_id if is_builtin else None,
                "input_method_visible": False,
                "keyboard_visible_estimated": False,
                "editable_count": 0,
                "scrollable_count": 0,
                "clickable_count": 1,
                "password_node_seen": False,
                "media_like_score": 0.0,
                "list_like_score": 0.0,
                "form_like_score": 0.0,
                "game_like_score": 0.0,
                "node_class_histogram": {"TextView": 1},
                "event_type": "TYPE_WINDOW_CONTENT_CHANGED",
                "coarse_orientation": "portrait",
                "estimated_context_category": task_category or "UNKNOWN",
            }
        ],
        "skip_events": [],
        "diagnostics": {
            "sensor_sample_count": 1,
            "context_event_count": 1,
            "touch_event_count": 2,
            "redaction_applied": True,
            "compression": "lz4_frame",
            "encryption": "none",
            "sampling_rate_hz": 100,
        },
    }


def envelope_for(batch: dict[str, Any]) -> dict[str, Any]:
    compressed = lz4.frame.compress(json.dumps(batch, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return {
        "algorithm": "LZ4_FRAME+JSON",
        "payload_base64": base64.b64encode(compressed).decode("ascii"),
        "payload_sha256_hex": hashlib.sha256(compressed).hexdigest(),
        "device_id": batch["device_id"],
        "batch_id": batch["batch_id"],
        "rule_version": batch["rule_version"],
        "rule_hash": batch["rule_hash"],
        "created_at_wall_millis": batch["started_at_wall_millis"],
    }


def post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url.rstrip("/") + "/api/v1/ingest",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--task-category", default="C3")
    parser.add_argument("--device-id")
    parser.add_argument("--device-suffix", default="fixed")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--interval", type=float, default=0.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    task_category = None if args.task_category == "THIRD_PARTY_APP" else args.task_category
    if task_category is not None and task_category not in TASK_CATEGORIES:
        raise SystemExit(f"unsupported task category: {task_category}")

    config = get_json(args.server.rstrip("/") + "/api/v1/config")
    fake_android_id = f"contextauthlab-fake-android-id-{args.device_suffix}-{args.seed}"
    device_id = args.device_id or compute_device_id(config["serverStudySalt"], fake_android_id)
    task_session_id = str(uuid.uuid4()) if task_category is not None else None
    session_id = task_session_id or str(uuid.uuid4())
    task_started_at = int(time.time() * 1000) if task_category is not None else None

    responses = []
    for index in range(args.count):
        batch = make_batch(device_id, task_category, index, session_id, task_session_id, task_started_at)
        responses.append(post_json(args.server, envelope_for(batch)))
        if args.interval > 0 and index < args.count - 1:
            time.sleep(args.interval)

    summary = {"device_id": device_id, "count": len(responses), "responses": responses}
    if args.output:
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
