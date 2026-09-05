"""Seed resolution. Avoids Math.random-style nondeterminism in our own code:
when no seed is given we derive one from the OS entropy pool via `secrets`.
"""
from __future__ import annotations

import secrets

_SEED_MAX = 2**32 - 1


def resolve_seed(seed: int | None) -> int:
    """Resolve a user seed. A negative value (or None) means "random" — note the UI
    sends -1 for random; an explicit 0 is a valid fixed seed. Result is clamped to
    the [0, 2**32-1] range torch generators accept."""
    try:
        s = int(seed) if seed is not None else -1
    except (TypeError, ValueError):
        s = -1
    if s < 0:
        return secrets.randbelow(_SEED_MAX)
    return min(s, _SEED_MAX)
