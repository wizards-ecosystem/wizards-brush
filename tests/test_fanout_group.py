"""The one fan-out primitive and the job-terminal hook.

`submit_group` is what grids, combinatorial prompts and Variant Sets share, so
the old `submit`/`submit_fanout` contracts are pinned here as well as the new
per-child idempotency. The terminal hook is how set-level work hears that a
child finished without the lanes knowing sets exist.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.app import db
from backend.app import queue as qmod
from backend.app.models import JobStatus
from backend.app.routers import common


def _handler(job_id, params, cb):
    return {"asset_ids": []}


@pytest.fixture()
def enqueued(monkeypatch):
    calls: list[tuple[str, int]] = []

    async def record(kind, job_id, handler):
        calls.append((kind, job_id))

    monkeypatch.setattr(common, "enqueue", record)
    return calls


def test_submit_group_creates_ordinary_jobs_with_verbatim_params(client, enqueued):
    children = [{"prompt": f"child {i}", "seed": i} for i in range(3)]
    out = asyncio.run(common.submit_group(
        "image_local", _handler, children, group_id="group-verbatim"))
    assert [created for _job, created in out] == [True, True, True]
    jobs = [db.get_job(job.id) for job, _ in out]
    assert [j.params["prompt"] for j in jobs] == ["child 0", "child 1", "child 2"]
    assert {j.group_id for j in jobs} == {"group-verbatim"}
    assert {j.kind for j in jobs} == {"image_local"}
    assert [job_id for _kind, job_id in enqueued] == [j.id for j in jobs]
    assert common.get_handler("image_local") is not None  # rerun still works


def test_per_child_request_ids_make_a_retried_group_idempotent(client, enqueued):
    children = [{"prompt": "a"}, {"prompt": "b"}]
    ids = ["retry-child-request-0001", "retry-child-request-0002"]
    first = asyncio.run(common.submit_group(
        "image_local", _handler, children, group_id="group-idem", request_ids=ids))
    again = asyncio.run(common.submit_group(
        "image_local", _handler, children, group_id="group-idem", request_ids=ids))
    assert [job.id for job, _ in first] == [job.id for job, _ in again]
    assert [created for _job, created in again] == [False, False]
    assert len(enqueued) == 2, "a recovered child must not be enqueued twice"
    assert len(db.list_jobs_by_group("group-idem")) == 2


def test_history_can_be_skipped_for_machine_generated_children(client, enqueued):
    before = len(db.list_history(limit=500))
    asyncio.run(common.submit_group(
        "image_local", _handler, [{"prompt": "no-history-probe"}],
        group_id="group-nohist", record_history=False))
    assert len(db.list_history(limit=500)) == before


def test_a_request_owned_by_another_group_stops_the_fanout(client, enqueued, monkeypatch):
    """The concurrent-retry path of submit_fanout: the first child's request id
    already belongs to the original group, so nothing else may be created."""
    original = asyncio.run(common.submit_fanout(
        "image_local", {}, _handler, [{"prompt": "x"}, {"prompt": "y"}],
        request_id="fanout-original-0001"))
    # Simulate the race: the early duplicate check misses, the unique index does not.
    monkeypatch.setattr(common.db, "job_for_request", lambda _rid: None)
    before = len(db.list_jobs(limit=1000))
    again = asyncio.run(common.submit_fanout(
        "image_local", {}, _handler, [{"prompt": "x"}, {"prompt": "y"}],
        request_id="fanout-original-0001"))
    assert again["group_id"] == original["group_id"]
    assert again["job_ids"] == original["job_ids"]
    assert len(db.list_jobs(limit=1000)) == before


def test_fanout_still_tags_children_with_their_grid(client, enqueued):
    r = asyncio.run(common.submit_fanout(
        "image_local", {"seed": 3}, _handler, [{"prompt": "p"}, {"prompt": "q"}]))
    jobs = [db.get_job(i) for i in r["job_ids"]]
    assert {j.params["grid_id"] for j in jobs} == {r["group_id"]}
    assert all(j.params["seed"] == 3 for j in jobs)


# ---- the terminal hook ---------------------------------------------------------
@pytest.fixture()
def heard(monkeypatch):
    got: list[tuple[int, str]] = []
    monkeypatch.setattr(qmod, "_TERMINAL_LISTENERS", {})
    qmod.on_job_terminal("probe", lambda job: got.append((job.id, job.status)))
    return got


def _run_lane_once(job_id: int, handler) -> None:
    async def drive():
        lane = qmod.JobQueue("test")
        lane._pending[job_id] = (handler, "image_local")
        lane._wake.set()
        task = asyncio.create_task(lane._run())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if db.get_job(job_id).status in ("done", "error", "canceled"):
                break
        await asyncio.sleep(0.02)
        task.cancel()

    asyncio.run(drive())


def test_listeners_hear_success_and_failure_from_the_lane(client, no_queue, heard):
    ok = db.create_job("image_local", {"prompt": "fine"})
    bad = db.create_job("image_local", {"prompt": "boom"})
    _run_lane_once(ok.id, _handler)

    def explode(job_id, params, cb):
        raise RuntimeError("handler failed")

    _run_lane_once(bad.id, explode)
    assert (ok.id, "done") in heard
    assert (bad.id, "error") in heard


def test_listeners_hear_a_queued_cancel(client, no_queue, heard):
    job = db.create_job("image_local", {"prompt": "never runs"})
    assert qmod.cancel(job.id)
    assert heard == [(job.id, JobStatus.canceled.value)]


def test_a_failing_listener_never_breaks_the_lane(client, no_queue, heard, monkeypatch):
    def broken(job):
        raise RuntimeError("listener bug")

    qmod.on_job_terminal("broken", broken)
    job = db.create_job("image_local", {"prompt": "still finishes"})
    _run_lane_once(job.id, _handler)
    assert db.get_job(job.id).status == "done"
    assert (job.id, "done") in heard


def test_registration_is_idempotent_by_name(monkeypatch):
    monkeypatch.setattr(qmod, "_TERMINAL_LISTENERS", {})
    qmod.on_job_terminal("same", lambda job: None)
    qmod.on_job_terminal("same", lambda job: None)
    assert list(qmod._TERMINAL_LISTENERS) == ["same"]


def test_async_listeners_run_as_tasks_on_the_loop(client, no_queue, monkeypatch):
    monkeypatch.setattr(qmod, "_TERMINAL_LISTENERS", {})
    got: list[int] = []

    async def listener_body(job_id):
        await asyncio.sleep(0)
        got.append(job_id)

    qmod.on_job_terminal("async", lambda job: listener_body(job.id))
    job = db.create_job("image_local", {"prompt": "async"})

    async def drive():
        qmod.cancel(job.id)
        for _ in range(20):
            await asyncio.sleep(0.01)

    asyncio.run(drive())
    assert got == [job.id]
