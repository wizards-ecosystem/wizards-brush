"""Queue priority plumbing: DB run-order helper and the reorder/front routes."""
from __future__ import annotations

from backend.app import db


def _mk(kind="image_local", **kw):
    return db.create_job(kind, {"prompt": "q"}, **kw)


def test_queued_ids_in_order_priority_then_id(client, no_queue):
    a, b, c = _mk(), _mk(), _mk()
    db.update_job(b.id, priority=5)
    order = db.queued_ids_in_order([a.id, b.id, c.id])
    assert order == [b.id, a.id, c.id]
    # canceled jobs drop out
    db.update_job(a.id, status="canceled")
    assert db.queued_ids_in_order([a.id, b.id, c.id]) == [b.id, c.id]


def test_cancel_route_reports_effect_and_404(client, no_queue):
    j = _mk()
    r = client.post(f"/api/jobs/{j.id}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True  # a queued job was actually canceled
    # cancelling an already-canceled job is a no-op
    assert client.post(f"/api/jobs/{j.id}/cancel").json()["changed"] is False
    # unknown id 404s instead of silently returning ok
    assert client.post("/api/jobs/999999/cancel").status_code == 404


def test_skip_route_reports_no_effect_when_not_running(client, no_queue):
    j = _mk()  # queued, not running → skip has no effect
    r = client.post(f"/api/jobs/{j.id}/skip")
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert client.post("/api/jobs/999999/skip").status_code == 404


def test_move_route_reports_edge_noop(client, no_queue):
    j = _mk()
    client.post(f"/api/jobs/{j.id}/front")  # bump to the top of its lane
    # already at the top → moving up changes nothing
    assert client.post(f"/api/jobs/{j.id}/move", json={"dir": "up"}).json()["changed"] is False


def test_front_route_bumps_priority(client, no_queue):
    a = _mk()
    b = _mk()
    r = client.post(f"/api/jobs/{b.id}/front")
    assert r.status_code == 200
    assert db.queued_ids_in_order([a.id, b.id])[0] == b.id


def test_move_route_swaps_neighbors(client, no_queue):
    a, b, c = _mk(), _mk(), _mk()
    r = client.post(f"/api/jobs/{c.id}/move", json={"dir": "up"})
    assert r.status_code == 200
    order = db.queued_ids_in_order([a.id, b.id, c.id])
    assert order.index(c.id) < order.index(b.id)
    assert order.index(a.id) == 0

    # moving the top job up is a no-op
    top = order[0]
    assert client.post(f"/api/jobs/{top}/move", json={"dir": "up"}).status_code == 200


def test_move_conflicts_on_non_queued(client, no_queue):
    a = _mk()
    db.update_job(a.id, status="done")
    assert client.post(f"/api/jobs/{a.id}/move", json={"dir": "up"}).status_code == 409
    assert client.post(f"/api/jobs/{a.id}/front").status_code == 409


def test_group_helpers(client, no_queue):
    g = "grp-test-1"
    j1 = db.create_job("image_local", {"prompt": "a"}, group_id=g)
    j2 = db.create_job("image_local", {"prompt": "b"}, group_id=g)
    ids = [j.id for j in db.list_jobs_by_group(g)]
    assert ids == [j1.id, j2.id]
