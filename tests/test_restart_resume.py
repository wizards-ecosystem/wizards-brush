"""A restart must cost the job that was running, not the queue behind it.

`reconcile_orphans` used to cancel running AND queued jobs. A queued job has
done no work — throwing it away discards a queue the user deliberately built,
which is a far worse thing for a restart to cost than the single job that was
genuinely interrupted.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.app import db
from backend.app.models import JobStatus


@pytest.fixture(autouse=True)
def _db():
    db.init_db()


def _mk(kind: str, status: str) -> int:
    job = db.create_job(kind, {"prompt": "x"})
    assert job.id is not None
    db.update_job(job.id, status=status)
    return int(job.id)


def test_running_is_canceled_but_queued_survives():
    running = _mk("image_local", JobStatus.running.value)
    queued = _mk("image_local", JobStatus.queued.value)

    assert db.reconcile_orphans() >= 1

    got_running = db.get_job(running)
    got_queued = db.get_job(queued)
    assert got_running is not None and got_queued is not None
    assert got_running.status == JobStatus.canceled.value
    assert "restart" in (got_running.message or "")
    # The whole point: it is still queued, not canceled.
    assert got_queued.status == JobStatus.queued.value


def test_resume_puts_queued_jobs_back_on_a_lane():
    """A queued DB row alone is inert — the lanes hold their pending set in
    memory, so without this the job looks queued forever and never runs."""
    from backend.app.queue import LANES, lane_for, resume_queued
    from backend.app.routers.common import register_handler

    register_handler("image_local", lambda job_id, params, cb: {"asset_ids": []})
    jid = _mk("image_local", JobStatus.queued.value)
    lane = LANES[lane_for("image_local")]
    lane._pending.pop(jid, None)

    resumed = asyncio.run(resume_queued())
    assert resumed >= 1
    assert jid in lane._pending
    lane._pending.pop(jid, None)


def test_resume_is_idempotent_when_the_lane_already_has_the_job():
    """A duplicate startup or a resumed lane must not enqueue the same row twice."""
    from backend.app.queue import LANES, lane_for, resume_queued
    from backend.app.routers.common import register_handler

    for j in db.list_jobs(limit=200):
        if j.status == JobStatus.queued.value:
            db.update_job(j.id, status=JobStatus.done.value)

    register_handler("image_local", lambda job_id, params, cb: {"asset_ids": []})
    jid = _mk("image_local", JobStatus.queued.value)
    lane = LANES[lane_for("image_local")]
    lane._pending[jid] = (lambda *args: {"asset_ids": []}, "image_local")

    resumed = asyncio.run(resume_queued())
    assert resumed == 0
    assert jid in lane._pending
    lane._pending.pop(jid, None)


def test_unresumable_kind_is_resolved_not_left_pending():
    """A job nothing can ever pick up is worse than one that visibly failed."""
    from backend.app.queue import resume_queued

    jid = _mk("kind_that_no_longer_exists", JobStatus.queued.value)
    asyncio.run(resume_queued())

    got = db.get_job(jid)
    assert got is not None
    assert got.status == JobStatus.canceled.value
    assert "cannot resume" in (got.message or "")
