"""The default: you provide the hardware, the app never spends anything.

This is not a stub. It is the shipped behaviour and the reason the feature is
safe to have at all — a fork that never sets `REMOTE_GPU_PROVISIONER` gets an
app that cannot create a billable resource even if something else is misconfigured.
"""
from __future__ import annotations

from .base import Provisioner, ProvisionerError, Session


class ManualProvisioner:
    id = "manual"
    label = "Self-hosted"

    def configured(self) -> bool:
        return True

    def start(self) -> Session:
        raise ProvisionerError(
            "This install is set to manage its own Remote GPU. Start the worker on your "
            "hardware and paste its URL into Settings, or set REMOTE_GPU_PROVISIONER=runpod "
            "in .env to let the app rent one. See docs/remote-gpu.md."
        )

    def status(self) -> Session | None:
        return None

    def adopt(self) -> Session | None:
        return None

    def stop(self) -> None:
        return None


_ = Provisioner
