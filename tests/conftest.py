from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def server_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    data_dir = tmp_path / "data" / "paper"
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("SERVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SERVER_LOG_DIR", str(log_dir))
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

    main.app.state.test_data_dir = data_dir
    main.app.state.test_log_dir = log_dir
    return TestClient(main.app)
