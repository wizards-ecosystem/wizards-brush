"""Warm-model affinity: the lane prefers a job whose model is already loaded,
but never at the cost of priority order, and never unboundedly."""
from __future__ import annotations

import pytest

from backend.app import db, queue
from backend.app.models import JobStatus

TURBO = "Tongyi-MAI/Z-Image-Turbo"
QUALITY = "Some-Org/Z-Image-Quality"


@pytest.fixture
def resident(monkeypatch):
    """Control what the GPU is holding."""
    state = {"model": None}

    def _set(model):
        state["model"] = model

    monkeypatch.setattr("backend.app.generators.local_image.resident_model",
                        lambda: state["model"])
    return _set


def _job(model_key: str | None, priority: int = 0) -> int:
    j = db.create_job("image_local", {"prompt": "x"}, model_key=model_key)
    assert j.id is not None
    if priority:
        db.update_job(j.id, priority=priority)
    return j.id


def _pick(ids: list[int]) -> int:
    """Run the scheduler over these ids, in the order given.

    `_affinity_pick` takes whole rows rather than ids: it needs `priority` and
    `model_key` for every candidate, and fetching those one id at a time opened a
    Session per row — up to 33 for a single pick, on the dequeue path. The tests
    still speak in ids because that is what reads clearly here.
    """
    rows = {j.id: j for j in db.queued_in_order(ids)}
    return queue._affinity_pick([rows[i] for i in ids if i in rows])


def test_head_wins_when_nothing_is_loaded(client, resident):
    resident(None)
    a, b = _job(QUALITY), _job(TURBO)
    assert _pick([a, b]) == a


def test_head_wins_when_it_already_matches(client, resident):
    resident(TURBO)
    a, b = _job(TURBO), _job(TURBO)
    assert _pick([a, b]) == a


def test_warm_job_jumps_a_cold_head(client, resident):
    """The whole point: do not reload the pipeline to run the next job in line."""
    resident(TURBO)
    cold, warm = _job(QUALITY), _job(TURBO)
    assert _pick([cold, warm]) == warm


def test_affinity_never_crosses_a_priority_tier(client, resident):
    """A warm job at lower priority must not overtake a cold, higher-priority one."""
    resident(TURBO)
    cold_important = _job(QUALITY, priority=10)
    warm_ordinary = _job(TURBO, priority=0)
    assert _pick([cold_important, warm_ordinary]) == cold_important


def test_affinity_applies_within_a_tier(client, resident):
    resident(TURBO)
    cold = _job(QUALITY, priority=5)
    warm = _job(TURBO, priority=5)
    assert _pick([cold, warm]) == warm


def test_window_is_bounded(client, resident):
    """Beyond AFFINITY_WINDOW the head runs, so a cold job cannot starve."""
    resident(TURBO)
    cold = _job(QUALITY)
    filler = [_job(QUALITY) for _ in range(queue.AFFINITY_WINDOW)]
    warm = _job(TURBO)
    ordered = [cold, *filler, warm]
    assert ordered.index(warm) > queue.AFFINITY_WINDOW
    assert _pick(ordered) == cold, "warm job is outside the window"


def test_warm_job_at_the_window_edge_is_still_reachable(client, resident):
    resident(TURBO)
    cold = _job(QUALITY)
    filler = [_job(QUALITY) for _ in range(queue.AFFINITY_WINDOW - 1)]
    warm = _job(TURBO)
    assert _pick([cold, *filler, warm]) == warm


def test_remote_jobs_have_no_model_key_and_never_trigger_a_swap(client, resident):
    """A NULL key means 'does not touch the local pipeline' — treat it as a match."""
    resident(TURBO)
    remote = _job(None)
    warm = _job(TURBO)
    assert _pick([remote, warm]) == remote


def test_a_failing_probe_falls_back_to_plain_order(client, monkeypatch):
    """Scheduling must never break because introspection did."""
    def boom():
        raise RuntimeError("cache is mid-swap")

    monkeypatch.setattr("backend.app.generators.local_image.resident_model", boom)
    a, b = _job(QUALITY), _job(TURBO)
    assert _pick([a, b]) == a


def test_model_key_is_set_from_params_at_creation(client):
    from backend.app.routers.common import model_key_for

    assert model_key_for("image_local", {"model_variant": "turbo"})
    assert model_key_for("upscale", {"model_variant": "turbo"}) is None, \
        "tool jobs never load the diffusion pipeline"
    assert model_key_for("t2v", {}) is None, "remote jobs get no local key"


def test_single_item_queue_is_unchanged(client, resident):
    resident(QUALITY)
    only = _job(TURBO)
    assert _pick([only]) == only


def test_canceled_jobs_are_not_picked(client, resident):
    resident(TURBO)
    cold = _job(QUALITY)
    warm = _job(TURBO)
    db.update_job(warm, status=JobStatus.canceled.value)
    # queued_ids_in_order filters terminal rows, so the lane never sees `warm`.
    assert db.queued_ids_in_order([cold, warm]) == [cold]


# ---- making the reordering visible -----------------------------------------
#
# Affinity is the one place the lane deliberately disagrees with the order the
# Queue page displays. An optimisation the user cannot see is indistinguishable
# from a bug, so the disagreement has to be reported, not just performed.
def test_next_endpoint_reports_the_head_with_no_reason(client, resident):
    resident(TURBO)
    a, b = _job(TURBO), _job(TURBO)
    _pretend_queued("local", [a, b])
    lanes = client.get("/api/jobs/next").json()["lanes"]
    assert lanes["local"]["job_id"] == a
    assert lanes["local"]["reason"] is None, "no reordering happened; say nothing"


def test_next_endpoint_explains_a_job_that_jumps_the_head(client, resident):
    resident(TURBO)
    cold, warm = _job(QUALITY), _job(TURBO)
    _pretend_queued("local", [cold, warm])
    lanes = client.get("/api/jobs/next").json()["lanes"]
    assert lanes["local"]["job_id"] == warm, "the warm job runs first"
    assert lanes["local"]["reason"], "and the page must be able to say why"


def test_next_endpoint_stays_quiet_for_an_empty_lane(client, resident):
    resident(TURBO)
    assert client.get("/api/jobs/next").json()["lanes"] == {}


@pytest.fixture(autouse=True)
def _clean_lanes():
    """The lanes are module-level singletons, so a pending map left behind by one
    test is a queued job the next one never created."""
    yield
    for q in queue.LANES.values():
        q._pending.clear()
        q._current = None


def _pretend_queued(lane: str, ids: list[int]) -> None:
    """Put ids in a lane's pending map without starting a worker."""
    q = queue.LANES[lane]
    q._current = None
    for jid in ids:
        q._pending[jid] = (lambda *a: {}, "image_local")
