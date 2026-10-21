"""OpenAI image routes, mapped onto the same jobs as POST /api/jobs.

The runner is either left queued (timeout 0) so the stored params can be read,
or faked with a tiny PNG so the official SDK can parse a finished response.
Nothing here imports a model.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io

import pytest
from PIL import Image

from backend.app import db
from backend.app.agent.openai_compat import friendly_id, raw_id
from backend.app.config import settings
from backend.app.generators import variants
from backend.app.routers import common, job_api
from backend.app.routers.common import persist_image

# Documented in openai_compat and docs/agents.md. A new catalogue row fails this
# until both the map and the docs name it.
_IDS = {
    ("local", "turbo"): ("brush-fast", "local/turbo"),
    ("local", "quality"): ("brush-quality", "local/quality"),
    ("local", "klein"): ("brush-klein", "local/klein"),
    ("local", "sdxl"): ("brush-sdxl", "local/sdxl"),
    ("local", "chroma"): ("brush-chroma", "local/chroma"),
    ("local", "flux"): ("brush-flux", "local/flux"),
    ("colab", "quality"): ("brush-remote", "remote/quality"),
    ("colab", "hidream"): ("brush-hidream", "remote/hidream"),
    ("colab", "alt"): ("brush-alt", "remote/alt"),
}


def _png(color=(20, 40, 60), size=(32, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _data_url(data: bytes | None = None) -> str:
    return "data:image/png;base64," + base64.b64encode(data or _png()).decode()


def _error(response):
    body = response.json()
    assert set(body) == {"error"}
    err = body["error"]
    assert {"message", "type", "param", "code"} <= set(err)
    return err


@pytest.fixture()
def enqueued(monkeypatch):
    calls: list[int] = []

    async def record(kind, job_id, handler):
        calls.append(job_id)

    monkeypatch.setattr(common, "enqueue", record)
    return calls


@pytest.fixture()
def impatient(monkeypatch):
    monkeypatch.setattr(settings, "openai_compat_timeout", 0)


@pytest.fixture()
def remote_choice(monkeypatch):
    """Two configured remote slots, so the image model picker is offered."""
    monkeypatch.setattr(settings, "a100_image_model", "Qwen/Qwen-Image-2512")
    monkeypatch.setattr(settings, "a100_image_model_hidream", "HiDream-ai/HiDream-O1-Image")


def _install_runner(monkeypatch):
    async def enqueue(kind, job_id, handler):
        async def run() -> None:
            db.mark_running(job_id)
            asset_id = persist_image(
                Image.new("RGBA", (8, 8), (10, 20, 30, 255)),
                job_id=job_id, generator=kind, meta={"seed": 1}, tag="openai",
            )
            db.mark_done(job_id, {"asset_ids": [asset_id]})

        asyncio.get_running_loop().create_task(run())

    monkeypatch.setattr(common, "enqueue", enqueue)
    monkeypatch.setattr(job_api, "_POLL_SECONDS", 0.01)


def _generate(client, **body):
    return client.post("/api/openai/v1/images/generations", json=body)


def _queued(client, **body):
    response = _generate(client, **body)
    assert response.status_code == 504, response.text
    job = db.get_job(response.json()["error"]["job_id"])
    assert job is not None
    return response, job


def test_friendly_ids_match_the_documented_map():
    seen = {(variant.lane, variant.name) for variant in variants.VARIANTS}
    assert seen == set(_IDS)
    for variant in variants.VARIANTS:
        friendly, raw = _IDS[(variant.lane, variant.name)]
        assert friendly_id(variant) == friendly
        assert raw_id(variant) == raw


def test_models_lists_friendly_ids_and_catalogue_notes(client):
    response = client.get("/api/openai/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    rows = {row["id"]: row for row in body["data"]}
    configured = [*variants.available("local"), *variants.available("colab")]
    assert list(rows) == [friendly_id(variant) for variant in configured]
    for variant in configured:
        row = rows[friendly_id(variant)]
        assert row["object"] == "model"
        assert row["owned_by"] == "brush"
        assert row["description"] == variant.note
        assert row["brush"]["raw_id"] == raw_id(variant)
        assert raw_id(variant) not in rows


def test_generations_map_friendly_and_raw_ids(client, enqueued, impatient, remote_choice):
    _, turbo = _queued(client, model="brush-fast", prompt="a cup")
    assert turbo.kind == "image_local"
    assert turbo.params["model_variant"] == "turbo"
    assert turbo.client == "openai"

    _, raw = _queued(client, model="local/turbo", prompt="a cup")
    assert raw.params["model_variant"] == "turbo"

    _, quality = _queued(client, model="brush-quality", prompt="a cup")
    assert quality.params["model_variant"] == "quality"

    _, remote = _queued(client, model="brush-remote", prompt="a cup")
    assert remote.kind == "image_colab"
    assert remote.params["model_variant"] == "quality"

    _, alias = _queued(client, model="colab/quality", prompt="a cup")
    assert alias.kind == "image_colab"
    assert alias.params["model_variant"] == "quality"

    _, hidream = _queued(client, model="remote/hidream", prompt="a cup")
    assert hidream.params["model_variant"] == "hidream"
    assert len(enqueued) == 6


def test_unconfigured_and_unknown_models_are_refused(client, enqueued, monkeypatch):
    monkeypatch.setattr(settings, "local_image_model_klein", "")
    missing = _generate(client, model="local/klein", prompt="a cup")
    err = _error(missing)
    assert missing.status_code == 400
    assert err["code"] == "invalid_option"
    assert err["param"] == "model"
    assert err["type"] == "invalid_request_error"
    assert "brush-fast" in err["message"]

    unknown = _generate(client, model="dall-e-3", prompt="a cup")
    assert unknown.status_code == 400
    assert _error(unknown)["param"] == "model"
    assert enqueued == []

    monkeypatch.setattr(settings, "local_image_model_klein", "org/FLUX.2-klein-4B")
    monkeypatch.setattr(settings, "openai_compat_timeout", 0)
    _, klein = _queued(client, model="brush-klein", prompt="a cup")
    assert klein.kind == "image_local"
    assert klein.params["model_variant"] == "klein"


def test_size_n_background_and_ignored_fields(client, enqueued, impatient, remote_choice):
    wide = _queued(client, model="brush-fast", prompt="a cup", size="16:9")[1]
    assert wide.params["aspect"] == "16:9"

    pixels = _queued(client, model="local/quality", prompt="a cup", size="1536x1024")[1]
    assert pixels.params["aspect"] == "Custom"
    # The device bucket may shrink the request. The ratio is what WxH asked for.
    assert pixels.params["width"] % 16 == 0
    assert pixels.params["height"] % 16 == 0
    assert pixels.params["width"] / pixels.params["height"] == pytest.approx(1.5, rel=0.03)

    tiny = _generate(client, model="brush-fast", prompt="a cup", size="8x8")
    assert tiny.status_code == 400
    assert _error(tiny)["code"] == "out_of_range"
    assert _error(tiny)["param"] == "size"

    over = _generate(client, model="brush-fast", prompt="a cup", n=9)
    assert over.status_code == 400
    err = _error(over)
    assert err["code"] == "out_of_range"
    assert err["param"] == "n"
    assert err["type"] == "invalid_request_error"

    flagged = _generate(client, model="brush-fast", prompt="a cup", n=True)
    assert flagged.status_code == 400
    assert _error(flagged)["code"] == "invalid_type"

    streamed = _generate(client, model="brush-fast", prompt="a cup", stream=True)
    assert streamed.status_code == 400
    assert _error(streamed)["code"] == "unsupported_parameter"
    assert _error(streamed)["param"] == "stream"

    jpeg = _generate(client, model="brush-fast", prompt="a cup", output_format="jpeg")
    assert jpeg.status_code == 400
    assert _error(jpeg)["param"] == "output_format"

    odd = _generate(client, model="brush-fast", prompt="a cup", response_format="gif")
    assert odd.status_code == 400
    assert _error(odd)["param"] == "response_format"

    warned, job = _queued(
        client, model="brush-fast", prompt="a cup", quality="hd", style="vivid", user="sam",
    )
    assert warned.headers["x-brush-warning"] == "ignored quality, style"
    assert job.params.get("quality") != "hd"

    local_clear = _queued(client, model="brush-fast", prompt="a cup", background="transparent")[1]
    steps = local_clear.params.get("finish_steps") or []
    assert any(step.get("processor") == "background_removal" for step in steps)

    remote_clear = _queued(client, model="brush-remote", prompt="a cup", background="transparent")[1]
    assert remote_clear.params.get("background") == "transparent"
    remote_steps = remote_clear.params.get("finish_steps") or []
    assert any(step.get("processor") == "background_removal" for step in remote_steps)
    assert enqueued  # the refusals above never queued


def test_idempotency_key_is_a_request_id(client, enqueued, impatient):
    short = "cup"
    first = _queued(client, model="brush-fast", prompt="a cup")
    assert first[1].request_id is None

    hashed = client.post(
        "/api/openai/v1/images/generations",
        json={"model": "brush-fast", "prompt": "a cup"},
        headers={"Idempotency-Key": short},
    )
    assert hashed.status_code == 504
    job_id = hashed.json()["error"]["job_id"]
    stored = db.get_job(job_id)
    assert stored is not None
    assert stored.request_id == hashlib.sha256(short.encode()).hexdigest()

    again = client.post(
        "/api/openai/v1/images/generations",
        json={"model": "brush-fast", "prompt": "something else"},
        headers={"Idempotency-Key": short},
    )
    assert again.status_code == 504
    assert again.json()["error"]["job_id"] == job_id
    assert again.json()["error"]["type"] == "timeout"
    assert again.json()["error"]["code"] == "timeout"

    fitting = "openai-idem-00000001"
    kept = client.post(
        "/api/openai/v1/images/generations",
        json={"model": "brush-fast", "prompt": "a cup"},
        headers={"Idempotency-Key": fitting},
    )
    kept_job = db.get_job(kept.json()["error"]["job_id"])
    assert kept_job is not None
    assert kept_job.request_id == fitting
    assert enqueued == [first[1].id, job_id, kept_job.id]


def test_edits_reject_http_urls_and_map_mask(client, enqueued, impatient, remote_choice):
    _, before = db.search_assets(limit=1)
    url = client.post("/api/openai/v1/images/edits", json={
        "model": "brush-fast",
        "prompt": "repaint",
        "images": [{"image_url": "https://example.test/a.png"}],
    })
    assert url.status_code == 400
    err = _error(url)
    assert err["code"] == "invalid_request"
    assert err["param"] == "image"
    assert "http" in err["message"]
    _, after = db.search_assets(limit=1)
    assert after == before
    assert enqueued == []

    source = _data_url()
    mask = _data_url(_png((255, 255, 255)))
    local = client.post("/api/openai/v1/images/edits", json={
        "model": "brush-fast", "prompt": "repaint", "image": source,
    })
    assert local.status_code == 504
    local_job = db.get_job(local.json()["error"]["job_id"])
    assert local_job is not None and local_job.kind == "img2img"

    painted = client.post("/api/openai/v1/images/edits", json={
        "model": "local/quality", "prompt": "repaint", "image": source, "mask": mask,
    })
    painted_job = db.get_job(painted.json()["error"]["job_id"])
    assert painted_job is not None and painted_job.kind == "inpaint"

    remote = client.post("/api/openai/v1/images/edits", json={
        "model": "brush-remote", "prompt": "make it red", "image": source,
    })
    remote_job = db.get_job(remote.json()["error"]["job_id"])
    assert remote_job is not None and remote_job.kind == "image_edit"
    # No picker on this kind: the stored variant is the lane default.
    assert remote_job.params["model_variant"] == "quality"

    other = client.post("/api/openai/v1/images/edits", json={
        "model": "brush-hidream", "prompt": "make it red", "image": source,
    })
    assert "edit model" in other.headers["x-brush-warning"]
    other_job = db.get_job(other.json()["error"]["job_id"])
    assert other_job is not None and other_job.kind == "image_edit"
    assert other_job.params["model_variant"] == "quality"

    masked = client.post("/api/openai/v1/images/edits", json={
        "model": "remote/quality", "prompt": "repaint", "image": source, "mask": mask,
    })
    masked_job = db.get_job(masked.json()["error"]["job_id"])
    assert masked_job is not None and masked_job.kind == "inpaint_remote"

    too_many = client.post("/api/openai/v1/images/edits", json={
        "model": "brush-fast", "prompt": "repaint", "images": [source, source],
    })
    assert too_many.status_code == 400

    counted = client.post("/api/openai/v1/images/edits", json={
        "model": "brush-remote", "prompt": "repaint", "image": source, "n": 2,
    })
    assert counted.status_code == 400
    assert _error(counted)["param"] == "n"

    files = {
        "image": ("cup.png", _png(), "image/png"),
        "mask": ("mask.png", _png((255, 255, 255)), "image/png"),
    }
    form = client.post(
        "/api/openai/v1/images/edits",
        data={"model": "brush-fast", "prompt": "change the cup", "n": "1"},
        files=files,
    )
    assert form.status_code == 504, form.text
    form_job = db.get_job(form.json()["error"]["job_id"])
    assert form_job is not None and form_job.kind == "inpaint"


def test_finished_response_keeps_sdk_size_and_brush_pixels(client, monkeypatch):
    _install_runner(monkeypatch)
    response = _generate(
        client, model="brush-fast", prompt="a ceramic cup", size="1536x1024",
        response_format="b64_json",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["output_format"] == "png"
    assert body["size"] == "1536x1024"
    assert body["brush"]["size"] == "8x8"
    assert body["brush"]["job_ids"]
    raw = base64.b64decode(body["data"][0]["b64_json"])
    assert raw.startswith(b"\x89PNG")
    job = db.get_job(body["brush"]["job_ids"][0])
    assert job is not None and job.client == "openai"

    ratio = _generate(client, model="brush-fast", prompt="a cup", size="16:9")
    assert ratio.status_code == 200
    assert "size" not in ratio.json()
    assert ratio.json()["brush"]["size"] == "8x8"

    linked = _generate(
        client, model="brush-fast", prompt="a cup", response_format="url",
    )
    url = linked.json()["data"][0]["url"]
    assert url.startswith("http://localhost/api/assets/")
    assert url.endswith("/file")


def test_token_name_labels_the_openai_job(client, enqueued, impatient, monkeypatch):
    monkeypatch.setattr(settings, "api_token_name", "studio laptop")
    _, job = _queued(client, model="brush-fast", prompt="a cup")
    assert job.client == "studio laptop"


def test_bare_v1_is_not_the_model_list(client):
    response = client.get("/v1/models")
    if "application/json" in response.headers.get("content-type", ""):
        assert response.json().get("object") != "list"
    else:
        assert response.status_code != 200 or "text/html" in response.headers.get("content-type", "")


def test_official_sdk_generate_and_edit(client, monkeypatch):
    """The real SDK, over httpx's ASGI transport. Sync httpx cannot drive it."""
    _install_runner(monkeypatch)
    import anyio
    import httpx
    from openai import AsyncOpenAI

    async def run() -> None:
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as http_client:
            sdk = AsyncOpenAI(
                base_url="http://localhost/api/openai/v1",
                api_key="test-key",
                http_client=http_client,
            )
            generated = await sdk.images.generate(
                model="brush-fast", prompt="a ceramic cup", size="1536x1024",
            )
            assert generated.data
            assert generated.data[0].b64_json
            raw = base64.b64decode(generated.data[0].b64_json)
            assert raw.startswith(b"\x89PNG")
            brush = (generated.model_extra or {}).get("brush") or {}
            assert brush.get("job_ids")
            assert brush.get("size") == "8x8"

            source = io.BytesIO(_png())
            source.name = "cup.png"
            mask = io.BytesIO(_png((255, 255, 255)))
            mask.name = "mask.png"
            edited = await sdk.images.edit(
                model="brush-fast", prompt="move the handle", image=source, mask=mask,
            )
            assert edited.data and edited.data[0].b64_json
            edited_brush = (edited.model_extra or {}).get("brush") or {}
            assert edited_brush.get("job_ids")
            row = db.get_job(edited_brush["job_ids"][0])
            assert row is not None
            assert row.kind == "inpaint"
            assert row.client == "openai"

    anyio.run(run)
