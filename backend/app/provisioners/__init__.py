"""Provisioners: how remote GPU hardware comes to exist.

Adding one is a module and a row here, deliberately the same shape as
`variants.py` and `routers/tools.py` — the conventions this project already uses
for "a catalogue with one source of truth".

The default is `manual`, and an unknown or unimportable name falls back to it
rather than failing: a provisioner is an optional convenience, and no
misconfiguration of one may stop a local-first app from starting.
"""
from __future__ import annotations

from .. import log
from ..config import settings
from .base import ERROR, READY, STARTING, STOPPING, Provisioner, ProvisionerError, Session
from .manual import ManualProvisioner
from .store import SessionStore

logger = log.get("provisioners")

__all__ = [
    "ERROR", "READY", "STARTING", "STOPPING",
    "Provisioner", "ProvisionerError", "Session", "SessionStore",
    "available", "get_provisioner",
]

_STORE = SessionStore()


def _build(name: str) -> Provisioner:
    if name == "runpod":
        from .runpod import RunpodProvisioner

        return RunpodProvisioner(_STORE)
    return ManualProvisioner()


def available() -> tuple[str, ...]:
    return ("manual", "runpod")


def get_provisioner() -> Provisioner:
    name = (settings.remote_gpu_provisioner or "manual").strip().lower()
    if name not in available():
        logger.warning("unknown REMOTE_GPU_PROVISIONER %r; using manual", name)
        return ManualProvisioner()
    try:
        return _build(name)
    except Exception as exc:  # noqa: BLE001 — a broken provisioner must not stop the app
        logger.warning("provisioner %s could not be loaded (%s); using manual", name, exc)
        return ManualProvisioner()
