"""HTTP CRUD and run for saved generation recipes.

A run goes through POST /api/jobs, so the queue stub is the same one the job
API tests use: the row is stored and the handler never imports a model.
"""
from __future__ import annotations

import uuid

import pytest
from PIL import Image

from backend.app import db
from backend.app.config import settings
from backend.app.routers import common


@pytest.fixture()
def enqueued(monkeypatch):
    calls: list[int] = []

    async def record(kind, job_id, handler):
        calls.append(job_id)

    monkeypatch.setattr(common, "enqueue", record)
    return calls


def _image(color=(120, 130, 140), size=(64, 48), mode="RGB", name="recipe-src.png"):
    settings.ensure_dirs()
    path = settings.images_dir / name
    Image.new(mode, size, color).save(path)
    return db.add_asset(
        "image", path, width=size[0], height=size[1],
        generator="local_image:txt2img", meta={"prompt": "src"},
    )


def _name(label: str) -> str:
    return f"{label} {uuid.uuid4().hex[:8]}"


def _save(client, **body):
    response = client.post("/api/recipes", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_save_run_and_dry_run_keep_frozen_settings(client, enqueued):
    saved = _save(
        client,
        name=_name("Ceramic cup"),
        description="A studio cup",
        kind="image_local",
        params={"prompt": "a ceramic cup", "seed": 7},
        expose=["prompt"],
        field_descriptions={"prompt": "What to draw."},
    )
    assert saved["client"] == "api"
    assert saved["params"]["seed"] == 7
    assert "image_path" not in saved["params"]
    assert saved["input_schema"]["additionalProperties"] is False
    assert list(saved["input_schema"]["properties"]) == ["prompt"]
    before = len(enqueued)

    dry = client.post(f"/api/recipes/{saved['slug']}/run", json={
        "inputs": {"prompt": "a blue cup"}, "dry_run": True,
    })
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["dry_run"] is True
    assert body["would_queue"] == 1
    assert body["job_id"] is None
    assert body["params"]["seed"] == 7
    assert body["params"]["prompt"] == "a blue cup"
    assert body["recipe_id"] == saved["id"]
    assert len(enqueued) == before

    ran = client.post(f"/api/recipes/{saved['name']}/run", json={
        "inputs": {"prompt": "a blue cup"},
    })
    assert ran.status_code == 200, ran.text
    queued = ran.json()
    assert queued["dry_run"] is False
    assert queued["job_id"] in enqueued
    job = db.get_job(queued["job_id"])
    assert job is not None
    assert job.params["seed"] == 7
    assert job.params["prompt"] == "a blue cup"
    assert job.client == "api"
    assert "recipe_id" not in job.params

    refused = client.post(f"/api/recipes/{saved['id']}/run", json={"inputs": {"steps": 4}})
    assert refused.status_code == 400
    error = refused.json()
    assert error["code"] == "unknown_setting"
    assert error["field"] == "steps"
    assert error["valid_options"] == ["prompt"]
    assert isinstance(error["detail"], str)


def test_name_slug_update_and_delete(client):
    name = _name("Tea bowl")
    saved = _save(client, name=name, kind="image_local", params={"prompt": "a bowl", "seed": 3}, expose=[])
    clash = client.post("/api/recipes", json={
        "name": name.upper(), "kind": "image_local", "params": {"prompt": "other"}, "expose": ["prompt"],
    })
    assert clash.status_code == 409
    assert clash.json()["code"] == "conflict"
    assert clash.json()["field"] == "name"

    renamed = client.put(f"/api/recipes/{saved['slug']}", json={
        "name": _name("Tea bowl renamed"),
        "kind": "image_local",
        "params": {"prompt": "a deeper bowl", "seed": 3},
        "expose": ["prompt"],
    })
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["slug"] == saved["slug"]
    assert renamed.json()["client"] == "api"

    explicit = client.put(f"/api/recipes/{saved['id']}", json={
        "name": renamed.json()["name"],
        "slug": f"bowl-{uuid.uuid4().hex[:8]}",
        "kind": "image_local",
        "params": {"prompt": "a deeper bowl"},
        "expose": ["prompt"],
    })
    assert explicit.status_code == 200
    assert explicit.json()["slug"] != saved["slug"]

    listed = client.get("/api/recipes").json()
    assert any(row["id"] == saved["id"] for row in listed)
    missing = client.delete(f"/api/recipes/{explicit.json()['slug']}")
    assert missing.status_code == 200 and missing.json() == {"ok": True}
    gone = client.get(f"/api/recipes/{explicit.json()['slug']}")
    assert gone.status_code == 404
    assert gone.json()["code"] == "not_found"
    assert gone.json()["field"] == "recipe"


def test_upscale_exposes_scale_and_keeps_the_image(client):
    source = _image(name=f"recipe-upscale-{uuid.uuid4().hex[:8]}.png")
    saved = _save(
        client, name=_name("Bigger"), kind="upscale",
        params={"scale": 4}, inputs={"images": [source.id]}, expose=["scale"],
    )
    assert saved["inputs"]["images"] == [source.id]
    assert saved["fields"][0]["constraints"]["options"] == [2, 4]
    dry = client.post(f"/api/recipes/{saved['slug']}/run", json={
        "inputs": {"scale": 2}, "dry_run": True,
    })
    assert dry.status_code == 200, dry.text
    assert dry.json()["params"]["scale"] == 2
    bad = client.post(f"/api/recipes/{saved['slug']}/run", json={"inputs": {"scale": 3}, "dry_run": True})
    assert bad.status_code == 400
    assert bad.json()["code"] in {"invalid_option", "out_of_range"}
    assert bad.json()["field"] == "scale"


def test_inpaint_requires_images_and_a_matching_mask(client, enqueued):
    saved = _save(
        client, name=_name("Mend"), kind="inpaint",
        params={"prompt": "mend the rim"}, expose=["images", "mask", "prompt"],
    )
    assert saved["input_schema"]["required"] == ["images", "mask"]
    bare = client.post(
        f"/api/recipes/{saved['slug']}/run",
        json={"inputs": {"prompt": "mend"}, "dry_run": True},
    )
    assert bare.status_code == 400
    assert bare.json()["field"] == "images"

    source = _image(name=f"recipe-inpaint-{uuid.uuid4().hex[:8]}.png")
    small = _image(color=255, size=(32, 32), mode="L", name=f"recipe-mask-{uuid.uuid4().hex[:8]}.png")
    mismatch = client.post(f"/api/recipes/{saved['slug']}/run", json={
        "inputs": {"images": [source.id], "mask": small.id, "prompt": "mend"},
        "dry_run": True,
    })
    assert mismatch.status_code == 400
    assert "must match" in mismatch.json()["detail"]

    fits = _image(color=255, size=(64, 48), mode="L", name=f"recipe-mask-fit-{uuid.uuid4().hex[:8]}.png")
    ran = client.post(f"/api/recipes/{saved['slug']}/run", json={
        "inputs": {"images": [source.id], "mask": fits.id, "prompt": "mend the rim"},
    })
    assert ran.status_code == 200, ran.text
    job = db.get_job(ran.json()["job_id"])
    assert job is not None
    assert job.params["image_path"] == source.path
    assert job.params["mask_path"] == fits.path
    assert ran.json()["job_id"] in enqueued


def test_browser_origin_tags_the_recipe_and_the_run_separately(client, enqueued):
    saved = client.post("/api/recipes", headers={"Origin": "http://localhost"}, json={
        "name": _name("Browser cup"),
        "kind": "image_local",
        "params": {"prompt": "a cup", "seed": 1},
        "expose": ["prompt"],
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["client"] == "ui"
    ran = client.post(
        f"/api/recipes/{saved.json()['slug']}/run",
        headers={"Origin": "http://localhost"},
        json={"inputs": {"prompt": "a cup on a table"}},
    )
    assert ran.status_code == 200, ran.text
    job = db.get_job(ran.json()["job_id"])
    assert job is not None and job.client == "ui"


def test_clear_all_keeps_generation_recipes(client, enqueued):
    saved = _save(
        client,
        name=_name("Keep me"),
        kind="image_local",
        params={"prompt": "stay"},
        expose=["prompt"],
    )
    client.post(f"/api/recipes/{saved['slug']}/run", json={"inputs": {"prompt": "go"}})
    assert db.get_recipe(saved["id"]) is not None
    db.clear_all()
    assert db.get_recipe(saved["id"]) is not None
    assert db.list_jobs(limit=100) == []
    # Job has no AUTOINCREMENT, so the next insert reuses ids. A cancel flag
    # for an id that clear_all just deleted would cancel that new job.
    from backend.app.queue import _CANCELLED

    _CANCELLED.clear()


def test_a_numeric_key_is_an_id_before_it_is_a_slug(client):
    first = _save(
        client, name=_name("Numbered"), kind="image_local",
        params={"prompt": "a cup"}, expose=["prompt"],
    )
    second = _save(
        client, name=_name("Also numbered"), kind="image_local",
        params={"prompt": "another cup"}, expose=["prompt"], slug=str(first["id"]),
    )
    by_id = client.get(f"/api/recipes/{first['id']}")
    assert by_id.status_code == 200
    assert by_id.json()["id"] == first["id"]
    by_name = client.get(f"/api/recipes/{second['name']}")
    assert by_name.status_code == 200
    assert by_name.json()["id"] == second["id"]
    assert client.get("/api/recipes/0").status_code == 404
