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


def _table_counts() -> tuple[int, int]:
    from sqlmodel import select

    from backend.app.db import session
    from backend.app.models import Asset, Job

    with session() as handle:
        jobs = len(list(handle.exec(select(Job.id))))
        assets = len(list(handle.exec(select(Asset.id))))
    return jobs, assets


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
    jobs_before, assets_before = _table_counts()

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
    assert _table_counts() == (jobs_before, assets_before)

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


def test_a_numeric_key_is_an_id_and_digit_only_names_are_refused(client):
    first = _save(
        client, name=_name("Numbered"), kind="image_local",
        params={"prompt": "a cup"}, expose=["prompt"],
    )
    by_id = client.get(f"/api/recipes/{first['id']}")
    assert by_id.status_code == 200
    assert by_id.json()["id"] == first["id"]
    assert client.get("/api/recipes/0").status_code == 404
    huge = client.get("/api/recipes/" + ("9" * 30))
    assert huge.status_code == 404
    assert client.get("/api/recipes/%C2%B2").status_code == 404
    digits = client.post("/api/recipes", json={
        "name": "12345", "kind": "image_local", "params": {"prompt": "a cup"}, "expose": [],
    })
    assert digits.status_code == 400
    assert digits.json()["field"] == "name"
    slug = client.post("/api/recipes", json={
        "name": _name("Slug"), "slug": str(first["id"]),
        "kind": "image_local", "params": {"prompt": "a cup"}, "expose": [],
    })
    assert slug.status_code == 400
    assert slug.json()["field"] == "slug"


def test_guidance_follows_a_different_model_when_it_was_left_at_the_default(client):
    from backend.app.generators import variants

    offered = variants.available("local")
    source = offered[0]
    target = next(variant for variant in offered if variant.guidance != source.guidance)
    saved = _save(
        client, name=_name("Guidance"), kind="image_local",
        params={
            "prompt": "a cup",
            "model_variant": source.name,
            "guidance": source.guidance,
        },
        expose=["model_variant"],
    )
    dry = client.post(f"/api/recipes/{saved['slug']}/run", json={
        "inputs": {"model_variant": target.name}, "dry_run": True,
    })
    assert dry.status_code == 200, dry.text
    assert dry.json()["params"]["model_variant"] == target.name
    assert dry.json()["params"]["guidance"] == target.guidance


def test_live_limits_accept_hunyuan_frames_saved_against_wan(client, monkeypatch):
    import backend.app.generators.registry as reg

    monkeypatch.setattr(reg.settings, "hunyuan_video_model", "tencent/HunyuanVideo")
    monkeypatch.setattr(reg, "remote_gpu_seen", lambda: False)
    saved = _save(
        client, name=_name("Clip"), kind="t2v",
        params={"prompt": "a cup spinning", "engine": "wan", "num_frames": 49},
        expose=["engine", "num_frames"],
    )
    frames = next(field for field in saved["fields"] if field["name"] == "num_frames")
    assert frames["constraints"]["min"] == 25
    dry = client.post(f"/api/recipes/{saved['slug']}/run", json={
        "inputs": {"engine": "hunyuan", "num_frames": 9}, "dry_run": True,
    })
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["params"]["engine"] == "hunyuan"
    assert body["params"]["num_frames"] == 9


def test_recipes_and_mcp_recipe_tools_require_the_api_token(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "tok123")
    assert client.get("/api/recipes").status_code == 401
    refused = client.post("/api/recipes", json={
        "name": "Locked cup", "kind": "image_local", "params": {"prompt": "a cup"}, "expose": [],
    })
    assert refused.status_code == 401
    headers = {"Accept": "application/json, text/event-stream"}
    for name in ("list_recipes", "get_recipe", "save_recipe", "run_recipe"):
        call = client.post("/api/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        })
        assert call.status_code == 401, name
    allowed = client.get("/api/recipes", headers={"Authorization": "Bearer tok123"})
    assert allowed.status_code == 200
    opened = client.post("/api/mcp", headers={**headers, "Authorization": "Bearer tok123"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_recipes", "arguments": {}},
    })
    assert opened.status_code != 401


# ---- a saved generator resolves the same prompt as Generate -------------------------------
_WATERCOLOR = {"id": "watercolor", "text": "soft watercolour washes, paper texture"}
_WARM = {"id": "warm", "text": "warm late-afternoon light"}


def _form_job(client, payload):
    """What the Generate button does: the multipart route with a composed prompt."""
    import json as _json

    response = client.post("/api/generate/image/local", data={"payload": _json.dumps(payload)})
    assert response.status_code == 200, response.text
    return db.get_job(response.json()["job_id"])


def test_a_styled_saved_generator_resolves_the_prompt_generate_sends(client, enqueued, monkeypatch):
    """Section 11: same subject, same distinctive styles, same effective prompt -
    and the raw subject and fragments stay apart so reuse cannot double them."""
    from backend.app.routers import images

    monkeypatch.setattr(images, "require_local_gpu", lambda: None)
    subject = "a fox curled in snow"
    composed = f"{subject}, {_WATERCOLOR['text']}, {_WARM['text']}"
    form = _form_job(client, {"prompt": composed, "raw_prompt": subject,
                              "style_ids": ["watercolor", "warm"], "seed": 11})

    saved = _save(client, name=_name("Styled fox"), kind="image_local",
                  params={"prompt": "placeholder", "seed": 11}, expose=["prompt"],
                  styles=[_WATERCOLOR, _WARM])
    assert saved["styles"] == [_WATERCOLOR, _WARM]
    assert saved["params"]["prompt"] == "placeholder"          # the subject, never composed

    check = client.post(f"/api/recipes/{saved['slug']}/run",
                        json={"inputs": {"prompt": subject}, "dry_run": True})
    assert check.status_code == 200, check.text
    assert check.json()["params"]["prompt"] == form.params["prompt"] == composed

    ran = client.post(f"/api/recipes/{saved['slug']}/run", json={"inputs": {"prompt": subject}})
    job = db.get_job(ran.json()["job_id"])
    assert job.params["prompt"] == form.params["prompt"]
    assert job.params["raw_prompt"] == form.params["raw_prompt"] == subject
    assert job.params["style_ids"] == form.params["style_ids"] == ["watercolor", "warm"]


def test_saved_style_text_is_frozen_and_ordered(client):
    saved = _save(client, name=_name("Order"), kind="image_local", params={"prompt": "x"},
                  expose=["prompt"], styles=[_WARM, _WATERCOLOR])
    assert [s["id"] for s in client.get(f"/api/recipes/{saved['slug']}").json()["styles"]] == [
        "warm", "watercolor"]


@pytest.mark.parametrize("styles,field", [
    ([{"id": "a", "text": ""}], "styles.0.text"),
    ([{"id": "a", "text": "x"}, {"id": "a", "text": "y"}], "styles.1.id"),
])
def test_bad_styles_are_refused_with_their_path(client, styles, field):
    response = client.post("/api/recipes", json={
        "name": _name("Bad"), "kind": "image_local", "params": {"prompt": "x"},
        "expose": ["prompt"], "styles": styles})
    assert response.status_code == 400
    assert response.json()["field"] == field


def test_the_job_api_composes_and_refuses_an_ambiguous_prompt(client, enqueued, monkeypatch):
    from backend.app.routers import images

    monkeypatch.setattr(images, "require_local_gpu", lambda: None)
    both = client.post("/api/jobs", json={
        "kind": "image_local", "params": {"prompt": "a"},
        "composition": {"subject": "a", "styles": [_WARM]}})
    assert both.status_code == 400 and both.json()["code"] == "ambiguous_request"
    dry = client.post("/api/jobs", json={
        "kind": "image_local", "dry_run": True,
        "composition": {"subject": "a lighthouse", "styles": [_WARM]}})
    assert dry.status_code == 200, dry.text
    assert dry.json()["params"]["prompt"] == f"a lighthouse, {_WARM['text']}"
    assert dry.json()["params"]["raw_prompt"] == "a lighthouse"
    tool = client.post("/api/jobs", json={
        "kind": "upscale", "composition": {"subject": "x"}, "inputs": {"images": [1]}})
    assert tool.status_code == 400 and tool.json()["field"] == "composition"
