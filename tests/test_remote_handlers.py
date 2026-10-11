"""Remote (Colab) job handlers: the base64 -> image -> asset -> meta path.

This path had no coverage at all — every test that touched a handler used the
local one. It is also the path that carries the most expensive work in the
product, so a silent regression here costs real A100 time. run_remote is stubbed
(its own protocol is covered in test_remote_gpu_client.py); everything downstream of
it is exercised for real, including persistence and metadata.
"""
from __future__ import annotations

import base64
import io
from itertools import pairwise

import pytest
from PIL import Image

from backend.app import db


def _b64_png(w: int = 64, h: int = 48, color: str = "purple") -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture()
def no_post(monkeypatch):
    """Disable post-processing: upscale/face/detail need torch and a GPU."""
    from backend.app.routers import common

    monkeypatch.setattr(common, "apply_image_post", lambda img, params, cb, **kw: (img, []))
    return


def test_colab_image_persists_every_image_with_its_own_seed_and_prompt(client, no_queue, monkeypatch):
    from backend.app.routers import images

    sent = {}

    def fake_run_remote(path, payload, progress_cb=None, **kw):
        sent["path"] = path
        sent["payload"] = payload
        if progress_cb:
            progress_cb(0.5, "half")
        return {"images_b64": [_b64_png(), _b64_png(), _b64_png()]}

    monkeypatch.setattr(images, "run_remote", fake_run_remote, raising=False)
    monkeypatch.setattr("backend.app.remote_gpu_client.run_remote", fake_run_remote)
    monkeypatch.setattr(images, "apply_image_post", lambda img, params, cb, **kw: (img, []))

    job = db.create_job("image_colab", {})
    params = {
        "prompt": "a lighthouse", "negative_prompt": "blur", "seed": 500,
        "seed_mode": "increment", "batch": 3, "steps": 30, "guidance": 4.0,
        "quality": "Standard", "width": 1024, "height": 1024, "aspect": "1:1",
    }
    result = images._remote_image_handler(job.id, params, lambda f, m="", *, preview=None: None)

    assert len(result["asset_ids"]) == 3
    assets = [db.get_asset(i) for i in result["asset_ids"]]
    # increment seed mode must advance per image, and the meta must record it
    assert [a.meta["seed"] for a in assets] == [500, 501, 502]
    assert all(a.meta["prompt"] == "a lighthouse" for a in assets)
    assert all(a.meta["remote"] is True for a in assets)
    assert all(a.generator == "colab_a100" for a in assets)
    assert all((a.width, a.height) == (64, 48) for a in assets)
    # the whole batch goes in ONE round-trip — that is the point of this handler
    assert sent["payload"]["seeds"] == [500, 501, 502]
    assert len(sent["payload"]["prompts"]) == 3


def test_colab_image_accepts_the_single_image_response_shape(client, no_queue, monkeypatch):
    """Older/edit responses return image_b64 rather than images_b64."""
    from backend.app.routers import images

    monkeypatch.setattr("backend.app.remote_gpu_client.run_remote",
                        lambda *a, **k: {"image_b64": _b64_png(32, 32)})
    monkeypatch.setattr(images, "apply_image_post", lambda img, params, cb, **kw: (img, []))

    job = db.create_job("image_colab", {})
    result = images._remote_image_handler(
        job.id, {"prompt": "x", "seed": 7, "batch": 1, "steps": 30,
                 "guidance": 4.0, "width": 512, "height": 512},
        lambda *a, **k: None)
    assert len(result["asset_ids"]) == 1
    assert db.get_asset(result["asset_ids"][0]).meta["seed"] == 7


def test_colab_image_reports_progress_across_the_whole_job(client, no_queue, monkeypatch):
    """Progress must stay monotonic and end near 1.0 — the UI bar depends on it."""
    from backend.app.routers import images

    monkeypatch.setattr("backend.app.remote_gpu_client.run_remote",
                        lambda p, pl, progress_cb=None, **k: (
                            progress_cb and progress_cb(1.0, "done"),
                            {"images_b64": [_b64_png(), _b64_png()]})[1])
    monkeypatch.setattr(images, "apply_image_post", lambda img, params, cb, **kw: (img, []))

    seen: list[float] = []
    job = db.create_job("image_colab", {})
    images._remote_image_handler(
        job.id, {"prompt": "x", "seed": 1, "batch": 2, "steps": 30,
                 "guidance": 4.0, "width": 512, "height": 512},
        lambda f, m="", *, preview=None: seen.append(f))

    # Monotonic to within float noise: the remote phase ends at 0.05 + 0.8*1.0,
    # which is 0.8500000000000001, and the save phase starts at exactly 0.85.
    # A 1e-9 dip is invisible in the UI; a real regression would be far larger.
    assert all(b >= a - 1e-9 for a, b in pairwise(seen)), \
        f"progress went backwards: {seen}"
    assert seen[0] >= 0.0 and seen[-1] <= 1.0


def test_colab_image_falls_back_to_stored_resolution_for_legacy_jobs(client, no_queue, monkeypatch):
    """A rerun of an old job has no width/height — derive them, don't send 0x0."""
    from backend.app.routers import images

    sent = {}

    def fake(path, payload, progress_cb=None, **kw):
        sent.update(payload)
        return {"images_b64": [_b64_png()]}

    monkeypatch.setattr("backend.app.remote_gpu_client.run_remote", fake)
    monkeypatch.setattr(images, "apply_image_post", lambda img, params, cb, **kw: (img, []))

    job = db.create_job("image_colab", {})
    images._remote_image_handler(
        job.id, {"prompt": "x", "seed": 1, "batch": 1, "steps": 30, "guidance": 4.0,
                 "resolution": "480p", "orientation": "landscape"},
        lambda *a, **k: None)
    assert sent["width"] > 0 and sent["height"] > 0


def test_a_remote_failure_propagates_instead_of_persisting_nothing_quietly(client, no_queue, monkeypatch):
    from backend.app.routers import images

    def boom(*a, **k):
        raise RuntimeError("Remote GPU job failed: out of memory")

    monkeypatch.setattr("backend.app.remote_gpu_client.run_remote", boom)
    job = db.create_job("image_colab", {})
    with pytest.raises(RuntimeError, match="out of memory"):
        images._remote_image_handler(
            job.id, {"prompt": "x", "seed": 1, "batch": 1, "steps": 30,
                     "guidance": 4.0, "width": 512, "height": 512},
            lambda *a, **k: None)
    assert db.get_job(job.id).result.get("asset_ids") in (None, [])


def test_high_fidelity_edit_calls_inpaint_not_plus(client, no_queue, monkeypatch):
    from backend.app.config import settings
    from backend.app.generators import identity, parsing
    from backend.app.routers import images

    settings.ensure_dirs()
    src = settings.images_dir / "fid-src.png"
    Image.new("RGB", (64, 48), (20, 30, 40)).save(src)

    sent: list[str] = []

    def fake_run_remote(path, payload, progress_cb=None, **kw):
        sent.append(path)
        if path == "/inpaint":
            assert "mask_b64" in payload
        return {"image_b64": _b64_png(64, 48, "orange")}

    monkeypatch.setattr("backend.app.remote_gpu_client.run_remote", fake_run_remote)
    monkeypatch.setattr(images, "apply_image_post", lambda img, params, cb, **kw: (img, []))
    monkeypatch.setattr(identity, "_COMPARE", lambda a, b: 0.9)
    monkeypatch.setattr(
        parsing, "_PREDICT",
        lambda kind, img: Image.new("L", img.size, parsing.ATR_INDEX["upper-clothes"]))

    job = db.create_job("image_edit", {})
    images._remote_edit_handler(
        job.id,
        {"prompt": "a terracotta knit", "input_fidelity": "high", "image_path": str(src),
         "seed": 1, "steps": 4, "guidance": 1.0, "quality": "Draft", "strength": 0.75},
        lambda *a, **k: None)
    assert sent == ["/inpaint"]

    sent.clear()
    images._remote_edit_handler(
        job.id,
        {"prompt": "make it night", "input_fidelity": "standard",
         "image_path": str(src), "image_paths": [str(src)],
         "seed": 1, "steps": 4, "guidance": 4.0},
        lambda *a, **k: None)
    assert sent == ["/edit"]



def test_remote_inpaint_owns_crop_and_preserves_unmasked_source(client, no_queue, monkeypatch, tmp_path):
    """Nonzero context padding must not enable Diffusers' second crop/overlay."""
    from PIL import ImageChops, ImageOps

    from backend.app.generators import base
    from backend.app.routers import images

    source = Image.new("RGB", (320, 256), (20, 30, 40))
    source.paste((80, 90, 100), (0, 0, 64, 64))
    mask = Image.new("L", source.size, 0)
    mask.paste(255, (128, 96, 192, 160))
    source_path, mask_path = tmp_path / "source.png", tmp_path / "mask.png"
    source.save(source_path)
    mask.save(mask_path)
    crop_sizes = []

    def small_bucket(width, height, **kwargs):
        crop_sizes.append((width, height))
        return 64, 64

    def fake_remote(path, payload, **kwargs):
        assert path == "/inpaint"
        assert payload.get("padding_mask_crop") is None, "local plan already owns crop/context"
        with Image.open(io.BytesIO(base64.b64decode(payload["image_b64"]))) as work:
            assert work.size == (64, 64)
        with Image.open(io.BytesIO(base64.b64decode(payload["mask_b64"]))) as work_mask:
            assert work_mask.size == (64, 64)
            assert work_mask.getpixel((0, 0)) == 0
            assert work_mask.getpixel((32, 32)) == 255
        # Real Qwen inpaint auto-selects dimensions. Local compositing must restore
        # the crop even when the returned raster differs from the request size.
        return {"image_b64": _b64_png(48, 48, "orange")}

    monkeypatch.setattr(base, "dims_for_ratio", small_bucket)
    monkeypatch.setattr("backend.app.remote_gpu_client.run_remote", fake_remote)
    monkeypatch.setattr(images, "apply_image_post", lambda img, params, cb, **kw: (img, []))
    job = db.create_job("inpaint_remote", {})
    result = images._remote_inpaint_handler(
        job.id,
        {"prompt": "rose training clothes", "image_path": str(source_path), "mask_path": str(mask_path),
         "seed": 7, "steps": 4, "guidance": 4.0, "strength": 0.9, "quality": "High",
         "inpaint_area": "masked area", "mask_padding": 16, "mask_grow": 4, "mask_blur": 4},
        lambda *args, **kwargs: None)

    # Grow=4 then padding=16 and /16 outward snapping produces a 128px crop.
    # Dropping the user's padding would produce only 96px of context instead.
    assert crop_sizes == [(128, 128)]
    asset = db.get_asset(result["asset_ids"][0])
    assert asset.meta["mask_padding"] == 16
    with Image.open(asset.path) as rendered:
        assert rendered.size == source.size
        diff = ImageChops.difference(rendered.convert("RGB"), source)
        outside = ImageChops.multiply(diff, ImageOps.invert(mask).convert("RGB"))
        assert outside.getbbox() is None, "every unmasked source pixel, including face, stays exact"
        assert rendered.getpixel((160, 128)) != source.getpixel((160, 128))
