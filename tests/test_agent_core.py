"""The agent façade submits the same jobs the HTTP API does.

Structured refusals and dry runs live on that API, so an MCP tool and a
direct POST cannot drift.
"""
from __future__ import annotations

import anyio

from backend.app import db
from backend.app.agent import core
from backend.app.routers.job_api import JobCreate


def test_core_submit_persists_the_same_params_as_http(client, no_queue):
    http = client.post("/api/jobs", json={
        "kind": "image_local", "params": {"prompt": "a ceramic cup", "seed": 7},
    })
    assert http.status_code == 200, http.text

    async def via_core():
        return await core.submit(JobCreate(
            kind="image_local", params={"prompt": "a ceramic cup", "seed": 7},
        ))

    submitted = anyio.run(via_core)
    assert db.get_job(http.json()["job_id"]).params == db.get_job(submitted.job_id).params
    assert db.get_job(submitted.job_id).client == "api"


def test_unknown_setting_names_the_field_and_a_suggestion(client):
    response = client.post("/api/jobs", json={
        "kind": "image_local", "params": {"promt": "a cup"},
    })
    assert response.status_code == 400
    body = response.json()
    assert isinstance(body["detail"], str)
    assert "promt" in body["detail"]
    assert body["code"] == "unknown_setting"
    assert body["field"] == "promt"
    assert "prompt" in body["valid_options"]
    assert body["suggestions"] == ["Did you mean 'prompt'?"]


def test_out_of_range_is_structured(client):
    response = client.post("/api/jobs", json={
        "kind": "image_local", "params": {"prompt": "a cup", "steps": 0},
    })
    assert response.status_code == 400
    body = response.json()
    assert isinstance(body["detail"], str)
    assert "must be between" in body["detail"]
    assert body["code"] == "out_of_range"
    assert body["field"] == "steps"


def test_dry_run_does_not_queue(client, no_queue):
    before = {job.id for job in db.list_jobs(limit=100_000)}
    response = client.post("/api/jobs", json={
        "kind": "image_local",
        "params": {"prompt": "{red|blue} cup", "combinatorial": True, "seed": 3},
        "dry_run": True,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["would_queue"] == 2
    assert body["params"]["prompt"] == "{red|blue} cup"
    assert body["job_ids"] == []
    after = {job.id for job in db.list_jobs(limit=100_000)}
    assert after == before


def test_ui_origin_and_token_name_are_stored(client, no_queue, monkeypatch):
    from backend.app.config import settings

    ui = client.post(
        "/api/jobs",
        json={"kind": "image_local", "params": {"prompt": "from the form", "seed": 1}},
        headers={"Origin": "http://localhost"},
    )
    assert ui.status_code == 200, ui.text
    job = client.get(f"/api/jobs/{ui.json()['job_id']}").json()
    assert job["client"] == "ui"

    monkeypatch.setattr(settings, "api_token", "tok123")
    monkeypatch.setattr(settings, "api_token_name", "studio laptop")
    named = client.post(
        "/api/jobs",
        json={"kind": "image_local", "params": {"prompt": "from a script", "seed": 2}},
        headers={"X-API-Token": "tok123"},
    )
    assert named.status_code == 200, named.text
    named_view = client.get(
        f"/api/jobs/{named.json()['job_id']}", headers={"X-API-Token": "tok123"},
    )
    assert named_view.status_code == 200, named_view.text
    assert named_view.json()["client"] == "studio laptop"

    monkeypatch.setattr(settings, "api_token_name", "has a bang!")
    unnamed = client.post(
        "/api/jobs",
        json={"kind": "image_local", "params": {"prompt": "unnamed token", "seed": 3}},
        headers={"Authorization": "Bearer tok123"},
    )
    assert unnamed.status_code == 200, unnamed.text
    assert db.get_job(unnamed.json()["job_id"]).client == "api"


def test_output_metadata_copies_the_client(client, no_queue):
    from PIL import Image

    from backend.app.routers.common import persist_image

    queued = client.post(
        "/api/jobs",
        json={"kind": "image_local", "params": {"prompt": "tagged", "seed": 4}},
        headers={"Origin": "http://localhost"},
    )
    assert queued.status_code == 200, queued.text
    job_id = queued.json()["job_id"]
    asset_id = persist_image(
        Image.new("RGB", (4, 4), "white"),
        job_id=job_id, generator="local_image:txt2img", meta={"seed": 4}, tag="tagged",
    )
    asset = client.get(f"/api/assets/{asset_id}").json()
    assert asset["client"] == "ui"
    assert asset["meta"]["client"] == "ui"


def test_missing_job_wait_is_structured(client):
    response = client.get("/api/jobs/999999/wait")
    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "job not found"
    assert body["code"] == "not_found"
    assert body["field"] == "job_id"
    assert body["suggestions"]


def test_decode_data_url_refuses_http():
    import pytest

    from backend.app.problems import RequestError

    with pytest.raises(RequestError) as caught:
        core.decode_data_url("https://example.test/a.png")
    assert caught.value.code == "invalid_request"
    assert caught.value.field == "images"


def test_describe_unknown_kind_suggests_a_close_name():
    import pytest

    from backend.app.problems import RequestError

    async def ask():
        await core.describe("image_locl")

    with pytest.raises(RequestError) as caught:
        anyio.run(ask)
    assert caught.value.code == "kind_unavailable"
    assert caught.value.field == "kind"
    assert "image_local" in (caught.value.valid_options or [])
    assert any("image_local" in item for item in caught.value.suggestions)
