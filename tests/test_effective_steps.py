"""Partial-denoise progress and metadata report what actually executes."""
from __future__ import annotations

from PIL import Image

from backend.app.generators.local_image import effective_step_count
from backend.app.routers.common import apply_image_post, image_progress_window


def test_flow_families_ceil_the_partial_schedule():
    assert effective_step_count("inpaint", 9, 0.65, "Tongyi-MAI/Z-Image") == 6
    assert effective_step_count("img2img", 9, 0.6, "lodestones/Chroma") == 6


def test_sdxl_floors_the_partial_schedule():
    assert effective_step_count("inpaint", 9, 0.65, "org/my-sdxl-model") == 5
    assert effective_step_count("img2img", 30, 0.35, "stable-diffusion-xl/base") == 10


def test_text_to_image_runs_the_requested_count():
    assert effective_step_count("txt2img", 9, 0.01, "org/model") == 9


def test_finishing_has_a_real_monotonic_progress_window(monkeypatch):
    import backend.app.generators.postprocess as pp

    monkeypatch.setattr(pp, "upscale_image", lambda img, scale: img)
    seen: list[float] = []
    start, denoise_end, end = image_progress_window({"post_upscale": True}, 1, 2)
    assert start < denoise_end < end
    apply_image_post(
        Image.new("RGB", (16, 16)), {"post_upscale": True},
        lambda f, *_a, **_kw: seen.append(f), start=denoise_end, end=end,
    )
    assert seen == sorted(seen)
    assert seen[0] == denoise_end and seen[-1] == end
