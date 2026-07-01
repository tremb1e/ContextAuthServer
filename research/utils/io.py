"""Filesystem IO helpers built on the standard library, pandas and pyarrow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Union

import pandas as pd

PathLike = Union[str, Path]


def ensure_dir(path: PathLike) -> Path:
    """Create ``path`` (and parents) as a directory if it does not exist.

    Args:
        path: Directory path to create.

    Returns:
        The resolved :class:`~pathlib.Path`.
    """
    resolved = Path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def read_json(path: PathLike) -> Any:
    """Read and parse a UTF-8 JSON file.

    Args:
        path: Path to the JSON file.

    Returns:
        The parsed JSON object.
    """
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: PathLike, obj: Any, *, sort_keys: bool = True, indent: int | None = 2) -> Path:
    """Serialize ``obj`` to a UTF-8 JSON file, creating parent dirs.

    Args:
        path: Destination path.
        obj: JSON-serializable object.
        sort_keys: Whether to sort object keys (deterministic output).
        indent: Indentation level; ``None`` writes compact JSON.

    Returns:
        The destination :class:`~pathlib.Path`.
    """
    destination = Path(path)
    ensure_dir(destination.parent)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False, sort_keys=sort_keys, indent=indent)
    return destination


def read_parquet(path: PathLike) -> pd.DataFrame:
    """Read a parquet file into a pandas DataFrame (pyarrow engine).

    Args:
        path: Path to the parquet file.

    Returns:
        The loaded DataFrame.
    """
    return pd.read_parquet(path, engine="pyarrow")


def write_parquet(path: PathLike, frame: pd.DataFrame) -> Path:
    """Write a DataFrame to parquet (pyarrow engine), creating parent dirs.

    Args:
        path: Destination path.
        frame: DataFrame to serialize.

    Returns:
        The destination :class:`~pathlib.Path`.
    """
    destination = Path(path)
    ensure_dir(destination.parent)
    frame.to_parquet(destination, engine="pyarrow", index=False)
    return destination
