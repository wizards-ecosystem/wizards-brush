"""API-level tests over TestClient with the queue stubbed: param sanitization,
upload validation, jobs CRUD/rerun, assets/gallery, registry/presets payloads."""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from PIL import Image

from backend.app import db
from backend.app.models import JobStatus


def _png_bytes(size=(8, 8), color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _payload(**kw) -> dict:
    return {"payload": json.dumps(kw)}


def test_generate_local_sanitizes_params(client, no_queue):
    r = client.post("/api/generate/image/local", data=_payload(
        prompt="x" * 3000, batch=99, quality="Custom", steps=250,
        aspect="Custom", width=4000, height=2000, seed=5,
    ))
    assert r.status_code == 200
    job = db.get_job(r.json()["job_id"])
    p = job.params
    assert len(p["prompt"]) == 2000
    # Batch is capped by a VRAM budget now, not a flat count: this request is
    # for a ~1280x640 image, so 99 comes down to whatever fits rather than to 8.
    assert 1 <= p["batch"] <= 8
    assert p["steps"] == 100
    assert p["width"] <= 1280 and p["height"] <= 1280
    assert abs(p["width"] / p["height"] - 2.0) < 0.15  # ~2:1 preserved


def test_generate_preset_aspect_ignores_stale_custom_dims(client, no_queue):
    r = client.post("/api/generate/image/local", data=_payload(
        prompt="p", aspect="1:1", width=4000, height=1000,
    ))
    p = db.get_job(r.json()["job_id"]).params
    assert p["width"] == p["height"]  # square wins over stale custom W/H


def test_img2img_requires_valid_image(client, no_queue):
    files = {"image": ("x.png", b"not an image", "image/png")}
    r = client.post("/api/generate/image/img2img", data=_payload(prompt="p"), files=files)
    assert r.status_code == 400
    assert "not a valid image" in r.json()["detail"]

    files = {"image": ("x.png", b"", "image/png")}
    r = client.post("/api/generate/image/img2img", data=_payload(prompt="p"), files=files)
    assert r.status_code == 400

    files = {"image": ("x.png", _png_bytes(), "image/png")}
    r = client.post("/api/generate/image/img2img", data=_payload(prompt="p"), files=files)
    assert r.status_code == 200
    assert db.get_job(r.json()["job_id"]).params["image_path"]


def test_upload_dimensions_are_checked_before_full_decode(monkeypatch):
    from backend.app.routers import common

    monkeypatch.setattr(common, "MAX_UPLOAD_SIDE", 16)
    with pytest.raises(ValueError, match="dimensions exceed"):
        common.load_uploaded_image(_png_bytes((32, 8)))


def test_input_aspect_is_resolved_from_uploaded_image(client, no_queue):
    files = {"image": ("wide.png", _png_bytes((320, 160)), "image/png")}
    r = client.post("/api/generate/image/img2img",
                    data=_payload(prompt="p", aspect="Match input"), files=files)
    assert r.status_code == 200
    params = db.get_job(r.json()["job_id"]).params
    assert params["aspect"] == "Match input"
    assert abs(params["width"] / params["height"] - 2.0) < 0.05


def test_new_inpaint_records_non_destructive_region_defaults(client, no_queue):
    files = {
        "image": ("wide.png", _png_bytes((320, 160)), "image/png"),
        "mask": ("mask.png", _png_bytes((320, 160), (255, 255, 255)), "image/png"),
    }
    r = client.post("/api/generate/image/inpaint", data=_payload(prompt="replace"), files=files)
    assert r.status_code == 200
    params = db.get_job(r.json()["job_id"]).params
    assert params["aspect"] == "Match input"
    assert params["inpaint_area"] == "masked area"
    assert (params["mask_grow"], params["mask_padding"], params["mask_blur"]) == (4, 64, 4)
    assert abs(params["width"] / params["height"] - 2.0) < 0.05


def test_jobs_listing_detail_and_404(client, no_queue):
    r = client.post("/api/generate/image/local", data=_payload(prompt="listed"))
    jid = r.json()["job_id"]
    jobs = client.get("/api/jobs").json()
    assert any(j["id"] == jid for j in jobs)
    assert client.get(f"/api/jobs/{jid}").json()["params"]["prompt"] == "listed"
    assert client.get("/api/jobs/999999").status_code == 404


def test_job_rerun(client, no_queue):
    r = client.post("/api/generate/image/local", data=_payload(prompt="again", seed=42))
    jid = r.json()["job_id"]
    db.mark_error(jid, "test failure")
    r2 = client.post(f"/api/jobs/{jid}/rerun?reseed=false")
    assert r2.status_code == 200
    new = db.get_job(r2.json()["job_id"])
    assert new.params["prompt"] == "again" and new.params["seed"] == 42
    assert r2.json()["replaced_job_id"] == jid
    assert db.get_job(jid).status == JobStatus.canceled.value

    # Generate more is a branch, not a replacement: retain the failure row.
    r = client.post("/api/generate/image/local", data=_payload(prompt="again", seed=42))
    failed_id = r.json()["job_id"]
    db.mark_error(failed_id, "test failure")
    r3 = client.post(f"/api/jobs/{failed_id}/rerun?reseed=true")
    assert r3.status_code == 200
    assert db.get_job(failed_id).status == JobStatus.error.value
    assert db.get_job(r3.json()["job_id"]).params["seed"] != 42


def test_rerun_warns_when_the_saved_model_slot_now_resolves_differently(client, no_queue):
    r = client.post("/api/generate/image/local", data=_payload(prompt="again", seed=42))
    jid = r.json()["job_id"]
    db.update_job(jid, model_key="org/the-original-model")
    rerun = client.post(f"/api/jobs/{jid}/rerun?reseed=false")
    assert rerun.status_code == 200
    warning = rerun.json()["model_warning"]
    assert "org/the-original-model" in warning
    assert "follows the saved slot name" in warning


def test_cold_gated_model_is_rejected_before_queueing(client, no_queue, monkeypatch):
    from backend.app.backends import local
    from backend.app.config import settings

    monkeypatch.setattr(settings, "local_image_model_flux", "black-forest-labs/FLUX.1-dev")
    monkeypatch.setattr(settings, "hf_token", "")
    monkeypatch.setattr(local, "_cache_status", lambda _model: ("absent", None))
    r = client.post("/api/generate/image/local", data=_payload(
        prompt="gated", model_variant="flux",
    ))
    assert r.status_code == 409
    assert "HF_TOKEN" in r.json()["detail"]


def test_video_params_snap_frames(client, no_queue):
    for requested, expected in ((50, 49), (24, 25), (999, 121)):
        r = client.post("/api/generate/video/t2v", data=_payload(prompt="v", num_frames=requested))
        assert db.get_job(r.json()["job_id"]).params["num_frames"] == expected


def test_video_params_fps_and_steps(client, no_queue):
    r = client.post("/api/generate/video/t2v", data=_payload(prompt="v", fps=99, quality="Draft"))
    p = db.get_job(r.json()["job_id"]).params
    assert p["fps"] == 30 and p["steps"] == 25  # clamped; Draft tier for video

    r = client.post("/api/generate/video/t2v", data=_payload(prompt="v", quality="Custom", steps=33))
    assert db.get_job(r.json()["job_id"]).params["steps"] == 33


def _seed_asset(name: str, **meta) -> int:
    from backend.app.config import settings

    p = settings.images_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_png_bytes())
    a = db.add_asset("image", p, width=8, height=8, generator="local_image:txt2img",
                     meta={"prompt": "api test asset", **meta})
    assert a.id is not None
    return a.id


def test_assets_filters_and_actions(client):
    aid = _seed_asset("t_api_1.png", seed=11)
    assert client.post(f"/api/assets/{aid}/favorite", json={"favorite": True}).json()["ok"]
    assert client.post(f"/api/assets/{aid}/rating", json={"rating": 9}).json()["ok"]
    assert db.get_asset(aid).rating == 5  # clamped
    r = client.post(f"/api/assets/{aid}/tags", json={"tags": [" beach ", "", "x" ] + ["t"] * 30})
    assert len(r.json()["tags"]) <= 20 and "beach" in r.json()["tags"]

    items = client.get("/api/assets", params={"q": "api test asset", "favorite": True,
                                              "min_rating": 5, "kind": "image"}).json()["items"]
    assert any(i["id"] == aid for i in items)

    detail = client.get(f"/api/assets/{aid}").json()
    assert detail["url"].startswith("/files/images/")


def test_structured_human_grade_updates_the_quick_score_and_preserves_the_rubric(client):
    aid = _seed_asset("t_api_grade.png")
    r = client.post(f"/api/assets/{aid}/grade", json={
        "prompt_fidelity": 5,
        "visual_quality": 4,
        "notes": "hands need a retry",
    })
    body = r.json()
    assert r.status_code == 200 and body["ok"]
    # 5*.40 + 4*.60 = 4.4 -> the gallery's rounded quick score.
    assert body["rating"] == 4
    detail = client.get(f"/api/assets/{aid}").json()
    assert detail["grade"]["overall"] == pytest.approx(4.4, abs=1e-3)
    assert detail["grade"]["notes"] == "hands need a retry"

    invalid = client.post(f"/api/assets/{aid}/grade", json={
        "prompt_fidelity": 0, "visual_quality": 5,
    })
    assert invalid.status_code == 422


def test_assets_export_and_delete(client):
    aid = _seed_asset("t_api_2.png")
    r = client.post("/api/assets/export", json={"ids": [aid]})
    assert r.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert "t_api_2.png" in zf.namelist()

    assert client.post("/api/assets/bulk-delete", json={"ids": [aid]}).json()["deleted"] == 1
    assert client.get(f"/api/assets/{aid}").status_code == 404


def test_models_and_presets_payloads(client):
    from backend.app.generators.registry import registry

    assert client.get("/api/models").json() == registry()
    p = client.get("/api/presets").json()
    for key in ("negative", "prompt", "style_profiles", "aspects", "quality_tiers", "hints", "user"):
        assert key in p
    assert "styles" not in p  # legacy flat list removed


def test_retried_generation_request_returns_the_original_job(client, no_queue):
    request_id = "request_retry_1234567890"
    body = _payload(prompt="only once", request_id=request_id)
    first = client.post("/api/generate/image/local", data=body)
    second = client.post("/api/generate/image/local", data=body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    job = db.job_for_request(request_id)
    assert job is not None and job.id == first.json()["job_id"]


def test_system_payload_shape(client):
    s = client.get("/api/system").json()
    assert "local_gpu" in s and "remote_gpu" in s and "models" in s
    assert s["remote_gpu"]["connected"] is False  # REMOTE_GPU_BASE_URL blanked in tests


def test_backend_diagnosis_payload_is_explicit_about_readiness(client):
    report = client.get("/api/backends")
    assert report.status_code == 200
    body = report.json()
    assert {item["id"] for item in body["backends"]} == {"local", "remote_gpu"}
    assert body["hardware"]["profile"]
    assert isinstance(body["credentials"]["hf_token_set"], bool)
    for backend in body["backends"]:
        for model in backend["models"]:
            assert model["status"] in {"ready", "partial", "absent", "unknown"}


def test_settings_model_inventory_comes_from_the_complete_catalogue(client):
    from backend.app.generators import variants

    models = client.get("/api/settings").json()["models"]
    ids = {item["id"] for item in models}
    assert {variants.repo_of(v) for v in variants.available("local")} <= ids
    assert {variants.repo_of(v) for v in variants.available("colab")} <= ids
    assert {model["lane"] for model in models} <= {"local", "remote_gpu"}
    assert all({"label", "id", "lane", "kind"} <= set(item) for item in models)


def test_local_generation_refuses_a_completed_cpu_only_probe(client, monkeypatch):
    from backend.app.backends import local

    monkeypatch.setattr(local, "_DEVICE", ("CPU only", 0.0))
    response = client.post("/api/generate/image/local", data=_payload(prompt="hello"))
    assert response.status_code == 503
    assert "CUDA GPU" in response.json()["detail"]


def test_image_edit_endpoint(client, no_queue):
    files = {"image": ("a.png", _png_bytes(), "image/png"),
             "image_2": ("b.png", _png_bytes(color=(0, 255, 0)), "image/png")}
    r = client.post("/api/generate/image/edit", data=_payload(prompt="merge them"), files=files)
    assert r.status_code == 200
    p = db.get_job(r.json()["job_id"]).params
    assert len(p["image_paths"]) == 2


def test_i2v_flf2v_upload(client, no_queue):
    files = {"image": ("first.png", _png_bytes(), "image/png"),
             "last_frame": ("last.png", _png_bytes(color=(0, 0, 255)), "image/png")}
    r = client.post("/api/generate/video/i2v", data=_payload(prompt="pan"), files=files)
    p = db.get_job(r.json()["job_id"]).params
    assert p["image_path"] and p["last_image_path"]


def test_video_speed_mode_coupling(client, no_queue):
    r = client.post("/api/generate/video/t2v", data=_payload(prompt="v", speed_mode=True))
    p = db.get_job(r.json()["job_id"]).params
    assert p["steps"] <= 8 and p["guidance"] == 1.0 and p["negative_prompt"] == ""


def test_hunyuan_engine_relaxes_frame_rule(client, no_queue):
    r = client.post("/api/generate/video/t2v", data=_payload(prompt="v", engine="hunyuan",
                                                             num_frames=50))
    p = db.get_job(r.json()["job_id"]).params
    assert p["engine"] == "hunyuan" and p["num_frames"] == 50
    assert p["negative_prompt"] == ""  # Wan negative not applied to other engines


def test_local_quality_variant_steps(client, no_queue):
    r = client.post("/api/generate/image/local", data=_payload(prompt="q", model_variant="quality"))
    p = db.get_job(r.json()["job_id"]).params
    assert p["steps"] == 28 and p["guidance"] == 4.0  # local_hq Standard tier

    r = client.post("/api/generate/image/local", data=_payload(prompt="q"))
    assert db.get_job(r.json()["job_id"]).params["steps"] == 9  # turbo default


def test_required_image_uploads_are_400_not_broken_jobs(client, no_queue):
    # img2img / inpaint / i2v with a missing required file must fail at the
    # route, not enqueue a job that can only error later.
    assert client.post("/api/generate/image/img2img", data=_payload(prompt="p")).status_code == 400
    files = {"image": ("x.png", _png_bytes(), "image/png")}
    r = client.post("/api/generate/image/inpaint", data=_payload(prompt="p"), files=files)
    assert r.status_code == 400  # mask missing
    assert client.post("/api/generate/video/i2v", data=_payload(prompt="p")).status_code == 400
    assert client.post("/api/generate/image/edit", data=_payload(prompt="p")).status_code == 400


def test_malformed_payload_is_400(client, no_queue):
    r = client.post("/api/generate/image/local", data={"payload": "{not json"})
    assert r.status_code == 400
    r = client.post("/api/generate/image/local", data={"payload": '["a","list"]'})
    assert r.status_code == 400


def test_unknown_engine_normalized_before_frame_clamp(client, no_queue):
    # engine "foo" is coerced to wan — so it must get Wan's 4k+1 clamp too.
    r = client.post("/api/generate/video/t2v", data=_payload(prompt="v", engine="foo",
                                                             num_frames=50))
    p = db.get_job(r.json()["job_id"]).params
    assert p["engine"] == "wan" and p["num_frames"] == 49


def test_non_numeric_params_fall_back(client, no_queue):
    r = client.post("/api/generate/image/local", data=_payload(
        prompt="p", guidance="abc", strength="zzz", batch="x", post_scale=500))
    assert r.status_code == 200
    p = db.get_job(r.json()["job_id"]).params
    # The default local variant is Z-Image-Turbo, whose documented CFG is 0.
    assert p["guidance"] == 0.0 and p["strength"] == 0.6 and p["batch"] == 1
    assert p["post_scale"] in (2, 4)


def test_tool_kinds_registered_for_rerun(client):
    # Rerun of a tool job must work in a fresh process — handlers register at
    # import like images/videos, not lazily on first submit.
    from backend.app.routers.common import get_handler

    for kind in ("upscale", "face_restore", "detail", "interpolate", "extend_video"):
        assert get_handler(kind) is not None, kind


def test_user_preset_validation(client):
    assert client.post("/api/presets/user", json={"name": "  ", "type": "prompt"}).status_code == 400
    assert client.post("/api/presets/user", json={"name": "x", "type": "bogus"}).status_code == 400
    r = client.post("/api/presets/user", json={"name": "x", "type": "prompt",
                                               "payload": {"text": "hello"}})
    assert r.status_code == 200 and r.json()["name"] == "x"


def test_loras_are_sanitized_before_they_are_persisted(client, no_queue, tmp_path, monkeypatch):
    """Job params are replayed verbatim on rerun, so a traversal path accepted
    here would be replayed too. Validation belongs at the router, not the handler."""
    from backend.app.config import settings

    d = tmp_path / "loras"
    d.mkdir()
    (d / "real.safetensors").write_bytes(b"x")
    monkeypatch.setattr(type(settings), "loras_dir", property(lambda self: d))

    from backend.app.routers.images import _common_params

    p = _common_params({
        "prompt": "x",
        "loras": [{"path": "real.safetensors", "weight": 0.5},
                  {"path": "../../etc/passwd", "weight": 1.0},
                  {"path": "missing.safetensors"}],
    }, "image_local")
    assert p["loras"] == [{"path": "real.safetensors", "weight": 0.5}]


def test_colab_jobs_never_carry_loras(client, no_queue):
    """The remote lane manages its own adapter; a picker value arriving from a
    stale draft must not be persisted as if it applied."""
    from backend.app.routers.images import _common_params

    p = _common_params({"prompt": "x", "loras": [{"path": "whatever.safetensors"}]}, "image_colab")
    assert p["loras"] == []


# --- outpainting -----------------------------------------------------------
def test_outpaint_route_is_registered_and_sanitizes_its_own_params(client, no_queue, tmp_path):
    """direction and expand_pct are outpaint-specific and reach the geometry
    directly, so a junk value must be clamped at the router, not the handler."""
    import io as _io

    from PIL import Image as _Image

    buf = _io.BytesIO()
    _Image.new("RGB", (64, 64), "red").save(buf, format="PNG")
    buf.seek(0)

    r = client.post(
        "/api/generate/image/outpaint",
        data={"payload": json.dumps({"prompt": "extend it", "direction": "sideways",
                                     "expand_pct": 9999, "seed": 3})},
        files={"image": ("in.png", buf.getvalue(), "image/png")},
    )
    assert r.status_code == 200
    from backend.app import db

    job = db.get_job(r.json()["job_ids"][0] if "job_ids" in r.json() else r.json()["job_id"])
    assert job.params["direction"] == "all"      # unknown direction falls back
    assert job.params["expand_pct"] == 100       # clamped to the max


def test_outpaint_requires_a_source_image(client, no_queue):
    r = client.post("/api/generate/image/outpaint",
                    data={"payload": json.dumps({"prompt": "x"})})
    assert r.status_code >= 400


def test_every_local_image_kind_has_a_sizing_profile():
    """_common_params does a bare _PROFILE[kind] lookup, so a generator added
    without an entry 500s on its very first request."""
    from backend.app.generators.registry import registry
    from backend.app.routers.images import _PROFILE

    for spec in registry():
        if spec["output"] == "image":
            assert spec["kind"] in _PROFILE, f"{spec['kind']} has no _PROFILE entry"


# --- trash -----------------------------------------------------------------
def test_delete_moves_to_trash_and_restore_brings_it_back(client, no_queue, tmp_path):
    """Deletion used to unlink immediately — one misclick on a multi-select and
    the files were gone. It is a state change now, reversible until purged."""
    from backend.app import db

    f = tmp_path / "t_trash.png"
    Image.new("RGB", (8, 8)).save(f)
    a = db.add_asset("image", f, generator="local_image:txt2img")

    assert client.delete(f"/api/assets/{a.id}").json()["ok"] is True
    assert client.get(f"/api/assets/{a.id}").status_code == 404      # gone from the gallery
    assert f.exists(), "soft delete must not touch the file"

    trash = client.get("/api/assets/trash").json()
    assert any(i["id"] == a.id for i in trash["items"])

    assert client.post("/api/assets/restore", json={"ids": [a.id]}).json()["restored"] == 1
    assert client.get(f"/api/assets/{a.id}").status_code == 200


def test_trashed_assets_disappear_from_search_and_stats(client, no_queue, tmp_path):
    from backend.app import db

    f = tmp_path / "t_hidden.png"
    Image.new("RGB", (8, 8)).save(f)
    a = db.add_asset("image", f, generator="local_image:txt2img")

    before = client.get("/api/assets").json()["total"]
    stats_before = client.get("/api/stats").json()
    client.delete(f"/api/assets/{a.id}")
    assert client.get("/api/assets").json()["total"] == before - 1
    stats_after = client.get("/api/stats").json()
    assert stats_after["images"] == stats_before["images"] - 1
    assert stats_after["today"] == stats_before["today"] - 1


def test_remote_gpu_url_validation_rejects_secret_exfiltration_targets():
    from fastapi import HTTPException

    from backend.app.routers.settings import _validated_remote_gpu_url

    assert _validated_remote_gpu_url("https://example.trycloudflare.com/") == \
        "https://example.trycloudflare.com"
    for bad in (
        "http://example.com", "https://localhost:8000", "https://127.0.0.1",
        "https://169.254.169.254/latest", "https://user:pass@example.com",
        "https://example.com?redirect=bad",
    ):
        with pytest.raises(HTTPException):
            _validated_remote_gpu_url(bad)


def test_remote_gpu_host_change_requires_a_fresh_secret(client, monkeypatch):
    from backend.app.config import settings
    from backend.app.routers import settings as settings_router

    saved: dict = {}
    monkeypatch.setattr(
        type(settings), "load_overrides",
        lambda _self: {
            "remote_gpu_base_url": "https://old.example",
            "remote_gpu_shared_secret": "existing-strong-secret",
        },
    )
    monkeypatch.setattr(
        type(settings), "effective_remote_gpu_url",
        property(lambda _self: "https://old.example"),
    )
    monkeypatch.setattr(
        type(settings), "save_overrides", lambda _self, value: saved.update(value)
    )

    async def healthy():
        return {"ok": True}

    monkeypatch.setattr(settings_router, "remote_gpu_health", healthy)
    rejected = client.post(
        "/api/settings", json={"remote_gpu_base_url": "https://new.example"}
    )
    assert rejected.status_code == 400
    accepted = client.post(
        "/api/settings",
        json={
            "remote_gpu_base_url": "https://new.example",
            "remote_gpu_shared_secret": "fresh-strong-secret",
        },
    )
    assert accepted.status_code == 200
    assert saved["remote_gpu_shared_secret"] == "fresh-strong-secret"


def test_metadata_reader_endpoint_is_prefill_only(client):
    from PIL.PngImagePlugin import PngInfo

    from backend.app.metadata import NATIVE_CHUNK, native_metadata_text

    info = PngInfo()
    info.add_text(NATIVE_CHUNK, native_metadata_text(
        {"prompt": "recover me", "seed": 77, "steps": 9}, "image_local"))
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG", pnginfo=info)
    r = client.post("/api/metadata/read", files={"image": ("source.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200
    assert r.json()["params"]["prompt"] == "recover me"
    assert r.json()["params"]["seed"] == 77


def test_purge_is_the_only_thing_that_touches_the_filesystem(client, no_queue, tmp_path):
    from backend.app import db

    f = tmp_path / "t_purge.png"
    Image.new("RGB", (8, 8)).save(f)
    a = db.add_asset("image", f, generator="local_image:txt2img")

    client.delete(f"/api/assets/{a.id}")
    assert f.exists()
    assert client.post("/api/assets/trash/purge", json={"ids": [a.id]}).json()["purged"] == 1
    assert not f.exists()
    assert db.get_asset(a.id, include_deleted=True) is None


def test_purge_cannot_take_a_live_asset(client, no_queue, tmp_path):
    """It filters on deleted_at, so a wrong id is a no-op rather than data loss."""
    from backend.app import db

    f = tmp_path / "t_live.png"
    Image.new("RGB", (8, 8)).save(f)
    a = db.add_asset("image", f, generator="local_image:txt2img")

    assert client.post("/api/assets/trash/purge", json={"ids": [a.id]}).json()["purged"] == 0
    assert f.exists()
    assert db.get_asset(a.id) is not None


# --- saved searches --------------------------------------------------------
def test_a_gallery_search_can_be_saved_and_recalled(client, no_queue):
    """Reuses UserPreset rather than a new table — a saved search is a named
    payload that has to survive `clear_all()`, which is what UserPreset is."""
    payload = {"q": "lighthouse", "kind": "image", "min_rating": 4, "sort": "rating"}
    r = client.post("/api/presets/user",
                    json={"type": "search", "name": "Best lighthouses", "payload": payload})
    assert r.status_code == 200

    saved = client.get("/api/presets/user?type=search").json()
    mine = next(p for p in saved if p["name"] == "Best lighthouses")
    assert mine["payload"] == payload

    assert client.delete(f"/api/presets/user/{mine['id']}").json()["ok"] is True


def test_saved_searches_survive_clearing_the_gallery(client, no_queue, tmp_path):
    from backend.app import db

    client.post("/api/presets/user",
                json={"type": "search", "name": "Keeper", "payload": {"q": "x"}})
    f = tmp_path / "wipe.png"
    Image.new("RGB", (8, 8)).save(f)
    db.add_asset("image", f, generator="local_image:txt2img")

    db.clear_all()
    names = [p["name"] for p in client.get("/api/presets/user?type=search").json()]
    assert "Keeper" in names


def test_an_unknown_preset_type_is_still_rejected(client, no_queue):
    r = client.post("/api/presets/user", json={"type": "nonsense", "name": "n", "payload": {}})
    assert r.status_code == 400


# --- camera moves ----------------------------------------------------------
def test_camera_move_is_baked_into_the_stored_prompt(client, no_queue):
    """Applied at sanitisation time, not at send time: what is stored in the job
    is exactly what the model saw, so a rerun cannot drift and the gallery's
    "reuse prompt" carries the move with it."""
    from backend.app.routers.videos import _video_params

    p = _video_params({"prompt": "waves on rocks", "camera": "push_in", "seed": 1}, "t2v")
    assert p["camera"] == "push_in"
    assert p["prompt"].startswith("waves on rocks")
    assert "dolly push in" in p["prompt"]


def test_no_camera_move_leaves_the_prompt_alone(client, no_queue):
    from backend.app.routers.videos import _video_params

    for value in ("none", None, "not_a_move"):
        p = _video_params({"prompt": "waves on rocks", "camera": value, "seed": 1}, "t2v")
        assert p["prompt"] == "waves on rocks"
        assert p["camera"] == "none"


def test_camera_text_is_appended_cleanly():
    from backend.app.presets import apply_camera

    assert apply_camera("a lighthouse,", "orbit").count(",,") == 0
    assert apply_camera("", "orbit") == "camera orbits slowly around the subject"
    assert apply_camera("x", "none") == "x"


def test_every_camera_option_has_wording():
    """A move that resolved to empty text would be a control that does nothing."""
    from backend.app.presets import CAMERA_MOVES

    for key, text in CAMERA_MOVES.items():
        if key == "none":
            assert text == ""
        else:
            assert text and len(text) > 10, f"{key} has no usable wording"


# ---- media cache policy ---------------------------------------------------
def test_generated_media_is_cached_as_immutable(client):
    """Filenames carry a millisecond stamp and a counter, so a URL always means
    the same bytes. Without a policy the browser revalidates every thumbnail on
    every gallery scroll."""
    from backend.app.config import settings

    settings.thumbs_dir.mkdir(parents=True, exist_ok=True)
    f = settings.thumbs_dir / "cache_probe.jpg"
    f.write_bytes(b"\xff\xd8\xff\xe0jpeg-ish")
    try:
        r = client.get("/files/thumbs/cache_probe.jpg")
        assert r.status_code == 200
        cache = r.headers.get("cache-control", "")
        assert "immutable" in cache and "max-age=86400" in cache
    finally:
        f.unlink(missing_ok=True)


def test_a_missing_file_is_also_cached_briefly(client):
    """A thumbnail that does not exist will not exist a second later either."""
    r = client.get("/files/thumbs/definitely_not_here.jpg")
    assert r.status_code == 404
    assert "max-age=300" in r.headers.get("cache-control", "")


def test_api_responses_are_not_given_a_cache_policy(client):
    """Only /files is immutable. Job state must never be cached."""
    r = client.get("/api/jobs")
    assert "immutable" not in r.headers.get("cache-control", "")
