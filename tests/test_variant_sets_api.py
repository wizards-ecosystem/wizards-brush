"""Variant Set API boundary, recipes, and backward compatibility.

Everything external is validated before anything is stored: a bad recipe is a
400 that says what to change, never a set of jobs that fail later.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from backend.app import db
from backend.app.config import settings
from backend.app.models import JobStatus, VariantItem, VariantSet
from backend.app.routers import common
from backend.app.variant_sets import store


@pytest.fixture()
def enqueued(monkeypatch):
    calls: list[int] = []

    async def record(kind, job_id, handler):
        calls.append(job_id)

    monkeypatch.setattr(common, "enqueue", record)
    return calls


@pytest.fixture()
def source(client):
    settings.ensure_dirs()
    path = settings.images_dir / "variant-api-source.png"
    Image.new("RGB", (64, 64), (120, 130, 140)).save(path)
    return db.add_asset("image", path, width=64, height=64, generator="local_image:txt2img",
                        meta={"prompt": "src"})


def _edit(source_id, **stage):
    return {"sources": [source_id], "stages": [{
        "operation": "image_edit", "prompt": "Make it {{a}}",
        "axes": [{"name": "a", "values": ["x", "y"]}], **stage}]}


def _create(client, recipe, **body):
    return client.post("/api/variant-sets", json={"recipe": recipe, **body})


# ---- boundary validation ------------------------------------------------------------------
@pytest.mark.parametrize("stage, fragment", [
    ({"operation": "teleport"}, "not available here"),
    ({"prompt": "Make it {{colour}}"}, "unknown placeholder {{colour}}"),
    ({"prompt": "Make it {{a"}, "malformed placeholder"),
    ({"axes": [{"name": "a", "values": ["x", "X"]}]}, "both reduce to the identifier"),
    ({"axes": [{"name": "a", "values": ["x"]}, {"name": "A", "values": ["y"]}]}, "used twice"),
    ({"params": {"sampler": "euler"}}, "'sampler' is not a setting of this operation"),
    ({"params": {"seed": 5}}, "the set supplies it"),
    ({"params": {"quality": "Ultra"}}, "quality must be one of"),
    ({"params": {"guidance": "loud"}}, "guidance must be a number"),
    ({"params": {"guidance": 99}}, "between"),
    ({"naming": {"template": "{{a}}/../x"}}, "empty folder or file name"),
    ({"naming": {"prefix": "{{a}}"}}, "unknown placeholder"),
    ({"mask": "nothing"}, "does not use a mask"),
    ({"finishing": [{"processor": "sparkle"}]}, "unknown finishing processor"),
    ({"value_params": {"a": {"z": {"quality": "High"}}}}, "has no value 'z'"),
    ({"value_params": {"b": {"x": {"quality": "High"}}}}, "not an axis known at this stage"),
])
def test_a_bad_recipe_is_refused_with_a_reason(client, enqueued, source, stage, fragment):
    before = len(db.list_jobs(limit=5000))
    r = _create(client, _edit(source.id, **stage))
    assert r.status_code == 400, r.text
    assert fragment in r.json()["detail"]
    assert len(db.list_jobs(limit=5000)) == before, "nothing may be queued for a bad recipe"


def test_conflicting_axis_settings_are_refused(client, enqueued, source):
    recipe = _edit(source.id, axes=[{"name": "a", "values": ["x"]}, {"name": "b", "values": ["y"]}],
                   value_params={"a": {"x": {"quality": "High"}}, "b": {"y": {"quality": "Draft"}}})
    r = _create(client, recipe)
    assert r.status_code == 400 and "set by both" in r.json()["detail"]


def test_content_maps_are_validated(client, enqueued, source):
    r = _create(client, {**_edit(source.id), "content": {"a": {"nope": "text"}}})
    assert r.status_code == 400 and "not one of its values" in r.json()["detail"]
    r = _create(client, {**_edit(source.id), "content": {"a": {"x": "see {{a}}"}}})
    assert r.status_code == 400 and "never expanded again" in r.json()["detail"]


def test_sources_and_masks_must_exist_and_be_live_images(client, enqueued, source):
    r = _create(client, {**_edit(source.id), "sources": [987654]})
    assert r.status_code == 400 and "does not exist or is in the trash" in r.json()["detail"]
    trashed = db.add_asset("image", Path(source.path), generator="x")
    db.delete_asset(trashed.id)
    r = _create(client, {**_edit(source.id), "sources": [trashed.id]})
    assert r.status_code == 400
    r = _create(client, {**_edit(source.id), "sources": []})
    assert r.status_code == 400 and "takes 1-3 source image(s)" in r.json()["detail"]
    inpaint = {"sources": [source.id], "stages": [{"operation": "inpaint", "prompt": "p"}]}
    r = _create(client, inpaint)
    assert r.status_code == 400 and "needs a mask" in r.json()["detail"]


def test_a_later_stage_needs_an_operation_that_takes_an_image(client, enqueued, source):
    recipe = _edit(source.id)
    recipe["stages"].append({"operation": "image_colab", "prompt": "p"})
    r = _create(client, recipe)
    assert r.status_code == 400 and "cannot take the previous stage's output" in r.json()["detail"]


def test_the_cap_is_enforced_before_anything_is_created(client, enqueued, source, monkeypatch):
    monkeypatch.setattr(settings, "variant_max_combinations", 10)
    recipe = _edit(source.id, axes=[{"name": "a", "values": ["1", "2", "3"]},
                                    {"name": "b", "values": ["1", "2", "3", "4"]}],
                   prompt="{{a}} {{b}}")
    for route in ("/api/variant-sets/preview", "/api/variant-sets"):
        r = client.post(route, json={"recipe": recipe})
        assert r.status_code == 400 and "12 combinations; the limit is 10" in r.json()["detail"]
    assert enqueued == []


def test_naming_collisions_are_previewed_and_refused(client, enqueued, source):
    recipe = _edit(source.id, axes=[{"name": "a", "values": ["x", "y"]},
                                    {"name": "b", "values": ["1", "2"]}],
                   prompt="{{a}} {{b}}", naming={"template": "{{a}}"})
    preview = client.post("/api/variant-sets/preview", json={"recipe": recipe}).json()
    assert {c["name"] for c in preview["collisions"]} == {"x.png", "y.png"}
    r = _create(client, recipe)
    assert r.status_code == 400 and "share the file name" in r.json()["detail"]


def test_closed_schemas_refuse_unknown_fields(client, enqueued, source):
    recipe = _edit(source.id)
    assert client.post("/api/variant-sets", json={"recipe": recipe, "surprise": 1}).status_code == 422
    recipe["stages"][0]["extra"] = True
    assert client.post("/api/variant-sets", json={"recipe": recipe}).status_code == 422
    assert client.post("/api/variant-sets", json={"recipe": _edit(source.id),
                                                  "request_id": "bad id"}).status_code == 400
    assert client.post("/api/variant-sets", json={}).status_code == 400


def test_preview_flags_axes_that_change_nothing(client, source):
    recipe = _edit(source.id, axes=[{"name": "a", "values": ["x"]}, {"name": "take", "values": ["1", "2"]}])
    warnings = client.post("/api/variant-sets/preview", json={"recipe": recipe}).json()["warnings"]
    assert any("'take' is not used" in w for w in warnings)
    recipe["seed"] = {"mode": "per_variant", "value": 3}
    assert client.post("/api/variant-sets/preview", json={"recipe": recipe}).json()["warnings"] == []


def test_capabilities_come_from_the_live_registry(client):
    caps = client.get("/api/variant-sets/capabilities").json()
    kinds = {op["kind"] for op in caps["operations"]}
    assert {"image_edit", "img2img", "inpaint", "image_local", "image_colab"} <= kinds
    assert "control_local" not in kinds          # ENABLE_CONTROLNET is off
    edit = next(op for op in caps["operations"] if op["kind"] == "image_edit")
    assert edit["max_sources"] == 3 and edit["lane"] == "remote"
    assert {p["name"] for p in caps["finishing"]} >= {"background_removal", "resize"}
    assert caps["cap"] == settings.variant_max_combinations
    assert "prompt" in caps["set_controlled"]


# ---- recipes ----------------------------------------------------------------------------------
def test_recipes_are_definitions_and_sets_keep_their_own_snapshot(client, enqueued, source):
    definition = {"sources": [], "stages": [{
        "operation": "image_edit", "prompt": "Make it {{a}}",
        "axes": [{"name": "a", "values": ["x", "y"]}]}]}
    saved = client.post("/api/variant-recipes", json={"name": "Two looks", "recipe": definition})
    assert saved.status_code == 200, saved.text
    recipe_id = saved.json()["id"]
    assert client.get(f"/api/variant-recipes/{recipe_id}").json()["recipe"]["sources"] == []

    made = client.post("/api/variant-sets", json={"recipe_id": recipe_id,
                                                  "sources": [source.id]}).json()
    assert made["recipe_id"] == recipe_id and made["expected"] == 2

    # Editing the recipe must not change what the set meant.
    edited = {**definition, "stages": [{**definition["stages"][0], "prompt": "Now {{a}}"}]}
    assert client.put(f"/api/variant-recipes/{recipe_id}",
                      json={"name": "Two looks", "recipe": edited}).status_code == 200
    snapshot = store.get_set(made["id"]).recipe
    assert snapshot["stages"][0]["prompt"] == "Make it {{a}}"
    assert snapshot["sources"] == [source.id]
    assert isinstance(snapshot["seed"]["value"], int) and snapshot["seed"]["value"] >= 0

    clone = client.post(f"/api/variant-recipes/{recipe_id}/clone").json()
    assert clone["name"] == "Two looks (copy)" and clone["id"] != recipe_id
    assert any(r["id"] == clone["id"] for r in client.get("/api/variant-recipes").json())
    assert client.delete(f"/api/variant-recipes/{clone['id']}").json() == {"ok": True}
    assert client.get(f"/api/variant-recipes/{clone['id']}").status_code == 404

    bad = client.post("/api/variant-recipes", json={"name": "Bad", "recipe": {
        "stages": [{"operation": "image_edit", "prompt": "{{nope}}"}]}})
    assert bad.status_code == 400


def test_recipe_id_and_inline_recipe_are_exclusive(client, source):
    r = client.post("/api/variant-sets/preview", json={"recipe": _edit(source.id), "recipe_id": 1})
    assert r.status_code == 400
    assert client.post("/api/variant-sets/preview", json={"recipe_id": 999999}).status_code == 404


# ---- backward compatibility ---------------------------------------------------------------------
def test_job_status_keeps_exactly_five_values():
    assert [s.value for s in JobStatus] == ["queued", "running", "done", "error", "canceled"]


def test_ordinary_jobs_are_unchanged(client, no_queue):
    r = client.post("/api/generate/image/local", data={"payload": json.dumps({"prompt": "cat"})})
    params = db.get_job(r.json()["job_id"]).params
    assert "variant" not in params and "finish_steps" not in params
    assert db.get_job(r.json()["job_id"]).group_id is None


def test_ordinary_asset_metadata_has_no_variant_key():
    meta = common.image_meta({"prompt": "p"}, Image.new("RGB", (8, 8)), prompt="p", seed=1)
    assert "variant" not in meta


def test_grids_and_combinatorial_prompts_still_fan_out_as_before(client, no_queue):
    grid = client.post("/api/generate/grid", json={
        "kind": "image_local", "payload": {"prompt": "g", "seed": 1},
        "x": {"param": "guidance", "values": [1, 2]}}).json()
    jobs = [db.get_job(j) for j in grid["job_ids"]]
    assert {j.group_id for j in jobs} == {grid["group_id"]}
    assert all(j.params["grid_id"] == grid["group_id"] for j in jobs)
    assert not grid["group_id"].startswith("vset-")
    combo = client.post("/api/generate/image/local", data={"payload": json.dumps(
        {"prompt": "a {red|blue} car", "combinatorial": True})}).json()
    assert len(combo["job_ids"]) == 2


def test_clear_all_removes_runs_but_keeps_recipes(client, enqueued, source):
    client.post("/api/variant-recipes", json={"name": "Keep me", "recipe": _edit(source.id)})
    made = _create(client, _edit(source.id)).json()
    db.clear_all()
    assert store.get_set(made["id"]) is None
    assert store.items_for_set(made["id"]) == []
    assert any(r.name == "Keep me" for r in store.list_recipes())


def test_history_pruning_keeps_jobs_a_set_points_at(client, enqueued, source):
    made = _create(client, _edit(source.id)).json()
    item = store.items_for_set(made["id"])[0]
    db.update_job(item.job_id, status=JobStatus.error.value)
    db.prune_job_history(keep=0)
    assert db.get_job(item.job_id) is not None


def test_tables_are_separate_from_jobs():
    """No column was added to job; child identity lives on the item."""
    assert "variant" not in " ".join(c.name for c in db.Job.__table__.columns)
    assert {"set_id", "job_id", "key"} <= {c.name for c in VariantItem.__table__.columns}
    assert "group_id" in {c.name for c in VariantSet.__table__.columns}


# ---- the API as a client meets it ---------------------------------------------------------
def test_detail_carries_the_recipe_snapshot_and_items_page(client, enqueued, source):
    made = _create(client, _edit(source.id, axes=[{"name": "a", "values": ["x", "y", "z"]}]))
    body = made.json()
    assert made.status_code == 200 and body["created"] is True
    set_id = body["id"]
    detail = client.get(f"/api/variant-sets/{set_id}").json()
    assert detail["recipe"]["stages"][0]["prompt"] == "Make it {{a}}"
    assert detail["recipe"]["seed"]["value"] >= 0, "the snapshot records the seed that ran"
    assert "created" not in detail
    summary = client.get(f"/api/variant-sets/{set_id}", params={"items": False}).json()
    assert "items" not in summary and summary["recipe"] == detail["recipe"]
    page = client.get(f"/api/variant-sets/{set_id}/items",
                      params={"limit": 2, "offset": 1}).json()
    assert [i["key"] for i in page] == ["a=y", "a=z"]
    assert "params" not in page[0], "params are for single-item reads"
    listed = next(s for s in client.get("/api/variant-sets").json() if s["id"] == set_id)
    assert "recipe" not in listed and "items" not in listed and "created" not in listed


def test_wait_returns_when_the_set_settles(client, enqueued, source):
    set_id = _create(client, _edit(source.id)).json()["id"]
    running = client.get(f"/api/variant-sets/{set_id}/wait", params={"timeout": 0}).json()
    assert running["settled"] is False and running["set"]["status"] == "active"
    assert "items" not in running["set"]
    client.post(f"/api/variant-sets/{set_id}/cancel")
    settled = client.get(f"/api/variant-sets/{set_id}/wait",
                         params={"timeout": 5, "items": True}).json()
    assert settled["settled"] is True and settled["set"]["status"] == "canceled"
    assert len(settled["set"]["items"]) == 2
    assert client.get("/api/variant-sets/999999/wait").status_code == 404


def test_finish_steps_belong_to_the_stage_not_its_params(client, enqueued, source):
    r = _create(client, _edit(source.id, params={"finish_steps": []}))
    assert r.status_code == 400
    assert "use the stage's `finishing` list" in r.json()["detail"]
