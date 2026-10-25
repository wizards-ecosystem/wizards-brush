"""P15: Variant Set stages that run a studio preset revision.

A preset stage is expanded once, through the same resolver a single run uses,
into an ordinary stage; the snapshot keeps the pin and the expansion, so a
later preset edit never changes a set that already exists.
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
from backend.app.resources.catalogue import model_resource_id
from backend.app.routers import common

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "studio" / "presets.json").read_text())


@pytest.fixture()
def enqueued(monkeypatch):
    calls: list[int] = []

    async def record(kind, job_id, handler):
        calls.append(job_id)

    monkeypatch.setattr(common, "enqueue", record)
    return calls


@pytest.fixture()
def local_ready(monkeypatch):
    from backend.app.backends import local
    from backend.app.routers import images

    monkeypatch.setattr(local, "unavailable_reason", lambda: "")
    monkeypatch.setattr(images, "require_local_gpu", lambda: None)
    monkeypatch.setattr(images, "require_model_access", lambda params: None)


def _preset(client, case="no_lora", **changes) -> dict:
    doc = copy.deepcopy(FIXTURES[case])
    doc.pop("id", None)
    doc.pop("revision", None)
    doc["name"] = f"{doc['name']} {uuid.uuid4().hex[:6]}"
    if case == "no_lora":
        doc["model"] = {"resource": model_resource_id(settings.local_image_model)}
    doc["prompt"] = {"template": "{{prompt}}, watercolor", "negative": "blurry"}
    doc["settings"] = {"quality": "High", "aspect": "1:1"}
    doc.update(changes)
    response = client.post("/api/studio/presets", json=doc)
    assert response.status_code == 200, response.text
    return response.json()


def _recipe(preset_id, revision=None, **stage):
    return {"stages": [{
        "preset": {"id": preset_id, **({"revision": revision} if revision else {})},
        "prompt": "a {{pose}} fox", "axes": [{"name": "pose", "values": ["sitting", "running"]}],
        **stage}], "seed": {"mode": "per_variant", "value": 5}}


def test_preview_expands_the_preset_and_pins_its_revision(client, local_ready):
    preset = _preset(client)
    r = client.post("/api/variant-sets/preview", json={"recipe": _recipe(preset["id"])})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 2 and body["stages"][0]["operation"] == "image_local"
    assert [i["prompt"] for i in body["items"]] == ["a sitting fox, watercolor",
                                                     "a running fox, watercolor"]
    assert body["items"][0]["negative_prompt"] == "blurry"
    report = body["presets"][0]
    assert report["preset"]["id"] == preset["id"] and report["preset"]["revision"] == 1
    assert report["readiness"]["submittable"] is True
    stage = body["recipe"]["stages"][0]
    assert stage["preset"]["revision"] == 1 and stage["preset"]["definition_hash"]
    assert stage["params"]["quality"] == "High"


def test_explicit_stage_settings_override_the_preset(client, local_ready):
    preset = _preset(client)
    recipe = _recipe(preset["id"], params={"quality": "Draft"},
                     value_params={"pose": {"running": {"aspect": "16:9"}}})
    body = client.post("/api/variant-sets/preview", json={"recipe": recipe}).json()
    stage = body["recipe"]["stages"][0]
    assert stage["params"]["quality"] == "Draft"
    assert stage["value_params"]["pose"]["running"]["aspect"] == "16:9"


def test_a_created_set_keeps_its_revision_after_the_preset_changes(client, enqueued, local_ready):
    preset = _preset(client)
    made = client.post("/api/variant-sets", json={"recipe": _recipe(preset["id"]),
                                                  "name": "foxes"})
    assert made.status_code == 200, made.text
    set_id = made.json()["id"]
    assert len(enqueued) == 2
    first = db.get_job(enqueued[0])
    assert first.kind == "image_local" and first.params["prompt"].endswith(", watercolor")
    assert first.params["quality"] == "High"
    edited = copy.deepcopy(preset["definition"])
    edited["prompt"]["template"] = "{{prompt}}, oil paint"
    r = client.put(f"/api/studio/presets/{preset['id']}",
                   json={"definition": edited, "base_revision": 1})
    assert r.status_code == 200, r.text
    snapshot = client.get(f"/api/variant-sets/{set_id}").json()["recipe"]
    assert snapshot["stages"][0]["preset"]["revision"] == 1
    assert snapshot["stages"][0]["prompt"] == "a {{pose}} fox, watercolor"
    # A pinned recipe keeps revision 1; an unpinned one follows the preset.
    pinned = client.post("/api/variant-sets/preview",
                         json={"recipe": _recipe(preset["id"], revision=1)}).json()
    assert pinned["items"][0]["prompt"].endswith("watercolor")
    latest = client.post("/api/variant-sets/preview",
                         json={"recipe": _recipe(preset["id"])}).json()
    assert latest["items"][0]["prompt"].endswith("oil paint")
    assert latest["presets"][0]["preset"]["revision"] == 2


def test_a_preset_validation_policy_becomes_the_stage_policy(client, local_ready):
    preset = _preset(client, validation=[
        {"validator": "alpha", "required": True, "options": {"alpha": "required"}}])
    body = client.post("/api/variant-sets/preview", json={"recipe": _recipe(preset["id"])}).json()
    assert body["stages"][0]["validation"] == {"alpha": "required", "required": ["alpha"]}


def test_a_stage_that_names_a_different_operation_is_refused(client, local_ready):
    preset = _preset(client)
    r = client.post("/api/variant-sets/preview",
                    json={"recipe": _recipe(preset["id"], operation="image_colab")})
    assert r.status_code == 400 and "leave the operation empty" in r.json()["detail"]


def test_an_unknown_preset_is_refused_before_anything_exists(client, enqueued):
    r = client.post("/api/variant-sets", json={"recipe": _recipe("custom/nope")})
    assert r.status_code == 400 and "custom/nope" in r.json()["detail"]
    assert enqueued == []


def test_a_remote_preset_stage_does_not_send_images_without_permission(client):
    settings.ensure_dirs()
    path = settings.images_dir / f"vs-preset-{uuid.uuid4().hex[:6]}.png"
    Image.new("RGB", (64, 64), (1, 2, 3)).save(path)
    source = db.add_asset("image", path, width=64, height=64)
    preset = _preset(client, "character_reference", references=[])
    recipe = {"sources": [source.id], "stages": [{
        "preset": {"id": preset["id"]}, "prompt": "she {{act}}",
        "axes": [{"name": "act", "values": ["reads", "sings"]}]}]}
    refused = client.post("/api/variant-sets/preview", json={"recipe": recipe})
    assert refused.status_code == 400 and "allow_remote" in refused.json()["detail"]
    recipe["stages"][0]["preset"]["allow_remote"] = True
    allowed = client.post("/api/variant-sets/preview", json={"recipe": recipe})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["stages"][0]["operation"] == "image_edit"


def test_a_saved_recipe_keeps_an_unpinned_preset_stage_as_written(client, local_ready):
    preset = _preset(client)
    r = client.post("/api/variant-recipes", json={"name": f"foxes {uuid.uuid4().hex[:6]}",
                                                  "recipe": _recipe(preset["id"])})
    assert r.status_code == 200, r.text
    stage = r.json()["recipe"]["stages"][0]
    assert stage["preset"] == {"id": preset["id"], "revision": None, "target": "auto",
                               "allow_remote": False, "definition_hash": ""}
    assert stage["operation"] == ""
