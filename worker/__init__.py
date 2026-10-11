"""The Wizard's Brush remote GPU worker.

Deployed as a container image. The app talks to it over HTTPS with a shared
secret; where that HTTPS comes from is the host's business, not this package's.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent
_REPO = _PACKAGE.parent

# The two build inputs that are not Python. In a checkout they live beside the
# package; the Dockerfile copies them INTO it, so an image fingerprints itself
# from the same bytes without knowing the repository's layout.
_SIDE_INPUTS = (
    ("requirements.lock", (_PACKAGE / "requirements.lock",
                           _REPO / "scripts" / "remote-gpu-requirements.lock")),
    ("Dockerfile", (_PACKAGE / "Dockerfile",
                    _REPO / "docker" / "remote-gpu" / "Dockerfile")),
)


def build_inputs() -> list[tuple[str, Path]]:
    """Everything that decides what an image does, as (label, path), in digest order.

    The package's sources first, then the dependency lock, then the Dockerfile.
    Hashing the sources alone let a lock or torch change keep the old tag, so
    the release workflow found that tag already published and never rebuilt
    it: the code was new and the image serving it was not.
    """
    items = [(path.name, path) for path in sorted(_PACKAGE.glob("*.py"))]
    for label, candidates in _SIDE_INPUTS:
        found = next((c for c in candidates if c.is_file()), None)
        if found is not None:
            items.append((label, found))
    return items


def build_id() -> str:
    """Fingerprint of the worker build.

    /health reports it so the app can say when a deployed worker is running
    older code than the checkout talking to it - a stale worker otherwise looks
    healthy while contradicting the source in front of you.

    Hashes `build_inputs()` in a fixed order and nothing else, so it never
    depends on configuration and can never leak a secret.
    """
    digest = hashlib.sha256()
    for label, path in build_inputs():
        digest.update(label.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]
