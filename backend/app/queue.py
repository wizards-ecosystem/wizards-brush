"""Job lanes + WebSocket progress hub.

Two independent lanes run in parallel:
  • "local"  — local-GPU work (txt2img/img2img/inpaint, upscale, face-restore,
               interpolate, detail). Serialized within the lane.
  • "remote" — Remote GPU work (image, edit, T2V, I2V). Only waits on the network,
               so it runs concurrently with the local lane.

Each lane picks its next job by DB order (priority DESC, id ASC), so queued jobs
can be reordered/prioritized from the UI while they wait.

GPU safety is enforced separately by a shared GPU mutex (see generators.base.GPU_LOCK)
that every GPU op acquires — so local generations never collide even though the lanes
are parallel, while a long Remote GPU job (holding no local GPU) never blocks them.
"""
from __future__ import annotations

import asyncio
import re
import time
import traceback
from collections.abc import Callable
from typing import Any

from . import db, errors, notify, wsframe
from .models import Job, JobStatus
from .params import TrackedParams

# handler(job_id, params, progress_cb) -> result dict.
# progress_cb(frac, message="", *, preview=None) — preview is raw JPEG bytes,
# sent as a binary WebSocket frame (never persisted to the DB, never base64).
ProgressCb = Callable[..., None]
Handler = Callable[[int, dict[str, Any], ProgressCb], dict[str, Any]]


class ProgressHub:
    """Fan-out of job events to all connected WebSocket clients."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    @property
    def has_subscribers(self) -> bool:
        """Whether producing a live-only payload can benefit any client."""
        return bool(self._subs)

    @staticmethod
    def _droppable(event: dict[str, Any]) -> bool:
        """Whether a client behind a backlog may simply miss this event.

        Three kinds are safe to drop: binary preview frames, "running" progress
        frames, and model-load telemetry — all of them are the latest value of
        something that will be resent, not a transition that only happens once.
        Everything else (queued/done/error/canceled) a client must not miss.

        One predicate, used both to decide whether to drop and to decide what may
        be evicted to make room. They have to agree, and when they were written
        out twice they were two places to forget.
        """
        return ("__binary__" in event
                or event.get("type") == "model_load"
                or event.get("status") == JobStatus.running.value)

    def _emit(self, event: dict[str, Any]) -> None:
        droppable = self._droppable(event)
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                if droppable:
                    # Slow client behind a flood of previews: just drop this
                    # frame rather than evicting a queued terminal event.
                    continue
                # Must-deliver event: drain, discard progress frames to make
                # room, and preserve every other queued transition event.
                drained: list[dict[str, Any]] = []
                try:
                    while True:
                        drained.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    pass  # the drain is done; empty is the loop's exit condition
                kept = [e for e in drained if not self._droppable(e)]
                kept.append(event)
                for e in kept[-q.maxsize:]:  # keep newest if an all-transition backlog overflows
                    try:
                        q.put_nowait(e)
                    except asyncio.QueueFull:
                        break

    def emit(self, event: dict[str, Any]) -> None:
        """Safe to call from any thread."""
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._emit, event)
        else:
            self._emit(event)

    def emit_binary(self, frame: bytes) -> None:
        """Send raw bytes to every subscriber.

        Wrapped in a one-key dict rather than queued as bare bytes so the
        existing backpressure logic can tell frames apart from events without
        type-sniffing every item. Binary frames are always droppable: they carry
        previews, and a client behind a flood should skip them rather than lag.
        """
        self.emit({"__binary__": frame})


hub = ProgressHub()

_PROGRESS_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:/\d+)?")


def _progress_stage(message: str) -> str:
    """Stable stage identity for messages whose counters change every tick."""
    return _PROGRESS_NUMBER.sub("#", message).strip().lower()


def load_reporter(job_id: int, kind: str = ""):
    """A LoadReporter whose stages reach the UI as `model_load` events.

    Deliberately its own event type rather than a progress frame: loading is not
    job progress, has no meaningful fraction, and must not move the progress bar.
    Every frame is droppable — a client that missed one heartbeat gets the next
    two seconds later, and the terminal state comes from the job event stream.
    """
    from .generators.base import LoadReporter

    def emit(stage: str, detail: str, extra: dict) -> None:
        hub.emit({"type": "model_load", "id": job_id, "kind": kind,
                  "stage": stage, "detail": detail, **extra})

    return LoadReporter(emit)


def _free_gpu() -> None:
    """Drop resident pipelines and empty the CUDA cache. Best effort.

    Imported lazily and swallowed on failure: this runs on an error path, and a
    recovery that raises would replace a useful message with a useless one.
    """
    try:
        from .generators import local_image

        local_image.free_all()
    except Exception:  # noqa: BLE001 — recovery must never mask the original error
        pass


class CancelledJob(Exception):
    pass


class SkipItem(Exception):
    """Raised inside progress_cb to skip the current item; batch handlers catch it
    and continue with the next item, single-item jobs end as 'skipped'."""


# Cancellation/skip are global (a job id lives in exactly one lane) — the running
# lane sees the flag at its next progress step.
_CANCELLED: set[int] = set()
_SKIPPED: set[int] = set()


# How far past the highest-priority queued job the lane may look for one whose
# model is already loaded.
#
# The number is derived, not picked. A local model swap costs roughly 90 seconds;
# the fastest job (Turbo at the Draft tier) takes roughly 3. Deferring the head
# job behind at most ~30 same-priority jobs therefore costs it about as much as
# the single swap the deferral avoids — so the trade can never be badly wrong in
# either direction. Round up to 32.
AFFINITY_WINDOW = 32

# A remote client already retries individual HTTP operations. This is the one
# wider retry for a failure that escaped that layer (for example, a tunnel that
# vanished between submit and poll). Two total attempts are enough to recover a
# momentary link without turning a flapping remote worker into an infinite queue.
TRANSIENT_ATTEMPTS = 2
TRANSIENT_RETRY_DELAY = 2.0
TRANSIENT_CANCEL_POLL = 0.25


def _resident_for_lane(lane: str) -> str | None:
    """The model id loaded on the device that serves `lane`, or None.

    Two devices, two answers. The local card's is authoritative (we loaded it);
    the A100's comes from its last /health, which is a moment stale — and that
    is fine for the same reason the local read is taken without the load lock:
    a wrong answer costs one avoidable swap, never a wrong result.
    """
    try:
        if lane == "local":
            from .generators.local_image import resident_model

            return resident_model()
        from .generators import variants
        from .remote_gpu_client import last_health

        variant = (last_health() or {}).get("image_variant")
        return variants.resolve(variant, lane="colab") if variant else None
    except Exception:  # noqa: BLE001 — scheduling must not fail because a probe did
        return None


def _affinity_pick(ordered: list[Job]) -> int:
    """Choose from `ordered` (already priority DESC, id ASC) the job to run next.

    Returns the head unless a *same-priority* job within AFFINITY_WINDOW needs
    the model that is already resident, in which case that job runs first and
    saves a full pipeline reload.

    Four properties make this safe to do behind the user's back:

    1. **Priority stays absolute.** The scan stops at the first job of a lower
       priority, so affinity can never promote a job over a more important one.
       Reordering in the UI remains the override, and remains sufficient.
    2. **Starvation is bounded.** The head's position never changes while it
       waits; newly queued warm jobs get larger ids and fall outside the window,
       so it runs after at most AFFINITY_WINDOW swaps-avoided.
    3. **It costs no queries.** The rows are already in hand, and `model_key` was
       resolved when the job was created, so nothing is fetched or parsed here.
    4. **It degrades to plain FIFO.** Nothing resident, a lane with no local
       models, or a queue using one model all take the early return.
    """
    head_job = ordered[0]
    head = int(head_job.id or 0)
    resident = _resident_for_lane(lane_for(head_job.kind))
    if resident is None:
        return head  # nothing loaded: no swap to avoid
    if head_job.model_key in (None, resident):
        return head  # the head already wants what is loaded (or wants nothing)

    for job in ordered[1:1 + AFFINITY_WINDOW]:
        if (job.priority or 0) != (head_job.priority or 0):
            break  # a different priority tier — out of bounds for a tie-break
        if job.model_key == resident and job.id is not None:
            return int(job.id)
    return head


class JobQueue:
    """One serial lane: a single worker draining pending jobs in DB order."""

    def __init__(self, name: str) -> None:
        self.name = name
        # job_id -> (handler, kind). Order of execution comes from the DB, so
        # queued jobs can be re-prioritized while they wait here.
        self._pending: dict[int, tuple[Handler, str]] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._current: int | None = None

    @property
    def current(self) -> int | None:
        return self._current

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        hub.bind_loop(loop)
        if self._task is None:
            self._task = loop.create_task(self._run())

    async def submit(self, job_id: int, handler: Handler, kind: str = "") -> None:
        # A queued job may already be back on this lane because the server is
        # re-starting or a retry path re-used the same job id. Re-submitting the
        # same row must be a no-op rather than duplicating the in-memory queue,
        # which would make the job run twice and can stall/restart the lane.
        if job_id in self._pending:
            self._pending[job_id] = (handler, kind)
            return
        self._pending[job_id] = (handler, kind)
        self._wake.set()
        hub.emit({"type": "job", "id": job_id, "kind": kind,
                  "status": JobStatus.queued.value, "lane": self.name})

    def _pick(self) -> int | None:
        """Next pending job, preferring one whose model is already loaded.

        Drops jobs canceled while queued as a side effect — they are gone from
        the DB's queued set, so they can never be picked again.
        """
        if not self._pending:
            return None
        ordered = db.queued_in_order(list(self._pending.keys()))
        alive = {j.id for j in ordered}
        for jid in list(self._pending.keys()):
            if jid not in alive:  # canceled (or vanished) while waiting
                self._pending.pop(jid, None)
                _CANCELLED.discard(jid)
        if not ordered:
            return None
        return _affinity_pick(ordered)

    def next_pick(self) -> dict[str, Any] | None:
        """What this lane runs next, and why it is not the head. Never blocking.

        Both values come from the same ordered list on purpose. Deriving the head
        from a separate query would compare two differently-scoped things — every
        queued row in the database against this lane's pending map — and report a
        reordering for a disagreement that affinity had nothing to do with.
        """
        if self._current is not None or not self._pending:
            return None
        try:
            ordered = db.queued_in_order(list(self._pending.keys()))
        except Exception:  # noqa: BLE001 — a UI hint must never raise into a request
            return None
        if not ordered:
            return None
        picked = _affinity_pick(ordered)
        head = int(ordered[0].id or 0)
        return {"job_id": picked,
                "reason": "model already loaded" if picked != head else None}

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while True:
                job_id = self._pick()
                if job_id is None:
                    break
                handler, kind = self._pending.pop(job_id)
                try:
                    await self._execute(job_id, handler, kind)
                except Exception as e:  # never let one job kill the worker loop  # noqa: BLE001
                    db.mark_error(job_id, f"worker error: {e}")
                    hub.emit({"type": "job", "id": job_id, "kind": kind,
                              "status": JobStatus.error.value, "error": str(e)})

    async def _execute(self, job_id: int, handler: Handler, kind: str) -> None:
        self._current = job_id
        notify.watcher.job_started()
        db.mark_running(job_id)
        hub.emit({"type": "job", "id": job_id, "kind": kind, "status": JobStatus.running.value,
                  "progress": 0.0, "lane": self.name})

        # Live progress rides the WebSocket; the DB value only backs reconnect
        # snapshots and /jobs polls, so throttle the per-step UPDATE rather than
        # committing on every denoising step. Stage (message) changes always write.
        last_db_ts = [0.0]
        last_db_stage = [""]
        last_emit_ts = [0.0]
        last_emit_stage = [""]

        def progress_cb(frac: float, message: str = "", *,
                        preview: bytes | None = None) -> None:
            if job_id in _CANCELLED:
                raise CancelledJob()
            if job_id in _SKIPPED:
                _SKIPPED.discard(job_id)
                raise SkipItem()
            frac = max(0.0, min(1.0, float(frac)))
            now = time.monotonic()
            stage = _progress_stage(message)
            if now - last_db_ts[0] >= 0.4 or stage != last_db_stage[0]:
                last_db_ts[0], last_db_stage[0] = now, stage
                db.set_progress(job_id, frac, message)
            # A 40-step batch does not need hundreds of whole-store React
            # updates. Stage transitions and previews remain immediate; scalar
            # progress is smooth at 4 fps and cancellation is still checked on
            # every callback above this gate.
            if (now - last_emit_ts[0] >= 0.25
                    or stage != last_emit_stage[0] or preview is not None):
                last_emit_ts[0], last_emit_stage[0] = now, stage
                hub.emit({"type": "job", "id": job_id, "kind": kind,
                          "status": JobStatus.running.value,
                          "progress": frac, "message": message})
            if preview:
                # Sent as raw bytes on the same socket rather than base64 inside
                # the event above: a preview is several KB, arrives many times
                # per generation, and base64 costs 33% plus escaping plus a
                # parse for nothing. See app/wsframe.py.
                hub.emit_binary(wsframe.encode(wsframe.FrameType.preview_jpeg,
                                               job_id, preview))

        job = db.get_job(job_id)
        # Wrapped so the handler's reads are recorded. A control that is declared,
        # rendered and never read is invisible otherwise — see app/params.py.
        params: dict[str, Any] = TrackedParams(job.params if job else {})

        def terminal_result() -> dict[str, Any]:
            """Recover outputs committed before an exceptional terminal state."""
            current = db.get_job(job_id)
            result = dict(current.result if current else {})
            asset_ids = db.asset_ids_for_job(job_id)
            if asset_ids:
                result["asset_ids"] = asset_ids
                result["partial"] = True
            return result

        try:
            for attempt in range(1, TRANSIENT_ATTEMPTS + 1):
                try:
                    result = await asyncio.to_thread(handler, job_id, params, progress_cb)
                    break
                except (CancelledJob, SkipItem):
                    raise
                except Exception as e:
                    failure = errors.classify(e, kind)
                    # Re-running after a handler has committed output risks a
                    # duplicate batch. Likewise, only the remote lane is safe:
                    # a local generation may have mutated resident GPU state
                    # before its network-shaped exception surfaced.
                    may_retry = (
                        failure.is_retryable
                        and self.name == "remote"
                        and attempt < TRANSIENT_ATTEMPTS
                        and not db.asset_ids_for_job(job_id)
                    )
                    if not may_retry:
                        raise

                    retry_message = (
                        f"Connection interrupted — retrying attempt {attempt + 1}/"
                        f"{TRANSIENT_ATTEMPTS}"
                    )
                    db.set_progress(job_id, 0.0, retry_message)
                    hub.emit({"type": "job", "id": job_id, "kind": kind,
                              "status": JobStatus.running.value, "progress": 0.0,
                              "message": retry_message, "retrying": True,
                              "attempt": attempt + 1, "max_attempts": TRANSIENT_ATTEMPTS})

                    # Sleep in small pieces so Cancel remains responsive while
                    # the lane is backing off rather than being ignored until
                    # the next network request begins.
                    deadline = time.monotonic() + TRANSIENT_RETRY_DELAY
                    while time.monotonic() < deadline:
                        if job_id in _CANCELLED:
                            raise CancelledJob() from None
                        await asyncio.sleep(min(
                            TRANSIENT_CANCEL_POLL,
                            max(0.0, deadline - time.monotonic()),
                        ))
            else:  # pragma: no cover — the loop always returns or raises
                raise RuntimeError("transient retry loop exhausted without a result")
            db.mark_done(job_id, result)
            hub.emit({"type": "job", "id": job_id, "kind": kind, "status": JobStatus.done.value,
                      "progress": 1.0, "result": result})
        except CancelledJob:
            result = terminal_result()
            db.mark_canceled(job_id, "canceled", result)
            hub.emit({"type": "job", "id": job_id, "kind": kind,
                      "status": JobStatus.canceled.value, "result": result})
        except SkipItem:
            # Single-item job (batch handlers catch SkipItem themselves).
            result = terminal_result()
            db.mark_canceled(job_id, "skipped", result)
            hub.emit({"type": "job", "id": job_id, "kind": kind,
                      "status": JobStatus.canceled.value, "result": result})
        except Exception as e:  # noqa: BLE001 — surface any handler failure to the UI
            # Two things beyond recording the traceback: say something the user
            # can act on, and put the GPU back in a state where the *next* job
            # has a chance. An OOM leaves the allocator fragmented and the model
            # resident, so without recovery one oversized job makes everything
            # after it fail too — which reads as "the app broke", not "that was
            # too big".
            tip = errors.tip_for(e, kind)
            if errors.clears_gpu(e, kind):
                _free_gpu()
            err = f"{e}\n{traceback.format_exc()}"
            result = terminal_result()
            db.mark_error(job_id, err, tip=tip, result=result)
            hub.emit({"type": "job", "id": job_id, "kind": kind,
                      "status": JobStatus.error.value, "error": str(e), "tip": tip,
                      "result": result})
        finally:
            _CANCELLED.discard(job_id)
            _SKIPPED.discard(job_id)
            self._current = None
            # Arms the idle debounce. If another job starts before it fires,
            # nothing is sent — which is what makes "the queue is finished"
            # mean something other than "there is a gap between two jobs".
            job = db.get_job(job_id)
            notify.watcher.job_finished(
                job_id=job_id, kind=kind,
                status=job.status if job else JobStatus.error.value)


# ---- the two lanes --------------------------------------------------------
LANES: dict[str, JobQueue] = {"local": JobQueue("local"), "remote": JobQueue("remote")}

# Local-GPU/CPU work serializes in the local lane; everything else uses Remote GPU.
_LOCAL_KINDS = {"image_local", "img2img", "inpaint", "outpaint", "upscale",
                "face_restore", "interpolate", "detail", "control_local"}


def lane_for(kind: str) -> str:
    return "local" if kind in _LOCAL_KINDS else "remote"


def start_all() -> None:
    for q in LANES.values():
        q.start()


async def resume_queued() -> int:
    """Put jobs still queued in the DB back onto their lanes after a restart.

    The lanes hold their pending set in memory, so a queued DB row alone is
    inert — it would sit there forever looking queued and never run. This is the
    other half of `reconcile_orphans` leaving those rows alone.

    A kind whose handler is not registered cannot be resumed (its route was
    removed, or the generator is gone). That is resolved to canceled with a
    reason rather than left pending, because a job nothing can ever pick up is
    worse than one that visibly failed.
    """
    from . import db
    from .routers.common import get_handler

    resumed = 0
    for job in db.queued_jobs_for_lane():
        if job.id is None:
            continue
        lane = LANES[lane_for(job.kind)]
        if job.id in lane._pending:
            continue
        handler = get_handler(job.kind)
        if handler is None:
            db.update_job(job.id, status=JobStatus.canceled.value,
                          message=f"cannot resume: no handler for {job.kind}")
            continue
        await lane.submit(int(job.id), handler, job.kind)
        resumed += 1
    return resumed


async def submit(kind: str, job_id: int, handler: Handler) -> None:
    await LANES[lane_for(kind)].submit(job_id, handler, kind)


def wake(kind: str) -> None:
    """Nudge the lane after a reorder so it re-reads DB order."""
    LANES[lane_for(kind)]._wake.set()


def next_picks() -> dict[str, dict[str, Any]]:
    """lane name -> {job_id, reason}, omitting lanes with nothing to run.

    Feeds the Queue page's marker: the list there is in queue order, and this is
    the only thing that says when the lane intends to depart from it.
    """
    return {name: pick for name, q in LANES.items()
            if (pick := q.next_pick()) is not None}


def cancel(job_id: int) -> bool:
    """Cancel a job in either lane. If it's actively running, stop it cooperatively
    at the next progress step; otherwise (queued or orphaned) resolve it in the DB now.
    Returns True if a job was actually stopped/transitioned, False if there was
    nothing to cancel (unknown id or already in a terminal state)."""
    running_here = any(q.current == job_id for q in LANES.values())
    if running_here:
        _CANCELLED.add(job_id)
        return True
    job = db.get_job(job_id)
    if job and job.status in (JobStatus.running.value, JobStatus.queued.value):
        _CANCELLED.add(job_id)
        db.update_job(job_id, status=JobStatus.canceled.value, message="canceled")
        hub.emit({"type": "job", "id": job_id, "status": JobStatus.canceled.value})
        # Nudge the lane so it drops the now-canceled entry from _pending and
        # _CANCELLED promptly instead of leaking until the next submit.
        wake(job.kind)
        return True
    return False  # nothing to cancel — don't leak a flag for a job that won't run


def skip(job_id: int) -> bool:
    """Skip the currently running item of a batch job (or the whole job if single).
    Returns True only if a running job was flagged (skip has no meaning otherwise)."""
    if any(q.current == job_id for q in LANES.values()):
        _SKIPPED.add(job_id)
        return True
    return False
