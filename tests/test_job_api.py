"""The unified job API: one JSON door to every generator and tool.

The contract under test is parity plus strictness. A job queued through
`POST /api/jobs` must persist exactly the params the form route would have,
so nothing downstream (rerun, retry, the queue, Variant Sets) can tell them
apart; and a program's mistake must be a 400 that names it, never a silent
clamp to a default.
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from backend.app import db
from backend.app import queue as qmod
from backend.app.config import settings
from backend.app.generators import matting
from backend.app.models import JobStatus
from backend.app.routers import common, job_api, tools
from backend.app.variant_sets import operations


@pytest.fixture()
def enqueued(monkeypatch):
    calls: list[int] = []

    async def record(kind, job_id, handler):
        calls.append(job_id)

    monkeypatch.setattr(common, "enqueue", record)
    return calls


def _image(client, color=(120, 130, 140), size=(64, 48), mode="RGB", name="api-src.png"):
    settings.ensure_dirs()
    path = settings.images_dir / name
    Image.new(mode, size, color).save(path)
    return db.add_asset("image", path, width=size[0], height=size[1],
                        generator="local_image:txt2img", meta={"prompt": "src", "seed": 7})


@pytest.fixture()
def source(client):
    return _image(client)


def _job(client, **body):
    return client.post("/api/jobs", json=body)


def _params(response):
    assert response.status_code == 200, response.text
    return db.get_job(response.json()["job_id"]).params


# ---- discovery --------------------------------------------------------------------------------
def test_kinds_describe_every_generator_and_tool(client):
    kinds = {k["kind"]: k for k in client.get("/api/jobs/kinds").json()}
    for kind in ("image_local", "image_colab", "img2img", "inpaint", "outpaint", "image_edit",
                 "t2v", "i2v", "long_video"):
        assert kinds[kind]["category"] == "generator"
    for kind in tools.TOOLS:
        assert kinds[kind]["category"] == "tool"
    edit = kinds["image_edit"]
    assert edit["inputs"]["images"] == {"min": 1, "max": 3, "asset_kind": "image"}
    assert edit["lane"] == "remote"
    assert kinds["inpaint"]["inputs"]["mask"] == "required"
    assert kinds["interpolate"]["inputs"]["images"]["asset_kind"] == "video"
    assert kinds["matte"]["lane"] == "local"


def test_kinds_schema_is_the_registry(client):
    """The published schema is derived, so it cannot drift from what is enforced."""
    kinds = {k["kind"]: k for k in client.get("/api/jobs/kinds").json()}
    for kind, spec in operations.registry_specs().items():
        schema = kinds[kind]["params"]
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == {c["name"] for c in spec["controls"]}
    quality = kinds["image_edit"]["params"]["properties"]["quality"]
    assert "Standard" in quality["enum"] and quality["default"] == "Standard"
    assert kinds["upscale"]["params"]["properties"]["scale"]["enum"] == [2, 4]
    assert kinds["image_local"]["params"]["properties"]["finish_steps"]["type"] == "array"


def test_kinds_route_is_not_shadowed_by_job_detail(client):
    # /jobs/{job_id} takes an int; if it matched first this would be a 422.
    assert client.get("/api/jobs/kinds").status_code == 200


def test_input_tables_agree_with_variant_operations():
    """A Variant Set child and an API job of the same kind take the same inputs."""
    for kind, op in operations.OPERATIONS.items():
        inputs = job_api.GENERATOR_INPUTS[kind]
        assert (inputs.min_images, inputs.max_images) == (op.min_sources, op.max_sources), kind
        assert (inputs.mask == "required") == (op.mask == "required"), kind


# ---- parity with the form routes ---------------------------------------------------------------
def _without(params, *keys):
    return {k: v for k, v in params.items() if k not in keys}


def test_image_edit_matches_the_upload_route(client, enqueued, source):
    settings_ = {"prompt": "Make it red", "quality": "High", "guidance": 3.5, "seed": 11}
    api = _params(_job(client, kind="image_edit", params=settings_,
                       inputs={"images": [source.id]}))
    with open(source.path, "rb") as fh:
        form = client.post("/api/generate/image/edit", data={"payload": json.dumps(settings_)},
                           files={"image": ("s.png", fh.read(), "image/png")})
    assert api["image_paths"] == [source.path], "a gallery asset is used in place, not copied"
    assert _without(api, "image_paths") == _without(_params(form), "image_paths")


def test_tool_matches_its_route(client, enqueued, source):
    api = _params(_job(client, kind="upscale", params={"scale": 2},
                       inputs={"images": [source.id]}))
    route = _params(client.post("/api/tools/upscale", json={"asset_id": source.id, "scale": 2}))
    assert api == route
    assert api["scale"] == 2 and api["source_asset_id"] == source.id


def test_defaults_fill_what_a_request_leaves_out(client, enqueued):
    params = _params(_job(client, kind="image_local", params={"prompt": "a cup"}))
    assert params["prompt"] == "a cup"
    assert params["quality"] == "Standard" and params["batch"] == 1


def test_video_kinds_build_through_the_video_builder(client, enqueued, source):
    t2v = _params(_job(client, kind="t2v", params={"prompt": "waves", "num_frames": 50}))
    assert t2v["num_frames"] == 49, "Wan's 4k+1 rule still applies"
    i2v = _params(_job(client, kind="i2v", params={"prompt": "drift"},
                       inputs={"images": [source.id]}))
    assert i2v["image_path"] == source.path


def test_combinatorial_prompt_fans_out(client, enqueued):
    r = _job(client, kind="image_colab",
             params={"prompt": "a {red|blue|green} cup", "combinatorial": True})
    body = r.json()
    assert r.status_code == 200 and len(body["job_ids"]) == 3 and body["group_id"]
    prompts = sorted(db.get_job(i).params["prompt"] for i in body["job_ids"])
    assert prompts == ["a blue cup", "a green cup", "a red cup"]


def test_finish_steps_are_validated_and_recorded(client, enqueued):
    steps = [{"processor": "resize", "width": 256, "height": 256, "mode": "contain",
              "background": "transparent"}]
    params = _params(_job(client, kind="image_local", params={"prompt": "p", "finish_steps": steps}))
    assert params["finish_steps"][0]["processor"] == "resize"
    bad = _job(client, kind="image_local",
               params={"prompt": "p", "finish_steps": [{"processor": "sparkle"}]})
    assert bad.status_code == 400 and "sparkle" in bad.json()["detail"]


# ---- idempotency ------------------------------------------------------------------------------
def test_request_id_returns_the_original_submission(client, enqueued, source):
    body = {"kind": "image_edit", "params": {"prompt": "once"},
            "inputs": {"images": [source.id]}, "request_id": "api-test-idem-00000001"}
    first = client.post("/api/jobs", json=body).json()
    again = client.post("/api/jobs", json=body).json()
    assert first["duplicate"] is False and again["duplicate"] is True
    assert again["job_id"] == first["job_id"] and len(enqueued) == 1


# ---- strictness -------------------------------------------------------------------------------
@pytest.mark.parametrize("body, status, fragment", [
    ({"kind": "teleport"}, 400, "unknown or unavailable kind 'teleport'"),
    ({"kind": "image_local", "params": {"promt": "typo"}},
     400, "'promt' is not a setting of image_local"),
    ({"kind": "image_local", "params": {"guidance": 99}}, 400, "guidance must be between"),
    ({"kind": "image_local", "params": {"quality": "Ultra"}}, 400, "quality must be one of"),
    ({"kind": "image_local", "params": {"batch": "four"}}, 400, "batch must be a number"),
    ({"kind": "image_local", "params": {"auto_negative": "yes"}}, 400, "true or false"),
    ({"kind": "image_local", "inputs": {"images": [1]}}, 400, "takes 0 input image(s); got 1"),
    ({"kind": "image_edit"}, 400, "takes 1-3 input image(s); got 0"),
    ({"kind": "t2v", "inputs": {"mask": 1}}, 400, "does not take a mask"),
    ({"kind": "detail", "params": {"targets": ["feet"]}, "inputs": {"images": [1]}},
     400, "targets must be a non-empty list of: face, hand"),
    ({"kind": "matte", "params": {"mode": "sparkle"}, "inputs": {"images": [1]}},
     400, "mode must be one of cutout, mask, inverse_mask"),
    ({"kind": "image_local", "request_id": "short"}, 400, "request_id must be"),
])
def test_mistakes_are_refused_with_a_reason(client, enqueued, source, body, status, fragment):
    before = len(db.list_jobs(limit=5000))
    r = client.post("/api/jobs", json=body)
    assert r.status_code == status, r.text
    assert fragment in r.json()["detail"]
    assert len(db.list_jobs(limit=5000)) == before


def test_unknown_body_fields_are_refused(client, enqueued):
    r = client.post("/api/jobs", json={"kind": "image_local", "parmas": {}})
    assert r.status_code == 422


def test_frame_limits_follow_the_engine(client, enqueued):
    """num_frames' range depends on `engine`, exactly as the form's slider does."""
    kinds = {k["kind"]: k for k in client.get("/api/jobs/kinds").json()}
    if "engine" not in kinds["t2v"]["params"]["properties"]:
        pytest.skip("no alternative video engine configured")
    assert _job(client, kind="t2v", params={"prompt": "p", "num_frames": 9}).status_code == 400
    ok = _job(client, kind="t2v", params={"prompt": "p", "engine": "hunyuan", "num_frames": 9})
    assert ok.status_code == 200, ok.text


def test_inputs_are_checked_against_the_library(client, enqueued, source):
    missing = _job(client, kind="image_edit", params={}, inputs={"images": [987654]})
    assert missing.status_code == 404 and "does not exist" in missing.json()["detail"]
    db.delete_assets([source.id])
    trashed = _job(client, kind="image_edit", params={}, inputs={"images": [source.id]})
    assert trashed.status_code == 404
    db.restore_assets([source.id])
    video = db.add_asset("video", settings.images_dir / "clip.mp4", generator="colab_wan:t2v")
    wrong = _job(client, kind="image_edit", inputs={"images": [video.id]})
    assert wrong.status_code == 400 and "is a video, not a image" in wrong.json()["detail"]


def test_inpaint_mask_must_match_its_image(client, enqueued, source):
    small = _image(client, color=255, size=(32, 32), mode="L", name="api-mask-small.png")
    r = _job(client, kind="inpaint", params={"prompt": "p"},
             inputs={"images": [source.id], "mask": small.id})
    assert r.status_code == 400 and "must match its image" in r.json()["detail"]
    fits = _image(client, color=255, size=(64, 48), mode="L", name="api-mask-fits.png")
    params = _params(_job(client, kind="inpaint", params={"prompt": "p"},
                          inputs={"images": [source.id], "mask": fits.id}))
    assert params["mask_path"] == fits.path and params["image_path"] == source.path
    need = _job(client, kind="inpaint", params={"prompt": "p"}, inputs={"images": [source.id]})
    assert need.status_code == 400 and "needs inputs.mask" in need.json()["detail"]


# ---- waiting ----------------------------------------------------------------------------------
def test_wait_returns_the_settled_job_with_its_outputs(client, enqueued, source):
    jid = client.post("/api/jobs", json={"kind": "image_local",
                                         "params": {"prompt": "w"}}).json()["job_id"]
    pending = client.get(f"/api/jobs/{jid}/wait", params={"timeout": 0}).json()
    assert pending["settled"] is False and pending["job"]["status"] == "queued"
    out = _image(client, name="api-out.png")
    db.update_asset(out.id, job_id=jid)
    db.mark_done(jid, {"asset_ids": [out.id]})
    done = client.get(f"/api/jobs/{jid}/wait", params={"timeout": 5}).json()
    assert done["settled"] is True and done["job"]["status"] == JobStatus.done.value
    assert [a["id"] for a in done["assets"]] == [out.id]
    assert client.get("/api/jobs/99999999/wait").status_code == 404
    assert client.get(f"/api/jobs/{jid}/wait", params={"timeout": 999}).status_code == 422


# ---- the matte tool ---------------------------------------------------------------------------
@pytest.fixture()
def fake_matte(monkeypatch):
    def predict(img):
        mask = Image.new("L", img.size, 0)
        ImageDraw.Draw(mask).ellipse((16, 8, 48, 40), fill=255)
        return mask

    monkeypatch.setattr(matting, "unavailable_reason", lambda: None)
    monkeypatch.setattr(matting, "predict_matte", predict)


def _run(job_id: int) -> dict:
    job = db.get_job(job_id)
    lane = qmod.JobQueue("test")
    asyncio.run(lane._execute(job_id, common.get_handler(job.kind), job.kind))
    return db.get_job(job_id).result


@pytest.mark.parametrize("mode, generator, pixel_mode", [
    ("cutout", "matte:cutout", "RGBA"),
    ("mask", "mask", "L"),
    ("inverse_mask", "mask", "L"),
])
def test_matte_makes_a_cutout_or_a_reusable_mask(client, enqueued, fake_matte, source,
                                                 mode, generator, pixel_mode):
    jid = _job(client, kind="matte", params={"mode": mode},
               inputs={"images": [source.id]}).json()["job_id"]
    assert qmod.lane_for("matte") == "local"
    result = _run(jid)
    asset = db.get_asset(result["asset_ids"][0])
    assert asset.generator == generator
    with Image.open(asset.path) as img:
        assert img.mode == pixel_mode and img.size == (64, 48)
        centre, corner = (32, 24), (0, 0)
        if mode == "cutout":
            assert img.getpixel(centre)[3] == 255 and img.getpixel(corner)[3] == 0
        elif mode == "mask":
            assert img.getpixel(centre) == 255 and img.getpixel(corner) == 0
        else:
            assert img.getpixel(centre) == 0 and img.getpixel(corner) == 255
    if generator == "mask":
        assert asset.meta["mask"] == {"source": "matte", "mode": mode,
                                      "source_asset_id": source.id}


def test_matte_is_refused_up_front_when_it_cannot_run(client, enqueued, monkeypatch, source):
    monkeypatch.setattr(matting, "unavailable_reason", lambda: "needs onnxruntime")
    r = _job(client, kind="matte", inputs={"images": [source.id]})
    assert r.status_code == 503 and "onnxruntime" in r.json()["detail"]
    kinds = {k["kind"]: k for k in client.get("/api/jobs/kinds").json()}
    assert kinds["matte"]["available"] is False


# ---- import and download ----------------------------------------------------------------------
def _png(mode="RGBA", size=(40, 30), color=(10, 20, 30, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def test_import_keeps_transparency_and_is_usable_by_id(client, enqueued):
    r = client.post("/api/assets/import", files={"file": ("cut out.png", _png(), "image/png")},
                    data={"tags": "product, cutout"})
    assert r.status_code == 200, r.text
    asset = r.json()
    assert asset["generator"] == "import" and asset["tags"] == ["product", "cutout"]
    assert asset["meta"]["import"] == {"filename": "cut out.png", "mode": "RGBA"}
    stored = db.get_asset(asset["id"])
    with Image.open(stored.path) as img:
        assert img.mode == "RGBA" and img.getpixel((0, 0))[3] == 0
    job = _job(client, kind="image_edit", params={"prompt": "p"}, inputs={"images": [asset["id"]]})
    assert job.status_code == 200


def test_import_as_mask_and_into_a_collection(client):
    collection = client.post("/api/collections", json={"name": "Imported"}).json()
    r = client.post("/api/assets/import",
                    files={"file": ("m.png", _png("L", color=255), "image/png")},
                    data={"role": "mask", "collection_id": str(collection["id"])})
    assert r.status_code == 200, r.text
    assert r.json()["generator"] == "mask"
    members = client.get("/api/assets", params={"collection_id": collection["id"]}).json()
    assert [a["id"] for a in members["items"]] == [r.json()["id"]]
    empty = client.post("/api/assets/import",
                        files={"file": ("m.png", _png("L", color=0), "image/png")},
                        data={"role": "mask"})
    assert empty.status_code == 400 and "empty" in empty.json()["detail"]


@pytest.mark.parametrize("payload, fragment", [
    (b"", "empty upload"),
    (b"not an image", "not a valid image"),
])
def test_import_refuses_bad_files(client, payload, fragment):
    r = client.post("/api/assets/import", files={"file": ("x.png", payload, "image/png")})
    assert r.status_code == 400 and fragment in r.json()["detail"]


def test_file_download_serves_live_assets_only(client, source):
    r = client.get(f"/api/assets/{source.id}/file")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content == Path(source.path).read_bytes()
    attached = client.get(f"/api/assets/{source.id}/file", params={"download": True})
    assert attached.headers["content-disposition"].startswith("attachment")
    db.delete_assets([source.id])
    assert client.get(f"/api/assets/{source.id}/file").status_code == 404
    db.restore_assets([source.id])


def test_header_token_reaches_files_through_the_api(client, source, monkeypatch):
    """With API_TOKEN set, /files needs the browser cookie; the API route takes the header."""
    monkeypatch.setattr(settings, "api_token", "t0ken-for-tests")
    assert client.get(f"/api/assets/{source.id}/file").status_code == 401
    ok = client.get(f"/api/assets/{source.id}/file", headers={"X-API-Token": "t0ken-for-tests"})
    assert ok.status_code == 200
    filename = source.path.rsplit("/", 1)[-1]
    headers = {"X-API-Token": "t0ken-for-tests"}
    assert client.get(f"/files/images/{filename}", headers=headers).status_code == 401


# ---- the published contract --------------------------------------------------------------------
def test_openapi_describes_responses_not_just_requests(client):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]

    def response_ref(path, method):
        content = paths[path][method]["responses"]["200"]["content"]["application/json"]
        return json.dumps(content["schema"])

    assert "JobSubmission" in response_ref("/api/jobs", "post")
    assert "JobKindInfo" in response_ref("/api/jobs/kinds", "get")
    assert "JobWait" in response_ref("/api/jobs/{job_id}/wait", "get")
    assert "SetDetail" in response_ref("/api/variant-sets/{set_id}", "get")
    assert "SetWait" in response_ref("/api/variant-sets/{set_id}/wait", "get")
    assert "AssetRead" in response_ref("/api/assets/import", "post")
    assert {t["name"] for t in spec["tags"]} >= {"jobs", "variant-sets", "assets"}


def test_the_api_guide_covers_every_kind():
    """docs/api.md's kind table is prose; this keeps it from drifting."""
    guide = (Path(__file__).resolve().parent.parent / "docs" / "api.md").read_text()
    for kind in [*job_api.GENERATOR_INPUTS, *tools.TOOLS]:
        assert f"`{kind}`" in guide, f"docs/api.md does not mention {kind}"
