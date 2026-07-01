"""Deterministic seeding helpers.

``set_seed`` seeds ``random``, ``numpy`` and ``torch`` (CPU + CUDA if present)
for reproducible runs. ``stable_int_seed`` derives a stable 32-bit integer seed
from arbitrary parts via SHA-256, mirroring the HMOG ``stable_int_seed`` idiom
(_recon_hmog.md §5) so every bootstrap / permutation is reproducible.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any


def set_seed(seed: int) -> None:
    """Seed Python ``random``, NumPy and Torch for deterministic behaviour.

    Torch is seeded on CPU (and CUDA if available), but no CUDA presence is
    assumed by the rest of the package.

    Args:
        seed: The base integer seed.
    """
    random.seed(seed)

    try:
        import numpy as np

        np.random.seed(seed % (2**32))
    except ImportError:  # pragma: no cover - numpy is a hard dep in practice
        pass

    try:
        import torch

        torch.manual_seed(seed)
        if torch.cuda.is_available():  # pragma: no cover - CPU-only in tests
            torch.cuda.manual_seed_all(seed)
    except ImportError:  # pragma: no cover - torch is a hard dep in practice
        pass


def stable_int_seed(*parts: Any) -> int:
    """Derive a stable, deterministic 32-bit int seed from arbitrary parts.

    The parts are stringified and joined, then hashed with SHA-256. The first
    8 hex digits are interpreted as an unsigned 32-bit integer. This is stable
    across processes and machines (unlike Python's salted ``hash``).

    Args:
        *parts: Any values identifying the seed context (ints, strs, ...).

    Returns:
        A non-negative integer in ``[0, 2**32)`` suitable for ``default_rng``.
    """
    joined = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)
