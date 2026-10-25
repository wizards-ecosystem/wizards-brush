"""P14: reference sets over HTTP, and what a plan does with them.

A set is revisioned and ordered; saving is a compare-and-set; a plan pins the
revision it used, so editing the set never changes work already queued.
"""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest
from PIL import Image

from backend.app import db
from backend.app.config import settings
from backend.app.routers import common

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "studio" / "presets.json").read_text())


@pytest.fixture()
def enqueued(monkeypatch):
    calls: list[int] = []

    async def record(kind, job_id, handler):
        calls.append(job_id)

    monkeypatch.setattr(common, "enqueue", record)
    return calls


def _image(size=(64, 48), color=(120, 130, 140)) -> int:
    settings.ensure_dirs()
    path = settings.images_dir / f"ref-{uuid.uuid4().hex[:8]}.png"
    Image.new("RGB", size, color).save(path)
    return int(db.add_asset("image", path, width=size[0], height=size[1],
                            generator="local_image:txt2img").id or 0)


def _definition(*ids, mask=None, notes="red scarf"):
    entries = [{"asset_id": i} for i in ids]
    if mask is not None:
        entries[0]["mask_id"] = mask
    return {"notes": notes, "roles": [{"role": "character", "label": "Mara", "entries": entries}]}


def _create(client, *ids, **kw):
    response = client.post("/api/reference-sets", json={
        "name": f"Mara {uuid.uuid4().hex[:6]}", "definition": _definition(*ids, **kw)})
    assert response.status_code == 200, response.text
    return response.json()


def test_a_set_keeps_order_and_masks_and_every_revision(client):
    a, b, c = _image(), _image(), _image()
    mask = _image()
    made = _create(client, c, a, b, mask=mask)
    assert made["revision"] == made["head_revision"] == 1
    entries = made["definition"]["roles"][0]["entries"]
    assert [e["asset_id"] for e in entries] == [c, a, b] and entries[0]["mask_id"] == mask
    updated = client.put(f"/api/reference-sets/{made['id']}", json={
        "definition": _definition(b), "base_revision": 1})
    assert updated.status_code == 200 and updated.json()["revision"] == 2
    old = client.get(f"/api/reference-sets/{made['id']}?revision=1").json()
    assert [e["asset_id"] for e in old["definition"]["roles"][0]["entries"]] == [c, a, b]
    assert old["head_revision"] == 2


def test_a_save_from_an_old_revision_is_a_conflict_not_an_overwrite(client):
    made = _create(client, _image())
    client.put(f"/api/reference-sets/{made['id']}",
               json={"definition": _definition(_image()), "base_revision": 1})
    stale = client.put(f"/api/reference-sets/{made['id']}",
                       json={"definition": _definition(_image()), "base_revision": 1})
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    assert stale.json()["state"]["current_revision"] == 2


def test_assets_masks_and_representatives_are_checked(client):
    good = _image()
    missing = client.post("/api/reference-sets", json={
        "name": "x", "definition": _definition(999_999)})
    assert missing.status_code == 404
    wrong_mask = client.post("/api/reference-sets", json={
        "name": "y", "definition": _definition(good, mask=_image(size=(10, 10)))})
    assert wrong_mask.status_code == 400 and wrong_mask.json()["code"] == "invalid_mask"
    stray = client.post("/api/reference-sets", json={"name": "z", "definition": {
        **_definition(good), "representatives": [_image()]}})
    assert stray.status_code == 400
    empty = client.post("/api/reference-sets", json={"name": "w", "definition": {"roles": []}})
    assert empty.status_code == 400


def test_names_are_unique_among_live_sets_and_archiving_hides(client):
    made = _create(client, _image())
    again = client.post("/api/reference-sets", json={"name": made["name"].upper(),
                                                     "definition": _definition(_image())})
    assert again.status_code == 409 and again.json()["code"] == "name_taken"
    client.post(f"/api/reference-sets/{made['id']}/archive")
    assert made["id"] not in [s["id"] for s in client.get("/api/reference-sets").json()]
    listed = client.get("/api/reference-sets?include_archived=true").json()
    assert any(s["id"] == made["id"] and s["archived"] for s in listed)
    # Archived sets stay readable: old plans name them.
    assert client.get(f"/api/reference-sets/{made['id']}").status_code == 200


def test_views_carry_asset_ids_never_paths(client):
    made = _create(client, _image())
    assert str(settings.images_dir) not in json.dumps(made)


def _character_preset(client) -> dict:
    doc = copy.deepcopy(FIXTURES["character_reference"])
    doc.pop("id", None)
    doc.pop("revision", None)
    doc["name"] = f"Character {uuid.uuid4().hex[:6]}"
    response = client.post("/api/studio/presets", json=doc)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_plan_pins_the_set_revision_and_queued_work_keeps_it(client, enqueued):
    first, second = _image(color=(10, 20, 30)), _image(color=(200, 10, 10))
    made = _create(client, first)
    preset = _character_preset(client)
    request = {"preset_id": preset["id"], "prompt": "she reads", "seed": 3,
               "allow_remote": True,
               "inputs": {"reference_sets": {"character": {"id": made["id"]}}}}
    preview = client.post("/api/jobs", json={**request, "dry_run": True})
    assert preview.status_code == 200, preview.text
    plan = preview.json()["plan"]
    assert plan["inputs"]["reference_sets"]["character"]["revision"] == 1
    assert [r["asset_id"] for r in plan["inputs"]["references"]["character"]] == [first]
    assert plan["prompt"]["positive"].endswith("Keep this character: red scarf")
    if not plan["readiness"]["submittable"]:
        pytest.skip(f"no remote edit slot here: {plan['readiness']}")
    submitted = client.post("/api/jobs", json={
        **request, "request_id": f"ref-{uuid.uuid4().hex}",
        "expected_plan_hash": plan["hashes"]["execution_plan_hash"]})
    assert submitted.status_code == 200, submitted.text
    job = db.get_job(submitted.json()["job_id"])
    before = dict(job.params)
    client.put(f"/api/reference-sets/{made['id']}",
               json={"definition": _definition(second), "base_revision": 1})
    after = db.get_job(job.id)
    assert after.params == before
    stored = db.get_plan(job.plan_id).plan
    assert stored["inputs"]["reference_sets"]["character"]["revision"] == 1
    # A new preview sees the new revision.
    fresh = client.post("/api/jobs", json={**request, "dry_run": True}).json()["plan"]
    assert fresh["inputs"]["reference_sets"]["character"]["revision"] == 2
    assert [r["asset_id"] for r in fresh["inputs"]["references"]["character"]] == [second]


def test_a_set_larger_than_the_role_is_refused(client):
    made = _create(client, *[_image() for _ in range(4)])
    preset = _character_preset(client)
    response = client.post("/api/jobs", json={
        "preset_id": preset["id"], "prompt": "x", "dry_run": True,
        "inputs": {"reference_sets": {"character": {"id": made["id"]}}}})
    assert response.status_code == 400
    assert response.json()["field"] == "inputs.reference_sets.character"


def test_a_kind_request_cannot_name_reference_sets(client):
    response = client.post("/api/jobs", json={
        "kind": "image_local", "params": {"prompt": "x"}, "dry_run": True,
        "inputs": {"reference_sets": {"character": {"id": 1}}}})
    assert response.status_code == 400 and response.json()["field"] == "inputs.reference_sets"
