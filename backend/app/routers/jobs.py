"""Job listing, detail, cancel/skip, queue reordering, and the live WebSocket stream."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .. import db, queue
from ..models import JobRead, JobStatus
from ..queue import cancel as cancel_job
from ..queue import hub

router = APIRouter(tags=["jobs"])


@router.get("/jobs")
async def list_jobs(limit: int = 100, status: str | None = None) -> list[JobRead]:
    return [JobRead.of(j) for j in db.list_jobs(limit=limit, status=status)]


@router.get("/jobs/eta")
async def queue_eta() -> dict:
    """Remaining time per lane, and per queued job.

    Honest by construction: a job with too little comparable history reports
    `confidence: "estimating"` and no number, rather than a confident guess.
    Model swaps are folded in, because an estimate that ignores a 90-second
    reload is a promise that will be broken.
    """
    from .. import eta
    from ..queue import LANES, lane_for

    resident = _resident_model()
    out: dict[str, dict] = {}
    for lane in LANES:
        kinds = {k for k in _all_kinds() if lane_for(k) == lane}
        jobs = [j for j in db.list_jobs(limit=200)
                if j.kind in kinds
                and j.status in (JobStatus.queued.value, JobStatus.running.value)]
        jobs.sort(key=lambda j: (j.status != JobStatus.running.value,
                                 -(j.priority or 0), j.id or 0))
        total = eta.queue_total(jobs, resident_model=resident)
        out[lane] = {
            "seconds": total.seconds,
            "human": eta.humanize(total.seconds),
            "confidence": total.confidence,
            "jobs": {
                str(j.id): _job_estimate(j, resident)
                for j in jobs if j.id is not None
            },
        }
    return {"lanes": out}


def _job_estimate(job, resident: str | None) -> dict:
    from .. import eta

    est = (eta.for_running(job) if job.status == JobStatus.running.value
           else eta.for_queued(job, resident_model=resident))
    return {"seconds": est.seconds, "human": eta.humanize(est.seconds),
            "confidence": est.confidence, "includes_swap": est.includes_swap}


def _resident_model() -> str | None:
    try:
        from ..generators.local_image import resident_model

        return resident_model()
    except Exception:  # noqa: BLE001 — an ETA must never raise into a request
        return None


@router.get("/jobs/next")
async def next_picks() -> dict:
    """Which job each lane will actually run next, and why.

    The Queue page lists jobs in queue order — priority then age — because that
    is the order the user controls. But the local lane may run a *later* job
    first when it needs the model already loaded, and silently reordering a list
    that displays an order is how that optimisation goes wrong.

    So the UI marks the job that will actually run next, with the reason when it
    is not the head. Predictive rather than retrospective: you can see what is
    about to happen and re-prioritise if you disagree.
    """
    from ..queue import next_picks as lane_picks

    # The lane answers both halves itself: which job, and whether that is a
    # departure from the displayed order. Recomputing the head here would mean
    # asking a different question (every queued row) than the one the lane
    # answered (its own pending set).
    return {"lanes": lane_picks()}


@router.get("/jobs/{job_id}")
async def get_job(job_id: int) -> JobRead:
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return JobRead.of(j)


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: int) -> dict:
    if not db.get_job(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    # `changed` lets the caller tell "canceled it" from "nothing to cancel"
    # (already finished/failed) instead of always seeing ok:true.
    return {"ok": True, "changed": cancel_job(job_id), "id": job_id}


@router.post("/jobs/{job_id}/skip")
async def skip(job_id: int) -> dict:
    """Skip the current item of a running batch job (single-item jobs end as skipped)."""
    if not db.get_job(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True, "changed": queue.skip(job_id), "id": job_id}


@router.post("/jobs/{job_id}/front")
async def run_next(job_id: int) -> dict:
    """Bump a queued job to the front of its lane."""
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.queued.value:
        raise HTTPException(status_code=409, detail="job is no longer queued")
    db.update_job(job_id, priority=db.max_queued_priority() + 1)
    queue.wake(job.kind)
    return {"ok": True, "changed": True, "id": job_id}


class MoveReq(BaseModel):
    dir: str  # "up" | "down"


@router.post("/jobs/{job_id}/move")
async def move(job_id: int, req: MoveReq) -> dict:
    """Swap a queued job with its neighbor in run order (within its lane)."""
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.queued.value:
        raise HTTPException(status_code=409, detail="job is no longer queued")
    lane = queue.lane_for(job.kind)
    kinds = {k for k in _all_kinds() if queue.lane_for(k) == lane}
    ordered = db.queued_jobs_for_lane(kinds)
    idx = next((i for i, j in enumerate(ordered) if j.id == job_id), None)
    if idx is None:
        raise HTTPException(status_code=409, detail="job left the queue")
    swap = idx - 1 if req.dir == "up" else idx + 1
    if swap < 0 or swap >= len(ordered):
        return {"ok": True, "changed": False, "id": job_id}  # already at the edge — no-op
    order = list(ordered)
    order[idx], order[swap] = order[swap], order[idx]
    # Renumber the lane descending so the new order is unambiguous.
    n = len(order)
    for pos, j in enumerate(order):
        assert j.id is not None
        db.update_job(j.id, priority=n - pos)
    queue.wake(job.kind)
    return {"ok": True, "changed": True, "id": job_id}


def _all_kinds() -> set[str]:
    from ..models import JobKind

    return {k.value for k in JobKind}


@router.post("/jobs/{job_id}/rerun")
async def rerun(job_id: int, reseed: bool = False) -> dict:
    """Re-run a finished job with its exact params. reseed=True picks a fresh seed
    ('generate more'); reseed=False reproduces it exactly ('retry'). The original
    upload (img2img/inpaint/i2v) is reused from the saved params — no re-upload.

    A Retry is a replacement attempt, so once its new job has been accepted the
    failed row is moved out of the live queue ledger.  Generate more deliberately
    keeps the source row: it is a creative branch, not a correction of a failure.
    We only supersede after submit succeeds so a transient enqueue error never
    hides the useful failure diagnosis.
    """
    from ..utils.seeds import resolve_seed
    from .common import get_handler, model_key_for, submit

    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    handler = get_handler(job.kind)
    if not handler:
        raise HTTPException(status_code=400, detail=f"kind '{job.kind}' cannot be re-run")
    params = dict(job.params)
    if reseed and "seed" in params:
        params["seed"] = resolve_seed(None)
    current_model = model_key_for(job.kind, params)
    model_warning = ""
    if job.model_key and current_model and job.model_key != current_model:
        model_warning = (
            f"Model slot changed: the original resolved to {job.model_key}, but it now resolves "
            f"to {current_model}. This rerun follows the saved slot name. Cancel and Edit the "
            "queued job if that is not what you want."
        )
    result = await submit(job.kind, params, handler)
    if model_warning:
        result["model_warning"] = model_warning
    if not reseed and job.status == JobStatus.error.value:
        db.update_job(job_id, status=JobStatus.canceled.value,
                      message=f"superseded by retry #{result['job_id']}")
        # update_job is intentionally silent; notify connected queue views so
        # the failed row disappears immediately instead of on their next poll.
        hub.emit({"type": "job", "id": job_id, "kind": job.kind,
                  "status": JobStatus.canceled.value})
        result["replaced_job_id"] = job_id
    return result


@router.websocket("/jobs/ws")
async def jobs_ws(ws: WebSocket) -> None:
    # API-token auth (when set) is enforced for all /api websockets by
    # WSAuthMiddleware in main.py — this endpoint is only reached when allowed.
    await ws.accept()
    q = hub.subscribe()
    try:
        # Send a snapshot of recent jobs on connect.
        for j in db.list_jobs(limit=20):
            await ws.send_json({"type": "job", "id": j.id, "kind": j.kind, "status": j.status,
                                "progress": j.progress, "message": j.message})
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=30)
                # A binary frame (a preview) goes out as bytes; everything else
                # is JSON. See app/wsframe.py for the frame layout.
                frame = event.get("__binary__") if isinstance(event, dict) else None
                if frame is not None:
                    await ws.send_bytes(frame)
                else:
                    await ws.send_json(event)
            except TimeoutError:
                await ws.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass  # a client closing the tab is ordinary; `finally` still unsubscribes
    finally:
        hub.unsubscribe(q)
