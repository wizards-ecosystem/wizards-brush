"""Start and stop rented Remote GPU hardware.

Three routes, one invariant: **nothing here runs without a user asking.** There
is no timer, no start-on-launch, and no "provision because a job is queued".
An app that rents a GPU on its own is an app that surprises you with a bill.

The status route deliberately joins two sources. The provisioner knows whether
the hardware exists; `remote_gpu_client` knows whether the worker on it answers.
"Ready" to a user means both, and reporting either alone is how a UI ends up
claiming a GPU is usable while the model is still loading.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import provisioners
from ..config import settings
from ..provisioners import ProvisionerError, Session
from ..remote_gpu_client import remote_gpu_status
from .common import error_responses

router = APIRouter(prefix="/remote-gpu", tags=["remote gpu"])


class SessionRead(BaseModel):
    """What the UI needs to render the control and the running cost."""

    provisioner: str
    label: str
    can_provision: bool          # this install may rent hardware at all
    running: bool
    state: str = "off"           # off | starting | ready | stopping | error
    detail: str = ""
    pod_id: str = ""
    base_url: str = ""
    gpu: str = ""
    elapsed_s: float = 0.0
    hourly_usd: float | None = None
    cost_estimate_usd: float | None = None
    worker_connected: bool = False


async def _read(session: Session | None) -> SessionRead:
    provisioner = provisioners.get_provisioner()
    base = SessionRead(
        provisioner=provisioner.id,
        label=provisioner.label,
        can_provision=provisioner.id != "manual" and provisioner.configured(),
        running=session is not None,
    )
    if session is None:
        return base
    # The worker is a separate question from the pod. Asking it here keeps the
    # UI honest about the gap between "hardware exists" and "it can take work".
    worker = await remote_gpu_status()
    connected = bool(worker.get("connected"))
    state = session.state
    if state == provisioners.READY and not connected:
        state = provisioners.STARTING
    now = time.time()
    return base.model_copy(update={
        "state": state,
        "detail": session.detail if connected or state != provisioners.READY else "",
        "pod_id": session.id,
        "base_url": session.base_url,
        "gpu": session.gpu,
        "elapsed_s": round(session.elapsed_s(now), 1),
        "hourly_usd": session.hourly_usd,
        "cost_estimate_usd": session.cost_estimate_usd(now),
        "worker_connected": connected,
    })


@router.get("/session", response_model=SessionRead,
            responses=error_responses(503))
async def get_session() -> SessionRead:
    """What is running right now, and what it has cost so far."""
    provisioner = provisioners.get_provisioner()
    try:
        session = provisioner.status()
    except ProvisionerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return await _read(session)


@router.post("/session", response_model=SessionRead,
             responses=error_responses(400, 409, 503))
async def start_session() -> SessionRead:
    """Rent hardware. **Billable**, and only ever from an explicit user action."""
    provisioner = provisioners.get_provisioner()
    try:
        session = provisioner.start()
    except ProvisionerError as exc:
        # 409 for "already running", which is a state problem the user can fix,
        # not a failure of the request.
        code = 409 if "already running" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    # Point the app at it immediately: the URL is deterministic from the pod id,
    # so there is nothing to wait for, and a crash before the next poll still
    # leaves a configured, stoppable session.
    if session.base_url:
        data = settings.load_overrides()
        data["remote_gpu_base_url"] = session.base_url
        settings.save_overrides(data)
    return await _read(session)


@router.delete("/session", response_model=SessionRead,
               responses=error_responses(503))
async def stop_session() -> SessionRead:
    """Terminate the rental. Safe to call when nothing is running."""
    provisioner = provisioners.get_provisioner()
    try:
        provisioner.stop()
    except ProvisionerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    data = settings.load_overrides()
    if data.pop("remote_gpu_base_url", None) is not None:
        settings.save_overrides(data)
    return await _read(None)
