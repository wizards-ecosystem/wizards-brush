"""A batch is one job, so its images have to be announced individually.

Before this, `persist_image` saved and enriched but told nobody, and the client
only refreshed on the job's terminal event — so a batch of eight showed nothing
until the eighth finished, even though the first seven were already on disk.
"""
from __future__ import annotations

import asyncio

from backend.app.queue import ProgressHub


def _drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_asset_event_survives_a_preview_flood():
    """Asset announcements are must-deliver. A client behind a flood of preview
    frames may lose previews — losing the only signal that an image exists means
    it never appears until the job ends, which is the bug this fixes."""
    hub = ProgressHub()
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    hub._subs.add(q)

    hub._emit({"type": "asset", "id": 7, "job_id": 1, "kind": "image"})
    for i in range(50):  # far past the queue's capacity
        hub._emit({"type": "job", "id": 1, "status": "running", "progress": i / 50})

    events = _drain(q)
    assert any(e.get("type") == "asset" and e["id"] == 7 for e in events)


def test_asset_event_is_not_droppable():
    hub = ProgressHub()
    assert not hub._droppable({"type": "asset", "id": 1, "job_id": 1, "kind": "image"})


def test_persist_announces_each_image_of_a_batch():
    """The whole point: N images in one job produce N announcements, each
    naming the job so a client can attribute it without a refetch."""
    import backend.app.queue as queue_mod
    from backend.app.routers import common

    seen: list[dict] = []

    class _Hub:
        @staticmethod
        def emit(event: dict) -> None:
            seen.append(event)

    real = queue_mod.hub
    queue_mod.hub = _Hub()  # type: ignore[assignment]
    try:
        for asset_id in (11, 12, 13):
            common._announce_asset(asset_id, job_id=4, kind="image")
    finally:
        queue_mod.hub = real

    assert [e["id"] for e in seen] == [11, 12, 13]
    assert {e["job_id"] for e in seen} == {4}
    assert {e["type"] for e in seen} == {"asset"}


def test_announcement_failure_never_breaks_a_saved_asset():
    """The asset is already on disk and in the database by this point. A hub
    that cannot accept the event must cost a late refresh, not the image."""
    import backend.app.queue as queue_mod
    from backend.app.routers import common

    class _Broken:
        @staticmethod
        def emit(event: dict) -> None:
            raise RuntimeError("no loop")

    real = queue_mod.hub
    queue_mod.hub = _Broken()  # type: ignore[assignment]
    try:
        common._announce_asset(1, job_id=1, kind="image")  # must not raise
    finally:
        queue_mod.hub = real
