"""Exercise a generation handler end-to-end with the heavy generator mocked out,
so the shared item_seed_prompt / image_meta helpers and asset persistence are
covered without torch or a GPU."""
from __future__ import annotations

from PIL import Image

from backend.app import db
from backend.app.routers.common import derivative_meta, item_seed_prompts
from backend.app.routers.images import _local_handler


def test_item_seed_prompts_expands_both_fields_with_the_same_semantics():
    params = {
        "prompt": "a {red|blue} (cat:1.2)",
        "negative_prompt": "{blur|noise}, [artifact]",
        "prompt_syntax": "a1111",
        "seed": 10,
    }
    assert item_seed_prompts(params, 1) == (11, "a blue cat", "noise, artifact")


def test_legacy_item_prompt_defaults_to_literal_punctuation():
    assert item_seed_prompts({
        "prompt": "(literal:1.2)", "negative_prompt": "[also literal]", "seed": 2,
    }, 0) == (2, "(literal:1.2)", "[also literal]")


def test_derivative_meta_appends_history_and_drops_ancestor_diagnostics():
    first = derivative_meta(
        {
            "prompt": "x", "width": 512, "height": 768,
            "warnings": ["ancestor warning"], "ignored_params": ["old_control"],
        },
        operations="upscale", input_size=(512, 768), output_size=(1024, 1536),
        details={"scale": 2, "source_asset_id": 7},
    )
    second = derivative_meta(
        first, operations="face_restore", input_size=(1024, 1536),
        output_size=(1024, 1536), details={"source_asset_id": 8},
    )

    assert "warnings" not in second and "ignored_params" not in second
    assert (second["generation_width"], second["generation_height"]) == (512, 768)
    assert (second["width"], second["height"]) == (1024, 1536)
    assert [step["operation"] for step in second["post"]] == ["upscale", "face_restore"]
    assert second["post"][0]["output_size"] == {"width": 1024, "height": 1536}
    assert second["post"][1]["source_asset_id"] == 8


def test_local_handler_persists_batch_with_correct_meta(client, no_queue, monkeypatch):
    from backend.app.generators import local_image

    monkeypatch.setattr(local_image, "resolve_model", lambda v: "test-model")
    monkeypatch.setattr(local_image, "generate", lambda **kw: Image.new("RGB", (64, 48), "blue"))

    job = db.create_job("image_local", {})
    params = {
        "prompt": "a cat", "negative_prompt": "blurry", "seed": 100,
        "seed_mode": "increment", "batch": 2, "steps": 9, "guidance": 1.0,
        "quality": "Standard", "aspect": "1:1",
    }
    frames: list[float] = []
    handler = _local_handler("txt2img")
    result = handler(job.id, params, lambda frac, msg="", *, preview=None: frames.append(frac))

    assert len(result["asset_ids"]) == 2
    assert frames  # progress callback was wired through

    a0 = db.get_asset(result["asset_ids"][0])
    a1 = db.get_asset(result["asset_ids"][1])
    assert (a0.width, a0.height) == (64, 48)
    # image_meta shared keys
    assert a0.meta["prompt"] == "a cat"
    assert a0.meta["negative_prompt"] == "blurry"
    assert a0.meta["quality"] == "Standard"
    assert a0.meta["steps"] == 9
    # handler extras
    assert a0.meta["mode"] == "txt2img"
    assert a0.meta["model"] == "test-model"
    # item_seed_prompt: increment mode advances the seed per batch item
    assert a0.meta["seed"] == 100
    assert a1.meta["seed"] == 101


def test_local_handler_records_measured_peak_vram(client, no_queue, monkeypatch):
    from backend.app.generators import local_image

    monkeypatch.setattr(local_image, "resolve_model", lambda _v: "test-model")

    def generate(**kwargs):
        kwargs["metrics"].update(peak_vram_gb=7.25, peak_reserved_vram_gb=8.0)
        return Image.new("RGB", (32, 32))

    monkeypatch.setattr(local_image, "generate", generate)
    job = db.create_job("image_local", {})
    result = _local_handler("txt2img")(
        job.id, {"prompt": "x", "seed": 1, "batch": 1}, lambda *a, **k: None)
    meta = db.get_asset(result["asset_ids"][0]).meta
    assert meta["peak_vram_gb"] == 7.25
    assert meta["peak_reserved_vram_gb"] == 8.0


def test_local_handler_skips_item_on_skipitem(client, no_queue, monkeypatch):
    from backend.app.generators import local_image
    from backend.app.queue import SkipItem

    monkeypatch.setattr(local_image, "resolve_model", lambda v: "m")

    def gen(**kw):
        raise SkipItem()

    monkeypatch.setattr(local_image, "generate", gen)

    job = db.create_job("image_local", {})
    params = {"prompt": "x", "seed": 1, "batch": 3, "steps": 9, "guidance": 1.0}
    result = _local_handler("txt2img")(job.id, params, lambda *a, **k: None)
    assert result["asset_ids"] == []  # every item skipped, none persisted


# --- LoRA plumbing ---------------------------------------------------------
def test_local_handler_passes_loras_through_and_records_them(client, no_queue, monkeypatch):
    """Adapters must reach generate() AND land in meta — meta is what "reuse all"
    and rerun replay, so dropping it there silently loses the style."""
    from backend.app.generators import local_image

    seen = {}

    def gen(**kw):
        seen["loras"] = kw.get("loras")
        return Image.new("RGB", (32, 32), "green")

    monkeypatch.setattr(local_image, "resolve_model", lambda v: "m")
    monkeypatch.setattr(local_image, "generate", gen)

    job = db.create_job("image_local", {})
    sel = [{"path": "style.safetensors", "weight": 0.75}]
    params = {"prompt": "x", "seed": 1, "batch": 1, "steps": 9, "guidance": 1.0, "loras": sel}
    result = _local_handler("txt2img")(job.id, params, lambda *a, **k: None)

    assert seen["loras"] == sel
    assert db.get_asset(result["asset_ids"][0]).meta["loras"] == sel


def test_local_handler_sends_an_empty_list_when_none_selected(client, no_queue, monkeypatch):
    """generate() must always receive a list — None would mean 'leave whatever
    adapters are already active on the shared pipeline', which leaks style."""
    from backend.app.generators import local_image

    seen: dict = {}
    monkeypatch.setattr(local_image, "resolve_model", lambda v: "m")
    monkeypatch.setattr(local_image, "generate",
                        lambda **kw: (seen.update(loras=kw.get("loras")),
                                      Image.new("RGB", (32, 32)))[1])

    job = db.create_job("image_local", {})
    _local_handler("txt2img")(job.id, {"prompt": "x", "seed": 1, "batch": 1}, lambda *a, **k: None)


def test_local_handler_coerces_none_steps_and_guidance(client, no_queue, monkeypatch):
    """Persisted legacy jobs may carry None values; the handler must coerce them to safe defaults."""
    from backend.app.generators import local_image

    seen: dict = {}

    def gen(**kw):
        seen["steps"] = kw["steps"]
        seen["guidance"] = kw["guidance"]
        seen["loras"] = kw.get("loras")
        return Image.new("RGB", (32, 32), "green")

    monkeypatch.setattr(local_image, "resolve_model", lambda v: "m")
    monkeypatch.setattr(local_image, "generate", gen)

    job = db.create_job("image_local", {})
    params = {
        "prompt": "x",
        "seed": 1,
        "batch": 1,
        "steps": None,
        "guidance": None,
        "model_variant": "turbo",
        "loras": [],
        "sampler": "default",
        "quality": "Standard",
        "aspect": "1:1",
    }

    _local_handler("txt2img")(job.id, params, lambda *a, **k: None)

    assert seen["steps"] == 9
    assert seen["guidance"] == 1.0
    assert seen["loras"] == []


def test_controlnet_refuses_to_run_under_cpu_offload(monkeypatch):
    """Not a preference — with offload on, the offload hooks and the resident
    ControlNet disagree about device placement and CUDA raises an illegal memory
    access, which poisons the context for the whole process. Raising here is the
    only outcome the backend survives."""
    import pytest
    from PIL import Image as _Image

    from backend.app.generators import local_image

    monkeypatch.setattr(local_image.settings, "local_offload", True)
    with pytest.raises(RuntimeError, match="LOCAL_OFFLOAD=false"):
        local_image.generate_control(
            prompt="x", seed=1, width=512, height=512,
            control_image=_Image.new("RGB", (512, 512)),
        )


# --- post-processing metadata ----------------------------------------------
def test_meta_records_generation_size_not_the_upscaled_size(client, no_queue, monkeypatch):
    """Found by reading real job params: every upscaled asset stored its POST
    dimensions as width/height, so "reuse all" fed 2688x3584 back in as
    generation dims — asking the model for an image 16x the area of the one it
    actually made."""
    from backend.app.generators import local_image
    from backend.app.routers import common, images

    monkeypatch.setattr(local_image, "resolve_model", lambda v: "m")
    monkeypatch.setattr(local_image, "generate",
                        lambda **kw: Image.new("RGB", (512, 640), "blue"))
    # stand in for Real-ESRGAN x4
    monkeypatch.setattr(images, "apply_image_post",
                        lambda img, params, cb, **kw: (
                            img.resize((img.width * 4, img.height * 4)), ["upscaled_x4"]))

    job = db.create_job("image_local", {})
    result = _local_handler("txt2img")(
        job.id, {"prompt": "x", "seed": 1, "batch": 1, "post_upscale": True},
        lambda *a, **k: None)

    a = db.get_asset(result["asset_ids"][0])
    assert (a.width, a.height) == (2048, 2560), "the FILE should be the upscaled size"
    assert a.meta["width"] == 2048 and a.meta["height"] == 2560
    assert a.meta["generation_width"] == 512
    assert a.meta["generation_height"] == 640
    assert a.meta["post"] == [{
        "operation": "upscale",
        "scale": 4,
        "input_size": {"width": 512, "height": 640},
        "output_size": {"width": 2048, "height": 2560},
    }]
    assert common is not None


def test_meta_records_no_post_steps_when_none_ran(client, no_queue, monkeypatch):
    from backend.app.generators import local_image

    monkeypatch.setattr(local_image, "resolve_model", lambda v: "m")
    monkeypatch.setattr(local_image, "generate", lambda **kw: Image.new("RGB", (64, 64)))

    job = db.create_job("image_local", {})
    r = _local_handler("txt2img")(job.id, {"prompt": "x", "seed": 1, "batch": 1},
                                 lambda *a, **k: None)
    assert db.get_asset(r["asset_ids"][0]).meta["post"] == []


def test_a_failed_post_step_does_not_claim_credit(client, no_queue, monkeypatch):
    """`applied` lists steps that SUCCEEDED. A step that degraded to a warning
    must not appear, or the gallery would say an image was upscaled when it was
    silently left alone."""
    from backend.app.routers.common import apply_image_post

    def boom(*a, **k):
        raise RuntimeError("CUDA out of memory")

    import backend.app.generators.postprocess as pp

    monkeypatch.setattr(pp, "upscale_image", boom)
    img = Image.new("RGB", (32, 32))
    out, applied = apply_image_post(img, {"post_upscale": True, "post_scale": "4"},
                                    lambda *a, **k: None)
    assert applied == []
    assert out.size == (32, 32)
