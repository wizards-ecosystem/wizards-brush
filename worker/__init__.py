"""The Wizard's Brush remote GPU worker.

Deployed as a container image. The app talks to it over HTTPS with a shared
secret; where that HTTPS comes from is the host's business, not this package's.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent


def build_id() -> str:
    """Fingerprint of the worker's own source.

    /health reports it so the app can say when a deployed worker is running
    older code than the checkout talking to it — a stale worker otherwise looks
    healthy while contradicting the source in front of you.

    Hashes the package's sources in a fixed order and nothing else, so it never
    depends on configuration and can never leak a secret.
    """
    digest = hashlib.sha256()
    for path in sorted(_PACKAGE.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]
