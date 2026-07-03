from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import lz4.frame
from prometheus_client.parser import text_string_to_metric_families

from helpers import DEVICE_ID, clone, envelope_for, sample_batch


def _data_dir(server_client) -> Path:
    return server_client.app.state.test_data_dir


def _log_dir(server_client) -> Path:
    return server_client.app.state.test_log_dir


def test_ingest_valid_envelope_stores_batch_and_meta(server_client) -> None:
    batch = sample_batch()
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 200
    assert response.json()["stored"] is True

    data_dir = _data_dir(server_client)
    batch_path = data_dir / "devices" / DEVICE_ID / "2024-03-09" / f"{batch['batch_id']}.json"
    meta_path = data_dir / "devices" / DEVICE_ID / "2024-03-09" / f"{batch['batch_id']}.meta.json"
    assert batch_path.exists()
    assert meta_path.exists()
    stored = json.loads(batch_path.read_text(encoding="utf-8"))
    assert stored["task_category"] == "I3"
    assert stored["task_id"] == "I3"
    assert stored["task_intuitive_description"] == "List browsing"
    assert stored["touch_events"][0]["event_type"] == "TOUCH_INTERACTION_START"
    assert "x" not in stored["touch_events"][0]
    assert stored["context_events"][0]["coarse_orientation"] == "portrait"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["compressed_payload_omitted"] is True
    assert "payload_base64" not in json.dumps(meta)


def test_by_category_index_created(server_client) -> None:
    batch = sample_batch(task_category="I4")
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 200
    link = _data_dir(server_client) / "devices" / DEVICE_ID / "by_category" / "I4" / "2024-03-09" / f"{batch['batch_id']}.json"
    assert link.exists()
    if link.is_symlink():
        assert link.resolve().exists()
        assert not Path(link.readlink()).is_absolute()


def test_all_canonical_categories_accepted(server_client) -> None:
    """Every canonical task class I0..I6 is accepted and indexed by category."""
    for category in ("I0", "I1", "I2", "I3", "I4", "I5", "I6"):
        batch = sample_batch(task_category=category)
        response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
        assert response.status_code == 200, f"{category} should be accepted"
        link = _data_dir(server_client) / "devices" / DEVICE_ID / "by_category" / category / "2024-03-09" / f"{batch['batch_id']}.json"
        assert link.exists(), f"{category} by_category index missing"


def test_i7_legacy_category_still_accepted(server_client) -> None:
    """LEGACY: old wrist id I7 is STILL accepted (backward-compat regression).

    New APKs emit I6 for wrist rotation, but old-APK / old on-disk batches carry
    I7. The ingest contract keeps I7 so those are never quarantined (the
    2026-07-03 morning incident that dropped 36 legacy batches).
    """
    batch = sample_batch(task_category="I7")
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 200
    link = _data_dir(server_client) / "devices" / DEVICE_ID / "by_category" / "I7" / "2024-03-09" / f"{batch['batch_id']}.json"
    assert link.exists()


def test_c6_legacy_category_still_accepted(server_client) -> None:
    """LEGACY: a retired research-taxonomy id C6 is STILL accepted (compat)."""
    batch = sample_batch(task_category="C6")
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 200
    link = _data_dir(server_client) / "devices" / DEVICE_ID / "by_category" / "C6" / "2024-03-09" / f"{batch['batch_id']}.json"
    assert link.exists()


def test_unknown_task_category_rejected(server_client) -> None:
    """A task id outside the CANONICAL+LEGACY union (e.g. I8) is rejected.

    All other task fields are filled in so the failure isolates the task-category
    union check (not the "missing task fields" rule).
    """
    batch = sample_batch(task_category="I8")
    batch["task_name"] = "Unknown future task"
    batch["task_intuitive_description"] = "Unknown"
    for feature in batch["context_features"]:
        feature["task_name"] = "Unknown future task"
        feature["task_intuitive_description"] = "Unknown"
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_duplicate_batch_is_idempotent_when_payload_matches(server_client) -> None:
    batch = sample_batch()
    env = envelope_for(batch)
    first = server_client.post("/api/v1/ingest", json=env)
    second = server_client.post("/api/v1/ingest", json=env)
    assert first.status_code == 200
    assert second.status_code == 200
    batches = (_data_dir(server_client) / "index" / "batches.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(batches) == 1


def test_duplicate_batch_conflict_rejected_without_overwrite(server_client) -> None:
    batch = sample_batch()
    env = envelope_for(batch)
    assert server_client.post("/api/v1/ingest", json=env).status_code == 200
    stored_path = _data_dir(server_client) / "devices" / DEVICE_ID / "2024-03-09" / f"{batch['batch_id']}.json"
    original_text = stored_path.read_text(encoding="utf-8")

    changed = clone(batch)
    changed["sensor_samples"][0]["x"] = 42.0
    response = server_client.post("/api/v1/ingest", json=envelope_for(changed))
    assert response.status_code == 409
    assert response.json()["detail"] == "duplicate_batch_id_conflict"
    assert stored_path.read_text(encoding="utf-8") == original_text


def test_ingest_rejects_bad_device_id(server_client) -> None:
    batch = sample_batch(device_id="A" * 64)
    env = envelope_for(batch)
    response = server_client.post("/api/v1/ingest", json=env)
    assert response.status_code == 400


def test_ingest_rejects_path_traversal_device_id(server_client) -> None:
    batch = sample_batch(device_id="../" + "a" * 61)
    env = envelope_for(batch)
    response = server_client.post("/api/v1/ingest", json=env)
    assert response.status_code == 400


def test_ingest_rejects_bad_uuid(server_client) -> None:
    batch = sample_batch()
    env = envelope_for(batch)
    env["batch_id"] = "not-a-uuid"
    response = server_client.post("/api/v1/ingest", json=env)
    assert response.status_code == 400


def test_ingest_rejects_bad_algorithm(server_client) -> None:
    batch = sample_batch()
    env = envelope_for(batch)
    env["algorithm"] = "AES-GCM"
    response = server_client.post("/api/v1/ingest", json=env)
    assert response.status_code == 400


def test_ingest_rejects_payload_hash_mismatch(server_client) -> None:
    batch = sample_batch()
    env = envelope_for(batch)
    env["payload_sha256_hex"] = "0" * 64
    response = server_client.post("/api/v1/ingest", json=env)
    assert response.status_code == 400
    assert response.json()["detail"] == "payload_hash_mismatch"


def test_ingest_rejects_corrupted_lz4_payload(server_client) -> None:
    batch = sample_batch()
    env = envelope_for(batch)
    compressed = bytearray(base64.b64decode(env["payload_base64"]))
    compressed[-1] ^= 0x01
    env["payload_base64"] = base64.b64encode(bytes(compressed)).decode("ascii")
    env["payload_sha256_hex"] = hashlib.sha256(bytes(compressed)).hexdigest()
    response = server_client.post("/api/v1/ingest", json=env)
    assert response.status_code == 400
    assert response.json()["detail"] == "corrupted_lz4_payload"


def test_lz4_roundtrip() -> None:
    payload = b'{"hello":"world"}'
    compressed = lz4.frame.compress(payload)
    assert lz4.frame.decompress(compressed) == payload


def test_server_rejects_text_redacted_email(server_client) -> None:
    batch = sample_batch(text_redacted="alice@example.com")
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_server_rejects_text_redacted_card(server_client) -> None:
    batch = sample_batch(text_redacted="4111 1111 1111 1111")
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_server_rejects_text_redacted_plain_content(server_client) -> None:
    batch = sample_batch(text_redacted="Alice hello")
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_ingest_rejects_non_editable_visible_text_but_view_id_field_is_supported(server_client) -> None:
    batch = sample_batch()
    batch["context_events"][0]["root_nodes"][0]["text"] = "visible UI label"
    batch["context_events"][0]["root_nodes"][0]["viewIdResourceName"] = "com.example:id/confirm"
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_server_accepts_legacy_ui_hash_fields_without_secondary_scan(server_client) -> None:
    for legacy_field in ["package_name_hash", "view_id_hash"]:
        batch = sample_batch()
        batch["context_events"][0]["root_nodes"][0][legacy_field] = "a" * 64
        response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
        assert response.status_code == 200


def test_quarantine_when_editable_node_contains_raw_text(server_client) -> None:
    batch = sample_batch()
    node = batch["context_events"][0]["root_nodes"][0]
    node["class_name"] = "android.widget.EditText"
    node["editable"] = True
    node["text"] = "typed secret"
    node["text_redacted"] = "<EDITABLE_TEXT_DROPPED>"
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_schema_rejects_batches_without_redaction_applied(server_client) -> None:
    batch = sample_batch()
    batch["diagnostics"]["redaction_applied"] = False
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_task_category_required_when_builtin(server_client) -> None:
    batch = sample_batch()
    batch["task_category"] = None
    batch["context_features"][0]["task_category"] = None
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_task_category_must_be_null_when_third_party(server_client) -> None:
    batch = sample_batch(collection_source="THIRD_PARTY_APP", task_category=None)
    batch["task_category"] = "I3"
    batch["context_features"][0]["task_category"] = "I3"
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_context_feature_must_reference_context_event(server_client) -> None:
    batch = sample_batch()
    batch["context_features"][0]["event_id"] = "missing-event"
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_context_feature_task_metadata_must_match_batch(server_client) -> None:
    batch = sample_batch()
    batch["context_features"][0]["collection_source"] = "THIRD_PARTY_APP"
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_diagnostics_counts_must_match_payload_arrays(server_client) -> None:
    batch = sample_batch()
    batch["diagnostics"]["touch_event_count"] = 99
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_diagnostics_sampling_rate_must_match_batch(server_client) -> None:
    batch = sample_batch()
    batch["diagnostics"]["sampling_rate_hz"] = 50
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 400
    assert response.json()["detail"] == "schema_validation_failed"


def test_envelope_batch_device_id_mismatch(server_client) -> None:
    batch = sample_batch()
    env = envelope_for(batch)
    env["device_id"] = "c" * 64
    response = server_client.post("/api/v1/ingest", json=env)
    assert response.status_code == 400
    assert response.json()["detail"] == "envelope_batch_device_id_mismatch"


def test_envelope_batch_id_mismatch(server_client) -> None:
    batch = sample_batch()
    env = envelope_for(batch)
    env["batch_id"] = "11111111-1111-4111-8111-111111111111"
    response = server_client.post("/api/v1/ingest", json=env)
    assert response.status_code == 400
    assert response.json()["detail"] == "envelope_batch_id_mismatch"


def test_ingest_accepts_missing_or_stale_rule_hash(server_client) -> None:
    batch = sample_batch()
    batch["rule_hash"] = "0" * 64
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 200


def test_ingest_accepts_rule_version_metadata_mismatch(server_client) -> None:
    batch = sample_batch()
    env = envelope_for(batch)
    env["rule_version"] = "stale"
    env["rule_hash"] = "c" * 64
    response = server_client.post("/api/v1/ingest", json=env)
    assert response.status_code == 200


def test_ingest_rejects_negative_envelope_created_at(server_client) -> None:
    batch = sample_batch()
    env = envelope_for(batch)
    env["created_at_wall_millis"] = -1
    response = server_client.post("/api/v1/ingest", json=env)
    assert response.status_code == 400


def test_error_log_no_plain_payload(server_client) -> None:
    batch = sample_batch()
    env = envelope_for(batch)
    env["payload_sha256_hex"] = "0" * 64
    server_client.post("/api/v1/ingest", json=env)
    errors = (_data_dir(server_client) / "index" / "errors.jsonl").read_text(encoding="utf-8")
    assert env["payload_base64"] not in errors
    assert "alice@example.com" not in errors


def test_structured_log_for_each_ingest(server_client) -> None:
    batch = sample_batch()
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 200
    log_text = (_log_dir(server_client) / "server.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line)["event"] for line in log_text.splitlines() if line.strip()]
    assert "ingest_received" in events
    assert "ingest_decompressed" in events
    assert "ingest_stored" in events


def test_structured_log_no_sensitive_fields(server_client) -> None:
    batch = sample_batch()
    env = envelope_for(batch)
    server_client.post("/api/v1/ingest", json=env)
    log_text = (_log_dir(server_client) / "server.jsonl").read_text(encoding="utf-8")
    assert "ENCRYPTION_PASSWORD" not in log_text
    assert env["payload_base64"] not in log_text
    assert DEVICE_ID not in log_text
    assert DEVICE_ID[:8] in log_text


def test_metrics_endpoint_returns_prometheus_format(server_client) -> None:
    response = server_client.get("/metrics")
    assert response.status_code == 200
    families = list(text_string_to_metric_families(response.text))
    names = {family.name for family in families}
    assert "ingest" in names
    assert "server_up" in names
    assert "ingest_total" in response.text


def test_metrics_counters_increment(server_client) -> None:
    for _ in range(2):
        batch = sample_batch()
        assert server_client.post("/api/v1/ingest", json=envelope_for(batch)).status_code == 200
    metrics = server_client.get("/metrics").text
    assert 'ingest_total{result="ok"}' in metrics


def test_disk_space_threshold_reject(server_client, monkeypatch) -> None:
    import app.main as main

    monkeypatch.setattr(main.STORE, "assert_space_available", lambda: (_ for _ in ()).throw(OSError("disk_space_below_threshold")))
    batch = sample_batch()
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 507
    assert response.json()["detail"] == "disk_space_below_threshold"


def test_storage_error_response_does_not_leak_path(server_client, monkeypatch) -> None:
    import app.main as main

    monkeypatch.setattr(main.STORE, "assert_space_available", lambda: None)
    monkeypatch.setattr(
        main.STORE,
        "store",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("/data/paper/private/path")),
    )
    batch = sample_batch()
    response = server_client.post("/api/v1/ingest", json=envelope_for(batch))
    assert response.status_code == 507
    assert response.json()["detail"] == "storage_write_failed"
