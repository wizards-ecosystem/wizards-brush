"""The job-worker mechanics: cooperative cancel/skip, error isolation, DB-ordered
picking, and the ProgressHub backlog-eviction invariant. All torch-free — a fake
handler stands in for a real generator."""
from __future__ import annotations

import asyncio
from pathlib import Path

from backend.app import db
from backend.app import queue as qmod
from backend.app.queue import JobQueue, ProgressHub, _progress_stage


def _mk(kind="image_local", **kw):
    return db.create_job(kind, {"prompt": "q"}, **kw)


# ---- ProgressHub eviction -------------------------------------------------
def test_hub_preserves_terminal_event_behind_preview_backlog():
    """A done/error event must survive even when the client's queue is already
    full of running/preview frames."""
    hub = ProgressHub()
    q: asyncio.Queue = asyncio.Queue(maxsize=3)
    hub._subs.add(q)
    for i in range(3):  # fill with high-frequency progress frames
        hub._emit({"type": "job", "id": 1, "status": "running", "progress": i / 10})
    hub._emit({"type": "job", "id": 1, "status": "done", "result": {}})
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert any(e["status"] == "done" for e in drained)


def test_hub_drops_progress_frame_rather_than_evicting_terminal():
    hub = ProgressHub()
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    hub._subs.add(q)
    hub._emit({"type": "job", "id": 1, "status": "done", "result": {}})
    for i in range(5):  # a flood of previews arriving after the terminal event
        hub._emit({"type": "job", "id": 1, "status": "running", "progress": i / 10})
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    assert any(e["status"] == "done" for e in drained)  # terminal never lost


def test_progress_stage_ignores_step_and_tile_counters():
    assert _progress_stage("step 1/40") == _progress_stage("step 39/40")
    assert _progress_stage("upscaling tile 2/16") == _progress_stage("upscaling tile 15/16")
    assert _progress_stage("loading model") != _progress_stage("step 1/40")


def test_worker_throttles_counter_only_live_frames(client, no_queue, monkeypatch):
    lane = JobQueue("test")
    job = _mk()
    events: list[dict[str, object]] = []
    monkeypatch.setattr(qmod.hub, "emit", events.append)
    monkeypatch.setattr(qmod.time, "monotonic", lambda: 10.0)

    def handler(job_id, params, cb):
        for step in range(20):
            cb((step + 1) / 20, f"step {step + 1}/20")
        return {"asset_ids": []}

    asyncio.run(lane._execute(job.id, handler, "image_local"))
    running = [event for event in events if event.get("status") == "running"]
    # Initial lifecycle frame + one immediate denoising-stage frame, not 20.
    assert len(running) == 2


# ---- lane picking ---------------------------------------------------------
def test_pick_returns_db_order_and_drops_canceled(client, no_queue):
    a, b = _mk(), _mk()
    db.update_job(a.id, priority=5)  # a should sort ahead of b
    lane = JobQueue("test")
    lane._pending[a.id] = (lambda *x: {}, "image_local")
    lane._pending[b.id] = (lambda *x: {}, "image_local")
    assert lane._pick() == a.id

    db.update_job(a.id, status="canceled")
    qmod._CANCELLED.add(a.id)
    assert lane._pick() == b.id  # canceled job skipped
    assert a.id not in lane._pending  # and reaped from the pending map
    assert a.id not in qmod._CANCELLED  # and from the cancel set


# ---- _execute lifecycle ---------------------------------------------------
def test_execute_marks_done_and_stores_result(client, no_queue):
    lane = JobQueue("test")
    job = _mk()

    def handler(job_id, params, cb):
        cb(0.5, "half")
        return {"ok": True, "asset_ids": []}

    asyncio.run(lane._execute(job.id, handler, "image_local"))
    j = db.get_job(job.id)
    assert j.status == "done"
    assert j.result.get("ok") is True
    assert lane.current is None  # released in finally


def test_execute_cancel_marks_canceled_and_cleans_up(client, no_queue):
    lane = JobQueue("test")
    job = _mk()

    def handler(job_id, params, cb):
        qmod._CANCELLED.add(job_id)  # cancel arrives while running
        cb(0.1, "tick")  # progress_cb should raise CancelledJob here
        raise AssertionError("handler continued past a cancel")

    asyncio.run(lane._execute(job.id, handler, "image_local"))
    assert db.get_job(job.id).status == "canceled"
    assert job.id not in qmod._CANCELLED  # cleaned up in finally


def test_cancel_preserves_outputs_completed_earlier_in_the_batch(client, no_queue):
    lane = JobQueue("test")
    job = _mk()

    def handler(job_id, params, cb):
        db.add_asset("image", Path("partial-cancel.png"), job_id=job_id)
        qmod._CANCELLED.add(job_id)
        cb(0.6, "next item")
        raise AssertionError("handler continued past cancel")

    asyncio.run(lane._execute(job.id, handler, "image_local"))
    result = db.get_job(job.id).result
    assert result == {"asset_ids": [db.asset_ids_for_job(job.id)[0]], "partial": True}


def test_execute_skip_marks_single_item_skipped(client, no_queue):
    lane = JobQueue("test")
    job = _mk()

    def handler(job_id, params, cb):
        qmod._SKIPPED.add(job_id)
        cb(0.1, "tick")  # raises SkipItem
        raise AssertionError("handler continued past a skip")

    asyncio.run(lane._execute(job.id, handler, "image_local"))
    j = db.get_job(job.id)
    assert j.status == "canceled"
    assert j.message == "skipped"
    assert job.id not in qmod._SKIPPED


def test_execute_isolates_handler_error(client, no_queue):
    lane = JobQueue("test")
    job = _mk()

    def handler(job_id, params, cb):
        raise RuntimeError("boom")

    asyncio.run(lane._execute(job.id, handler, "image_local"))
    j = db.get_job(job.id)
    assert j.status == "error"
    assert "boom" in (j.error or "")


def test_remote_transient_failure_retries_once_and_reports_it(client, no_queue, monkeypatch):
    lane = JobQueue("remote")
    job = _mk(kind="image_a100")
    calls = 0
    events: list[dict[str, object]] = []
    monkeypatch.setattr(qmod, "TRANSIENT_RETRY_DELAY", 0.0)
    monkeypatch.setattr(qmod.hub, "emit", events.append)

    def handler(job_id, params, cb):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Colab unreachable: tunnel reset")
        return {"ok": True, "asset_ids": []}

    asyncio.run(lane._execute(job.id, handler, "image_a100"))
    assert calls == 2
    assert db.get_job(job.id).status == "done"
    retry = next(event for event in events if event.get("retrying"))
    assert retry["attempt"] == 2
    assert retry["max_attempts"] == 2


def test_remote_fatal_failure_is_never_retried(client, no_queue, monkeypatch):
    lane = JobQueue("remote")
    job = _mk(kind="image_a100")
    calls = 0
    monkeypatch.setattr(qmod, "TRANSIENT_RETRY_DELAY", 0.0)

    def handler(job_id, params, cb):
        nonlocal calls
        calls += 1
        raise RuntimeError("403 gated repository")

    asyncio.run(lane._execute(job.id, handler, "image_a100"))
    assert calls == 1
    assert db.get_job(job.id).status == "error"


def test_remote_transient_failure_does_not_duplicate_partial_output(client, no_queue, monkeypatch):
    lane = JobQueue("remote")
    job = _mk(kind="image_a100")
    calls = 0
    monkeypatch.setattr(qmod, "TRANSIENT_RETRY_DELAY", 0.0)

    def handler(job_id, params, cb):
        nonlocal calls
        calls += 1
        db.add_asset("image", Path("partial-before-link-loss.png"), job_id=job_id)
        raise RuntimeError("Colab connection lost")

    asyncio.run(lane._execute(job.id, handler, "image_a100"))
    assert calls == 1
    saved = db.get_job(job.id)
    assert saved.status == "error"
    assert saved.result["partial"] is True


def test_error_preserves_outputs_completed_earlier_in_the_batch(client, no_queue):
    lane = JobQueue("test")
    job = _mk()

    def handler(job_id, params, cb):
        db.add_asset("image", Path("partial-error.png"), job_id=job_id)
        raise RuntimeError("item two failed")

    asyncio.run(lane._execute(job.id, handler, "image_local"))
    result = db.get_job(job.id).result
    assert result["asset_ids"] == db.asset_ids_for_job(job.id)
    assert result["partial"] is True
