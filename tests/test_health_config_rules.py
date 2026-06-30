from __future__ import annotations

import importlib
import json
from pathlib import Path

from fastapi.testclient import TestClient


def test_health_endpoint_returns_ok(server_client) -> None:
    response = server_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_endpoint_checks_writable_storage(server_client) -> None:
    response = server_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_config_endpoint_returns_stable_values_across_calls(server_client) -> None:
    first = server_client.get("/api/v1/config").json()
    second = server_client.get("/api/v1/config").json()
    assert first["serverStudySalt"] == "Continuous_Authentication"
    assert first["serverStudySalt"] == second["serverStudySalt"]
    assert first["rulesVersion"] == "1"
    assert isinstance(first["serverTimeMillis"], int)
    assert first["timeSync"] == second["timeSync"]
    assert first["timeSync"]["method"] == "HTTP_MIDPOINT"
    assert first["timeSync"]["serverTimeField"] == "serverTimeMillis"
    assert first["timeSync"]["region"] == "CN"
    assert first["timeSync"]["maxAcceptableRttMillis"] > 0
    assert "ntp.aliyun.com" in first["timeSync"]["recommendedNtpServers"]
    assert "ntp.tencent.com" in first["timeSync"]["recommendedNtpServers"]
    assert "0.cn.pool.ntp.org" in first["timeSync"]["recommendedNtpServers"]


def test_rules_returns_hash_and_redaction_policy(server_client) -> None:
    from app.rules import DEFAULT_PACKAGE_BLOCKLIST, DEFAULT_UI_REDACTION_RULES, rule_hash

    response = server_client.get("/api/v1/rules")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "version",
        "updated_at",
        "rules",
        "package_blocklist",
        "max_text_length",
        "default_text_action",
        "rule_hash",
    }
    assert payload["version"] == "1"
    assert payload["rules"] == DEFAULT_UI_REDACTION_RULES
    assert payload["package_blocklist"] == DEFAULT_PACKAGE_BLOCKLIST == []
    assert payload["max_text_length"] == 128
    assert payload["default_text_action"] == "REDACT"
    assert len(payload["rule_hash"]) == 64
    hash_payload = {key: value for key, value in payload.items() if key != "rule_hash"}
    assert payload["rule_hash"] == rule_hash(hash_payload)


def test_rules_file_is_materialized_from_packaged_default(server_client) -> None:
    rules_file = server_client.app.state.test_data_dir / "rules.json"
    assert rules_file.exists()
    stored = json.loads(rules_file.read_text(encoding="utf-8"))
    payload = server_client.get("/api/v1/rules").json()
    assert stored["version"] == payload["version"]
    assert stored["rules"] == payload["rules"]
    assert "rule_hash" not in stored


def test_custom_rules_file_initializes_server(tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data" / "paper"
    log_dir = tmp_path / "logs"
    rules_file = tmp_path / "custom_rules.json"
    rules_file.write_text(
        json.dumps(
            {
                "version": "42",
                "updated_at": "2026-05-22T00:00:00Z",
                "rules": [
                    {
                        "id": "ticket",
                        "target": "text",
                        "action": "REDACT",
                        "pattern": "TICKET-[0-9]+",
                        "replacement": "<TICKET>",
                    }
                ],
                "package_blocklist": [],
                "max_text_length": 64,
                "default_text_action": "REDACT",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SERVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SERVER_LOG_DIR", str(log_dir))
    monkeypatch.setenv("SERVER_RULES_FILE", str(rules_file))
    monkeypatch.setenv("SERVER_STUDY_SALT", "Continuous_Authentication")
    monkeypatch.setenv("RULES_VERSION", "1")
    monkeypatch.setenv("SERVER_MIN_FREE_BYTES", "0")

    import app.config as config
    import app.logging_config as logging_config
    import app.rules as rules
    import app.storage as storage
    import app.main as main

    importlib.reload(config)
    importlib.reload(rules)
    importlib.reload(logging_config)
    importlib.reload(storage)
    importlib.reload(main)

    with TestClient(main.app) as client:
        payload = client.get("/api/v1/rules").json()
        config_payload = client.get("/api/v1/config").json()

    assert payload["version"] == "42"
    assert payload["rules"][0]["id"] == "ticket"
    assert payload["package_blocklist"] == []
    assert config_payload["rulesVersion"] == "42"
    assert len(payload["rule_hash"]) == 64


def test_default_rules_are_non_empty_and_schema_backed(server_client) -> None:
    payload = server_client.get("/api/v1/rules").json()

    assert len(payload["rules"]) >= 5
    assert payload["package_blocklist"] == []
    ids = {rule["id"] for rule in payload["rules"]}
    assert {"email", "phone_cn", "url", "id_number_cn", "payment_card", "opaque_token", "long_number"} <= ids
    for rule in payload["rules"]:
        assert rule["target"] == "text"
        assert rule["action"] == "REDACT"
        assert rule["pattern"]
        assert rule["replacement"].startswith("<")
        assert rule["replacement"].endswith(">")


def test_openapi_documents_config_and_rules_models(server_client) -> None:
    response = server_client.get("/openapi.json")
    assert response.status_code == 200
    schemas = response.json()["components"]["schemas"]
    assert "ConfigResponse" in schemas
    assert "TimeSyncConfig" in schemas
    assert "RulesResponse" in schemas
    assert "UiRedactionRule" in schemas


def test_no_dashboard_routes(server_client) -> None:
    assert server_client.get("/dashboard").status_code == 404
    assert server_client.get("/dashboard/devices").status_code == 404


def test_no_templates_or_static_frontend_required() -> None:
    server_root = Path(__file__).resolve().parents[1]
    assert not (server_root / "app" / "templates").exists()
    assert not (server_root / "app" / "static").exists()
