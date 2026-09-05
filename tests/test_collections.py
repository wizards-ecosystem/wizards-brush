"""Collections: grouping assets without owning them.

The invariant worth guarding is that a collection is a *view*. Deleting one, or
removing an asset from one, must never touch the asset or its file.
"""
from __future__ import annotations

from PIL import Image

from backend.app import db


def _asset(tmp_path, name="c.png"):
    f = tmp_path / name
    Image.new("RGB", (8, 8)).save(f)
    return db.add_asset("image", f, generator="local_image:txt2img")


def test_create_list_rename_and_count(client, no_queue, tmp_path):
    c = db.create_collection("Lighthouses")
    a1, a2 = _asset(tmp_path, "a.png"), _asset(tmp_path, "b.png")
    assert db.add_to_collection(c.id, [a1.id, a2.id]) == 2

    listed = {x.name: n for x, n in db.list_collections()}
    assert listed["Lighthouses"] == 2

    assert db.rename_collection(c.id, "Coastal") is True
    assert any(x.name == "Coastal" for x, _ in db.list_collections())


def test_adding_the_same_asset_twice_is_a_no_op(client, no_queue, tmp_path):
    c = db.create_collection("Dupes")
    a = _asset(tmp_path)
    assert db.add_to_collection(c.id, [a.id]) == 1
    assert db.add_to_collection(c.id, [a.id]) == 0
    assert db.collection_asset_ids(c.id) == [a.id]


def test_deleting_a_collection_keeps_every_asset_and_file(client, no_queue, tmp_path):
    """The whole point. "Delete" next to a pile of images reads as something far
    worse than it is, so this pins the behaviour the confirm text promises."""
    c = db.create_collection("Temp")
    a = _asset(tmp_path)
    db.add_to_collection(c.id, [a.id])
    path = a.path

    assert db.delete_collection(c.id) is True
    assert db.get_asset(a.id) is not None
    from pathlib import Path

    assert Path(path).exists()


def test_removing_from_a_collection_keeps_the_asset(client, no_queue, tmp_path):
    c = db.create_collection("Temp")
    a = _asset(tmp_path)
    db.add_to_collection(c.id, [a.id])
    assert db.remove_from_collection(c.id, [a.id]) == 1
    assert db.get_asset(a.id) is not None
    assert db.collection_asset_ids(c.id) == []


def test_gallery_can_scope_to_one_collection(client, no_queue, tmp_path):
    inside, outside = _asset(tmp_path, "in.png"), _asset(tmp_path, "out.png")
    c = db.create_collection("Scoped")
    db.add_to_collection(c.id, [inside.id])

    rows, total = db.search_assets(collection_id=c.id)
    ids = {r.id for r in rows}
    assert inside.id in ids and outside.id not in ids
    assert total == 1


def test_a_trashed_asset_drops_out_of_its_collections(client, no_queue, tmp_path):
    """Collections filter through search_assets, so the soft-delete rule applies
    here too — a deleted image must not linger in a collection view."""
    c = db.create_collection("Mixed")
    a = _asset(tmp_path)
    db.add_to_collection(c.id, [a.id])
    db.delete_asset(a.id)
    _rows, total = db.search_assets(collection_id=c.id)
    assert total == 0


def test_api_round_trip(client, no_queue, tmp_path):
    a = _asset(tmp_path)
    before = len(client.get("/api/collections").json())
    r = client.post("/api/collections", json={"name": "Via API"})
    assert r.status_code == 200
    cid = r.json()["id"]

    assert client.post(f"/api/collections/{cid}/add", json={"asset_ids": [a.id]}).json()["added"] == 1
    mine = next(c for c in client.get("/api/collections").json() if c["id"] == cid)
    assert mine["count"] == 1
    assert client.get(f"/api/assets?collection_id={cid}").json()["total"] == 1

    assert client.delete(f"/api/collections/{cid}").json()["ok"] is True
    after = client.get("/api/collections").json()
    assert len(after) == before
    assert all(c["id"] != cid for c in after)


def test_unknown_collection_is_a_404_not_a_silent_success(client, no_queue):
    assert client.delete("/api/collections/9999").status_code == 404
    assert client.post("/api/collections/9999/rename", json={"name": "x"}).status_code == 404
    # adding to a missing collection reports 0 rather than pretending
    assert client.post("/api/collections/9999/add", json={"asset_ids": [1]}).json()["added"] == 0


def test_names_are_trimmed_and_capped(client, no_queue):
    assert db.create_collection("   ").name == "Untitled"
    assert len(db.create_collection("x" * 500).name) <= db.MAX_COLLECTION_NAME


