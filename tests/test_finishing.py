"""Finishing processors and their integration into inline post-processing.

The background-removal model itself never runs here — that would import numpy
and download a weight. Its prediction is replaced by a known matte, which is
enough to pin everything this project owns: real alpha, preserved size,
untouched pixels, provenance, and graceful degradation.
"""
from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from backend.app import finishing
from backend.app.generators import matting
from backend.app.params import TrackedParams
from backend.app.routers import common


def _photo(size=(80, 60)):
    img = Image.new("RGB", size, (220, 220, 210))
    ImageDraw.Draw(img).ellipse((20, 10, 60, 50), fill=(180, 30, 40))
    return img


def _matte_for(img):
    matte = Image.new("L", img.size, 0)
    ImageDraw.Draw(matte).ellipse((20, 10, 60, 50), fill=255)
    return matte


# ---- step validation --------------------------------------------------------------
def test_steps_are_validated_and_canonicalised():
    steps = finishing.sanitize_steps([
        {"processor": "background_removal"},
        {"processor": "resize", "width": "512", "height": 512, "mode": "contain"},
    ])
    assert steps == [
        {"processor": "background_removal"},
        {"processor": "resize", "width": 512, "height": 512, "mode": "contain",
         "background": "transparent"},
    ]
    assert finishing.sanitize_steps(None) == [] and finishing.sanitize_steps([]) == []


@pytest.mark.parametrize("raw, fragment", [
    ("resize", "must be a list"),
    ([{"processor": "sparkle"}], "unknown finishing processor"),
    (["resize"], "must be an object"),
    ([{"processor": "resize", "width": 0, "height": 5}], "between 1 and 8192"),
    ([{"processor": "resize", "width": 5, "height": 5, "mode": "tile"}], "contain, cover or stretch"),
    ([{"processor": "resize", "width": 5, "height": 5, "background": "red"}], "#rrggbb"),
    ([{"processor": "background_removal", "strength": 2}], "takes no options"),
    ([{"processor": "upscale", "scale": 3}], "2 or 4"),
    ([{"processor": "resize", "width": 1, "height": 1}] * (finishing.MAX_STEPS + 1), "at most"),
])
def test_bad_steps_are_errors_not_silently_dropped(raw, fragment):
    with pytest.raises(finishing.FinishingError) as caught:
        finishing.sanitize_steps(raw)
    assert fragment in str(caught.value)


def test_describe_lists_every_processor_without_heavy_imports():
    names = {p["name"] for p in finishing.describe()}
    assert {"background_removal", "resize", "upscale", "face_restore"} <= names


# ---- resize ---------------------------------------------------------------------------
def test_resize_contain_pads_with_real_transparency():
    out, records = finishing.run_steps(
        _photo((80, 40)), [{"processor": "resize", "width": 64, "height": 64,
                            "mode": "contain", "background": "transparent"}], lambda f, m: None)
    assert out.size == (64, 64) and out.mode == "RGBA"
    assert out.getpixel((0, 0))[3] == 0          # padding is transparent
    assert out.getpixel((32, 32))[3] == 255      # content is opaque
    assert records[0]["operation"] == "resize"
    assert records[0]["output_size"] == {"width": 64, "height": 64}
    assert records[0]["input_size"] == {"width": 80, "height": 40}


def test_resize_with_a_colour_or_cover_is_opaque_and_exact():
    colour, _ = finishing.run_steps(
        _photo((80, 40)), [{"processor": "resize", "width": 50, "height": 50,
                            "mode": "contain", "background": "#ffffff"}], lambda f, m: None)
    assert colour.mode == "RGB" and colour.getpixel((0, 0)) == (255, 255, 255)
    cover, _ = finishing.run_steps(
        _photo((80, 40)), [{"processor": "resize", "width": 30, "height": 30, "mode": "cover",
                            "background": "transparent"}], lambda f, m: None)
    assert cover.size == (30, 30) and cover.mode == "RGB"


def test_resize_keeps_existing_transparency():
    rgba = matting.apply_matte(_photo(), _matte_for(_photo()))
    out, _ = finishing.run_steps(rgba, [{"processor": "resize", "width": 40, "height": 30,
                                         "mode": "stretch", "background": "transparent"}],
                                 lambda f, m: None)
    assert out.mode == "RGBA" and out.getpixel((0, 0))[3] == 0


# ---- background removal ---------------------------------------------------------------
@pytest.fixture()
def fake_model(monkeypatch):
    monkeypatch.setattr(matting, "unavailable_reason", lambda: None)
    monkeypatch.setattr(matting, "predict_matte", _matte_for)


def test_background_removal_makes_real_alpha_at_the_same_size(fake_model):
    src = _photo()
    out, records = finishing.run_steps(src, [{"processor": "background_removal"}],
                                       lambda f, m: None)
    assert out.mode == "RGBA" and out.size == src.size
    # The pixels are the source's own; only the alpha is new. No checkerboard,
    # no matte colour baked in.
    assert out.convert("RGB").tobytes() == src.tobytes()
    assert out.getpixel((0, 0))[3] == 0 and out.getpixel((40, 30))[3] == 255
    record = records[0]
    assert record["operation"] == "background_removal"
    assert record["model"] == matting.MODEL_ID and record["model_sha256"] == matting.MODEL_SHA256
    assert record["license"] == "MIT"
    assert 0.5 < record["transparent_fraction"] < 0.9


def test_an_existing_alpha_is_never_restored_by_a_second_pass(fake_model):
    src = _photo()
    already = src.convert("RGBA")
    already.putalpha(Image.new("L", src.size, 100))
    out = matting.apply_matte(already, _matte_for(src))
    assert out.getpixel((40, 30))[3] == 100      # min of the two, never more opaque
    assert out.getpixel((0, 0))[3] == 0


def test_matte_size_must_match():
    with pytest.raises(ValueError, match="matte is"):
        matting.apply_matte(_photo((10, 10)), Image.new("L", (5, 5)))


def test_unavailable_runtime_is_reported_not_faked(monkeypatch):
    monkeypatch.setattr(matting.importlib.util, "find_spec",
                        lambda name: None if name == "onnxruntime" else object())
    assert "onnxruntime" in (matting.unavailable_reason() or "")
    warnings: list[str] = []
    src = _photo()
    out, records = finishing.run_steps(src, [{"processor": "background_removal"}],
                                       lambda f, m: None, warnings)
    assert out is src and records == []
    assert warnings and "Remove background was skipped" in warnings[0]


def test_a_failing_step_keeps_the_previous_image_and_warns(monkeypatch):
    def broken(img):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(matting, "unavailable_reason", lambda: None)
    monkeypatch.setattr(matting, "predict_matte", broken)
    warnings: list[str] = []
    steps = [{"processor": "background_removal"},
             {"processor": "resize", "width": 20, "height": 20, "mode": "stretch",
              "background": "transparent"}]
    out, records = finishing.run_steps(_photo(), steps, lambda f, m: None, warnings)
    assert [r["operation"] for r in records] == ["resize"]   # later steps still run
    assert out.size == (20, 20) and out.mode == "RGB"
    assert any("model exploded" in w for w in warnings)


# ---- integration with inline finishing ---------------------------------------------------
def test_absent_finish_steps_changes_nothing():
    img = _photo()
    params = TrackedParams({"finish": "none", "post_upscale": False})
    out, applied = common.apply_image_post(img, params, lambda *a, **k: None)
    assert out is img and applied == []


def test_finish_steps_run_inline_and_are_recorded(fake_model):
    params = TrackedParams({
        "finish": "none",
        "finish_steps": [{"processor": "background_removal"},
                         {"processor": "resize", "width": 40, "height": 40,
                          "mode": "contain", "background": "transparent"}],
    })
    progress: list[float] = []
    warnings: list[str] = []
    out, applied = common.apply_image_post(
        _photo(), params, lambda f, m="", **k: progress.append(f), start=0.5, end=1.0,
        warnings=warnings)
    assert out.size == (40, 40) and out.mode == "RGBA"
    assert [r["operation"] for r in applied] == ["background_removal", "resize"]
    assert "finish_steps" in params.read_keys and warnings == []
    assert progress and min(progress) >= 0.5 and max(progress) <= 1.0


def test_post_history_chains_sizes_through_a_resize(fake_model):
    src = _photo((80, 60))
    params = {"finish_steps": [{"processor": "resize", "width": 32, "height": 24,
                                "mode": "stretch", "background": "transparent"},
                               {"processor": "background_removal"}]}
    out, applied = common.apply_image_post(src, params, lambda *a, **k: None)
    meta = common.image_meta(params, out, prompt="p", seed=1, width=80, height=60, post=applied)
    history = meta["post"]
    assert history[0]["input_size"] == {"width": 80, "height": 60}
    assert history[0]["output_size"] == {"width": 32, "height": 24}
    assert history[1]["input_size"] == {"width": 32, "height": 24}
    assert meta["generation_width"] == 80 and meta["width"] == 32


def test_progress_window_reserves_room_for_finish_steps():
    _start, denoise_end, _end = common.image_progress_window(
        {"finish_steps": [{"processor": "resize"}]}, 0, 1)
    assert denoise_end < 1.0


def test_generate_routes_sanitize_finish_steps(client, no_queue):
    from backend.app import db

    def post(steps):
        return client.post("/api/generate/image/local",
                           data={"payload": __import__("json").dumps(
                               {"prompt": "p", "finish_steps": steps})})

    bad = post([{"processor": "sparkle"}])
    assert bad.status_code == 400 and "unknown finishing processor" in bad.text
    ok = post([{"processor": "resize", "width": 64, "height": 64}])
    assert ok.status_code == 200
    params = db.get_job(ok.json()["job_id"]).params
    assert params["finish_steps"] == [{"processor": "resize", "width": 64, "height": 64,
                                       "mode": "contain", "background": "transparent"}]
    plain = client.post("/api/generate/image/local",
                        data={"payload": '{"prompt": "no finishing"}'})
    assert "finish_steps" not in db.get_job(plain.json()["job_id"]).params


def test_transparent_thumbnails_show_the_cutout_not_the_hidden_pixels(tmp_path, monkeypatch):
    from backend.app.config import settings
    from backend.app.utils import io as io_utils

    settings.ensure_dirs()
    src = _photo()
    cut = matting.apply_matte(src, _matte_for(src))   # background pixels still underneath
    name = io_utils.make_thumb(cut, "thumb-alpha-probe.png")
    thumb = Image.open(settings.thumbs_dir / name).convert("RGB")
    pixel = thumb.getpixel((0, 0))
    assert isinstance(pixel, tuple)
    assert all(abs(channel - 128) < 8 for channel in pixel)
