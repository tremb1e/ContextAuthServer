"""Structured logging + run-context snapshot.

- :func:`get_logger` returns a plain stdlib logger for human-facing messages.
- :class:`JsonlLogger` writes one JSON object per line (UTC timestamp) for
  machine-readable event logs (``logs/train.jsonl`` etc.), per _recon_spec §12.
- :func:`run_context` captures an environment snapshot (python/torch/numpy
  versions, git commit best-effort, seed, hostname, timestamp). The timestamp
  is passed IN by the caller so this module NEVER calls ``datetime.now`` at
  import time (keeps imports side-effect free and deterministic).
"""

from __future__ import annotations

import json
import logging
import platform
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Union

PathLike = Union[str, Path]

_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_configured_root = False


def get_logger(name: str = "research", level: int = logging.INFO) -> logging.Logger:
    """Return a configured stdlib logger for human-facing messages.

    Idempotent: the root handler is configured at most once, and the named
    logger will not duplicate handlers across calls.

    Args:
        name: Logger name.
        level: Logging level for the returned logger.

    Returns:
        A :class:`logging.Logger`.
    """
    global _configured_root
    if not _configured_root:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root = logging.getLogger()
        if not root.handlers:
            root.addHandler(handler)
        root.setLevel(level)
        _configured_root = True
    logger = logging.getLogger(name)
    logger.setLevel(level)
    return logger


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with ``Z`` suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class JsonlLogger:
    """Append-only JSON-lines event logger.

    Each :meth:`log` call writes exactly one JSON object on its own line, with
    a UTC ISO-8601 ``ts`` and the given ``event`` name plus arbitrary fields.
    Non-JSON-serializable field values are coerced with ``str`` so logging can
    never crash a run.
    """

    def __init__(self, path: PathLike) -> None:
        """Open (create) the JSONL log file for appending.

        Args:
            path: Destination ``.jsonl`` path; parent dirs are created.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields: Any) -> None:
        """Write a single event record as one JSON line.

        Args:
            event: Short event name (e.g. ``"epoch_end"``).
            **fields: Arbitrary structured fields to attach to the record.
        """
        record: dict[str, Any] = {"ts": _utc_now_iso(), "event": event}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _git_commit(cwd: PathLike | None = None) -> str | None:
    """Return the short git commit hash, or ``None`` if unavailable.

    Best-effort: swallows any error (not a repo, git missing, timeout).

    Args:
        cwd: Working directory to run ``git`` in.

    Returns:
        Short commit hash string, or ``None``.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            commit = result.stdout.strip()
            return commit or None
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - env dependent
        return None
    return None


def run_context(
    *,
    seed: int,
    timestamp: str,
    config_hash: str | None = None,
    extra: dict[str, Any] | None = None,
    cwd: PathLike | None = None,
) -> dict[str, Any]:
    """Build an environment snapshot dict for reproducibility.

    The ``timestamp`` is passed in by the caller so this function does not read
    the wall clock implicitly. Versions are probed lazily and degrade to
    ``None`` if a package is not importable.

    Args:
        seed: The run's base seed.
        timestamp: Caller-provided UTC timestamp string (see :func:`_utc_now_iso`).
        config_hash: Optional config hash string (see ``config.config_hash``).
        extra: Optional additional key/values to merge in.
        cwd: Working directory for the git probe.

    Returns:
        A JSON-serializable dict describing the runtime environment.
    """
    try:
        import numpy as np

        numpy_version: str | None = np.__version__
    except ImportError:  # pragma: no cover
        numpy_version = None

    try:
        import torch

        torch_version: str | None = torch.__version__
        cuda_available = bool(torch.cuda.is_available())
    except ImportError:  # pragma: no cover
        torch_version = None
        cuda_available = False

    context: dict[str, Any] = {
        "python_version": platform.python_version(),
        "torch_version": torch_version,
        "numpy_version": numpy_version,
        "cuda_available": cuda_available,
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "git_commit": _git_commit(cwd),
        "seed": seed,
        "config_hash": config_hash,
        "timestamp": timestamp,
    }
    if extra:
        context.update(extra)
    return context
