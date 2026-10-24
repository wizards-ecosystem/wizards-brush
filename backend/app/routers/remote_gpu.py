"""Start and stop rented Remote GPU hardware.

Four routes, one invariant: **nothing here runs without a user asking.** There
is no timer, no start-on-launch, and no "provision because a job is queued".
An app that rents a GPU on its own is an app that surprises you with a bill.

The status route deliberately joins two sources. The provisioner knows whether
the hardware exists; `remote_gpu_client` knows whether the worker on it answers.
"Ready" to a user means both, and reporting either alone is how a UI ends up
claiming a GPU is usable while the model is still loading.

Stopping inference and stopping hardware are different things. Canceling a job
never ends a rental; only `DELETE /session` does, and it reports a stop that
Runpod did not confirm as such (`stop_unconfirmed` or `stop_failed`) with the
session kept, because the pod may still be billing.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .. import provisioners
from ..config import settings
from ..problems import RequestError
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
    # off | starting | ready | stopping | stop_unconfirmed | stop_failed | error
    state: str = "off"
    detail: str = ""
    pod_id: str = ""
    base_url: str = ""
    gpu: str = ""
    elapsed_s: float = 0.0
    hourly_usd: float | None = None
    cost_estimate_usd: float | None = None
    worker_connected: bool = False
    # True while recorded hardware may still be billing: running, or a stop
    # that was not confirmed. Costs are estimates either way.
    possibly_billing: bool = False
    stop_error: str = ""
    stop_attempts: int = 0
    # Unix seconds of Runpod's last answer about this session, and why the
    # latest question went unanswered. 0 means never observed by this process.
    observed_at: float = 0.0
    observe_error: str = ""


class ForgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pod_id: str = Field(min_length=1, max_length=128,
                        description="The recorded pod id, typed back as confirmation.")


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
        "possibly_billing": state != provisioners.ERROR or session.unresolved_stop,
        "stop_error": session.stop_error,
        "stop_attempts": session.stop_attempts,
        "observed_at": session.observed_at,
        "observe_error": session.observe_error,
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
        # 409 for a session that already exists or whose stop is unresolved:
        # a state problem the user can fix, not a failure of the request.
        if exc.code in {"session_exists", "stop_unresolved"}:
            raise RequestError(409, str(exc), code=exc.code, retryable=False) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Point the app at it immediately: the URL is deterministic from the pod id,
    # so there is nothing to wait for, and a crash before the next poll still
    # leaves a configured, stoppable session.
    if session.base_url:
        data = settings.load_overrides()
        data["remote_gpu_base_url"] = session.base_url
        settings.save_overrides(data)
    return await _read(session)


@router.delete("/session", response_model=SessionRead,
               responses=error_responses(502))
async def stop_session() -> SessionRead:
    """Terminate the rental. Safe to call when nothing is running.

    A stop Runpod does not confirm answers 502 with `code` `stop_unconfirmed`
    (the request may have landed) or `stop_failed` (it was refused). The
    session is kept in `state`, and calling this again checks with Runpod
    before it repeats the termination.
    """
    provisioner = provisioners.get_provisioner()
    try:
        provisioner.stop()
    except ProvisionerError as exc:
        # Keep the Remote GPU URL as well: the worker may still be answering.
        view = await _read(provisioner.status())
        raise RequestError(
            502, str(exc), code=exc.code if exc.code.startswith("stop_") else "stop_failed",
            retryable=True, state=view.model_dump(),
            suggestions=["Retry Stop; it asks Runpod whether the pod still exists first.",
                         "Check the pod in the Runpod console."],
        ) from exc
    _drop_remote_url()
    return await _read(None)


@router.post("/session/forget", response_model=SessionRead,
             responses=error_responses(400, 404))
async def forget_session(body: ForgetBody) -> SessionRead:
    """Drop the recorded session without contacting the provider.

    For when the pod is confirmed gone some other way (the Runpod console) and
    Stop cannot confirm it. Afterwards the app can no longer stop that pod, so
    the request must name it.
    """
    provisioner = provisioners.get_provisioner()
    if not provisioner.forget(body.pod_id.strip()):
        raise RequestError(
            404, "no recorded session has that pod id", code="not_found", field="pod_id",
            suggestions=["Read GET /api/remote-gpu/session and send its pod_id."],
        )
    _drop_remote_url()
    return await _read(None)


def _drop_remote_url() -> None:
    data = settings.load_overrides()
    if data.pop("remote_gpu_base_url", None) is not None:
        settings.save_overrides(data)
