from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .config import SETTINGS, Settings
from .schemas import RulesResponse


PACKAGED_RULES_PATH = Path(__file__).with_name("default_rules.json")
ZERO_RULE_HASH = "0" * 64


def _read_rules_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid_rules_json:{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("rules_file_must_contain_json_object")
    return payload


def _validated_rules_payload(payload: dict[str, Any], fallback_version: str) -> dict[str, Any]:
    normalized = dict(payload)
    # The stored rules file is the editable policy source. The response hash is
    # computed at runtime so it cannot drift from file contents.
    normalized.pop("rule_hash", None)
    if not str(normalized.get("version", "")).strip():
        normalized["version"] = fallback_version
    model_input = dict(normalized)
    model_input["rule_hash"] = ZERO_RULE_HASH
    return RulesResponse.model_validate(model_input).model_dump(exclude={"rule_hash"})


def _write_rules_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _packaged_rules_payload(fallback_version: str = SETTINGS.rules_version) -> dict[str, Any]:
    return _validated_rules_payload(_read_rules_file(PACKAGED_RULES_PATH), fallback_version)


def ensure_rules_file(settings: Settings = SETTINGS) -> Path:
    if settings.rules_file.exists():
        return settings.rules_file
    payload = _packaged_rules_payload(settings.rules_version)
    payload["version"] = settings.rules_version
    _write_rules_file(settings.rules_file, payload)
    return settings.rules_file


def load_rules(settings: Settings = SETTINGS) -> dict[str, Any]:
    rules_path = ensure_rules_file(settings)
    return _validated_rules_payload(_read_rules_file(rules_path), settings.rules_version)


DEFAULT_RULES: dict[str, Any] = _packaged_rules_payload()
DEFAULT_UI_REDACTION_RULES: list[dict[str, Any]] = copy.deepcopy(DEFAULT_RULES["rules"])
DEFAULT_PACKAGE_BLOCKLIST: list[str] = list(DEFAULT_RULES["package_blocklist"])
ACTIVE_RULES: dict[str, Any] = load_rules()


def active_rules_payload() -> dict[str, Any]:
    return copy.deepcopy(ACTIVE_RULES)


def active_rules_version() -> str:
    return str(ACTIVE_RULES["version"])


def rule_hash(rules: dict[str, Any] | None = None) -> str:
    payload = json.dumps(rules if rules is not None else ACTIVE_RULES, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rules_response() -> RulesResponse:
    payload = active_rules_payload()
    payload["rule_hash"] = rule_hash(payload)
    return RulesResponse.model_validate(payload)
