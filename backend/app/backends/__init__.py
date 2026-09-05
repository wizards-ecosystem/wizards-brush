"""Backend registry: the places jobs can run.

One module-level registry rather than dependency injection. There are two
backends, they are decided by configuration at import time, and threading a
container through the routers to swap them in tests would be more machinery than
the problem has.
"""
from __future__ import annotations

from .base import Backend, BackendKind, Health, ModelInfo, RemoteBackend
from .local import LocalBackend
from .remote_gpu import RemoteGPUBackend

LOCAL = LocalBackend()
REMOTE_GPU = RemoteGPUBackend()

_REGISTRY: dict[str, Backend] = {LOCAL.id: LOCAL, REMOTE_GPU.id: REMOTE_GPU}


def get(backend_id: str) -> Backend | None:
    return _REGISTRY.get(backend_id)


def all_backends() -> list[Backend]:
    return list(_REGISTRY.values())


def register(backend: Backend) -> None:
    """Add a backend. The extension point for another worker or a LAN box."""
    _REGISTRY[backend.id] = backend


def health_report() -> dict[str, Health]:
    """Every backend's health, for the settings page and the startup banner.

    A backend whose probe raises reports as disconnected with the reason rather
    than taking the whole report down — one unreachable machine must not hide
    the state of the others.
    """
    out: dict[str, Health] = {}
    for b in all_backends():
        try:
            out[b.id] = b.health()
        except Exception as e:  # noqa: BLE001 — one bad backend must not hide the rest
            out[b.id] = Health(connected=False, reason=str(e))
    return out


def has_feature(feature: str, *, backend_id: str = "remote_gpu",
                configured: bool = True) -> bool:
    """Whether a backend can do something, treating silence as "unknown".

    The rule that matters: an empty feature set means we have not heard from the
    backend, NOT that it lacks the feature. In that case fall back to what is
    configured, so a not-yet-connected session does not hide controls the user
    is about to need. Once a backend has actually reported, believe it.
    """
    b = get(backend_id)
    if b is None:
        return configured
    feats = b.features()
    if not feats:
        return configured
    return feature in feats


__all__ = ["LOCAL", "REMOTE_GPU", "Backend", "BackendKind", "Health", "ModelInfo",
           "RemoteBackend", "all_backends", "get", "has_feature",
           "health_report", "register"]
