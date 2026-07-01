"""Config loading, deep-merge over the default config, and config hashing."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Union

import yaml

PathLike = Union[str, Path]

#: Location of the packaged default config (build contract §7).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "configs" / "default.yaml"


def _read_yaml(path: PathLike) -> dict[str, Any]:
    """Load a YAML file into a dict (empty file -> empty dict).

    Args:
        path: YAML file path.

    Returns:
        Parsed mapping.

    Raises:
        TypeError: If the YAML top-level is not a mapping.
    """
    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TypeError(f"config file {path} must contain a mapping at the top level")
    return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``.

    Nested dicts are merged key-by-key; any non-dict value in ``override``
    replaces the corresponding value in ``base``. ``base`` is not mutated.

    Args:
        base: Base mapping (lower priority).
        override: Override mapping (higher priority).

    Returns:
        A new merged mapping.
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: PathLike | None = None, *, default_path: PathLike = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load a config as ``default.yaml`` deep-merged with an optional override.

    Args:
        path: Optional path to an override YAML (e.g. ``configs/experiments/m7.yaml``).
            If ``None``, the default config is returned as-is.
        default_path: Path to the base default config.

    Returns:
        The fully merged config dict.
    """
    base = _read_yaml(default_path)
    if path is None:
        return base
    override = _read_yaml(path)
    return deep_merge(base, override)


def config_hash(cfg: dict[str, Any]) -> str:
    """Return a stable SHA-256 hex digest of a config dict.

    The config is serialized to canonical JSON (sorted keys, compact
    separators) so logically-equal configs hash identically regardless of key
    ordering. Non-JSON values are coerced via ``str``.

    Args:
        cfg: The config mapping.

    Returns:
        A 64-char lowercase hex SHA-256 digest.
    """
    canonical = json.dumps(cfg, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
