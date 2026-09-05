"""Outpaint geometry.

Pure functions, so this covers the part that actually decides output quality —
canvas size, where the source lands, and which pixels the model is allowed to
repaint — without torch or a GPU.
"""
from __future__ import annotations

from itertools import pairwise

import pytest
from PIL import Image

from backend.app.outpaint import DIRECTIONS, MAX_SIDE, build, plan


def test_canvas_always_snaps_to_a_multiple_of_16():
    """The VAE downsamples by 8 and patchifies by 2; unsnapped dims break or
    silently get rounded somewhere less visible."""
    for w, h in [(1024, 1024), (777, 513), (100, 100), (1023, 769)]:
        for d in DIRECTIONS:
            p = plan(w, h, d, 33)
            assert p.canvas_w % 16 == 0 and p.canvas_h % 16 == 0


def test_the_source_is_never_scaled_or_cropped():
    """Outpainting must preserve the original pixels exactly — it adds, never
    resamples. plan() carries the source size through untouched."""
    p = plan(800, 600, "all", 50)
    assert (p.src_w, p.src_h) == (800, 600)
    assert p.canvas_w >= 800 and p.canvas_h >= 600


def test_a_non_aligned_untouched_axis_never_floor_crops_the_source():
    """The old floor snap made 1350px become 1344px on a right-only extend."""
    p = plan(1080, 1350, "right", 25)
    assert p.canvas_w >= 1080 and p.canvas_h >= 1350
    assert p.canvas_w % 16 == 0 and p.canvas_h % 16 == 0


@pytest.mark.parametrize("direction,grows_x,grows_y", [
    ("right", True, False),
    ("left", True, False),
    ("up", False, True),
    ("down", False, True),
    ("wider", True, False),
    ("taller", False, True),
    ("all", True, True),
])
def test_each_direction_grows_only_the_axis_it_should(direction, grows_x, grows_y):
    p = plan(1024, 1024, direction, 25)
    assert (p.canvas_w > 1024) is grows_x
    assert (p.canvas_h > 1024) is grows_y


def test_one_sided_extension_keeps_the_source_against_that_edge():
    """Extending right must not silently centre the image — the new space has to
    land on the requested side."""
    right = plan(1024, 768, "right", 25)
    assert right.paste_x == 0                       # source stays flush left

    left = plan(1024, 768, "left", 25)
    assert left.paste_x > 0                          # new space is on the left
    assert left.paste_x == left.canvas_w - left.src_w

    up = plan(1024, 768, "up", 25)
    assert up.paste_y == up.canvas_h - up.src_h

    down = plan(1024, 768, "down", 25)
    assert down.paste_y == 0


def test_symmetric_extension_centres_the_source():
    p = plan(1000, 1000, "all", 20)
    assert abs(p.paste_x - (p.canvas_w - p.src_w) // 2) <= 8
    assert abs(p.paste_y - (p.canvas_h - p.src_h) // 2) <= 8


def test_percent_is_clamped_to_a_sane_band():
    assert plan(512, 512, "right", 0).canvas_w == plan(512, 512, "right", 5).canvas_w
    assert plan(512, 512, "right", 9999).canvas_w == plan(512, 512, "right", 100).canvas_w


def test_a_huge_source_cannot_explode_the_canvas():
    """'all' at 100% on a big input would be 4x the pixels; the cap keeps it
    inside something a 16 GB card can actually decode."""
    p = plan(1900, 1900, "all", 100)
    assert p.canvas_w <= MAX_SIDE and p.canvas_h <= MAX_SIDE


def test_unknown_direction_falls_back_to_all():
    assert plan(512, 512, "sideways", 25) == plan(512, 512, "all", 25)


# --- build() ---------------------------------------------------------------
def test_build_returns_canvas_and_mask_at_the_planned_size():
    src = Image.new("RGB", (256, 192), "red")
    p = plan(256, 192, "all", 25)
    canvas, mask = build(src, p)
    assert canvas.size == (p.canvas_w, p.canvas_h) == mask.size
    assert canvas.mode == "RGB" and mask.mode == "L"


def test_the_original_pixels_survive_into_the_canvas():
    src = Image.new("RGB", (128, 128), (12, 34, 56))
    p = plan(128, 128, "all", 25)
    canvas, _ = build(src, p)
    assert canvas.getpixel((p.paste_x + 64, p.paste_y + 64)) == (12, 34, 56)


def test_mask_protects_the_interior_and_opens_the_new_area():
    src = Image.new("RGB", (256, 256), "blue")
    p = plan(256, 256, "all", 25)
    _, mask = build(src, p)
    # dead centre of the source is protected
    assert mask.getpixel((p.paste_x + 128, p.paste_y + 128)) == 0
    # the far corner is new territory
    assert mask.getpixel((2, 2)) == 255


def test_the_mask_overlaps_the_source_so_the_seam_can_blend():
    """A mask that stopped exactly at the source edge leaves a visible seam. It
    must eat a few pixels of known content for the model to blend into."""
    src = Image.new("RGB", (512, 512), "green")
    p = plan(512, 512, "all", 25)
    _, mask = build(src, p, overlap=24)
    just_inside = mask.getpixel((p.paste_x + 4, p.paste_y + 4))
    assert just_inside == 255, "mask should extend into the source, not stop at its edge"


def test_a_tiny_source_still_produces_a_usable_mask():
    """overlap must not swallow the whole image on small inputs."""
    src = Image.new("RGB", (32, 32), "white")
    p = plan(32, 32, "all", 50)
    _, mask = build(src, p, overlap=24)
    assert mask.getpixel((p.paste_x + 16, p.paste_y + 16)) == 0  # centre still protected


def test_canvas_edges_are_extended_not_left_black():
    """New area is primed with smeared edge pixels; a flat black fill would give
    the model a hard boundary to latch onto."""
    src = Image.new("RGB", (128, 128), (200, 100, 50))
    p = plan(128, 128, "all", 25)
    canvas, _ = build(src, p)
    assert canvas.getpixel((2, p.paste_y + 64)) != (0, 0, 0)
    assert canvas.getpixel((p.canvas_w - 3, p.paste_y + 64)) != (0, 0, 0)


# --- composite -------------------------------------------------------------
def test_composite_restores_the_true_source_pixels():
    """The interior must come back byte-identical. An inpaint pipeline encodes
    and decodes the whole canvas, so without this the 'untouched' region returns
    slightly shifted and shows as a rectangular seam."""
    from backend.app.outpaint import composite

    src = Image.new("RGB", (256, 256), (10, 20, 30))
    p = plan(256, 256, "all", 25)
    generated = Image.new("RGB", (p.canvas_w, p.canvas_h), (200, 200, 200))
    out = composite(generated, src, p)
    assert out.getpixel((p.paste_x + 128, p.paste_y + 128)) == (10, 20, 30)


def test_composite_leaves_the_new_area_to_the_model():
    from backend.app.outpaint import composite

    src = Image.new("RGB", (256, 256), (10, 20, 30))
    p = plan(256, 256, "all", 25)
    generated = Image.new("RGB", (p.canvas_w, p.canvas_h), (200, 200, 200))
    assert composite(generated, src, p).getpixel((2, 2)) == (200, 200, 200)


def test_composite_ramps_across_the_seam_instead_of_stepping():
    """A hard paste would trade a brightness seam for an edge seam. Along a line
    crossing the boundary values must change gradually, not in one jump."""
    from backend.app.outpaint import composite

    src = Image.new("RGB", (256, 256), (0, 0, 0))
    p = plan(256, 256, "all", 25)
    generated = Image.new("RGB", (p.canvas_w, p.canvas_h), (255, 255, 255))
    out = composite(generated, src, p, overlap=24)

    y = p.paste_y + 128
    row = [out.getpixel((x, y))[0] for x in range(p.paste_x - 8, p.paste_x + 40)]
    biggest_step = max(abs(b - a) for a, b in pairwise(row))
    assert biggest_step < 200, f"hard edge across the seam (max step {biggest_step})"
    assert row[0] > row[-1], "should ramp from generated toward source"


def test_composite_is_safe_on_a_tiny_source():
    from backend.app.outpaint import composite

    src = Image.new("RGB", (16, 16), (5, 5, 5))
    p = plan(16, 16, "all", 50)
    out = composite(Image.new("RGB", (p.canvas_w, p.canvas_h), (250, 250, 250)), src, p, overlap=24)
    assert out.size == (p.canvas_w, p.canvas_h)


def test_the_default_denoise_stays_in_the_seamless_band():
    """Measured, not assumed. A sweep on real output put the seam at 3.2 levels
    per channel at 0.65 and -15.6 at 0.95: above ~0.85 the model stops respecting
    the edge-extended priming and invents its own exposure, which is exactly what
    makes an outpaint look like an outpaint."""
    from backend.app.outpaint import OUTPAINT_STRENGTH

    assert 0.5 <= OUTPAINT_STRENGTH <= 0.8


def test_outpaint_exposes_its_own_strength_control_not_img2img_s():
    """img2img defaults to 0.6 with a different meaning; sharing one control
    would silently push outpaint into the seam-producing range."""
    from backend.app.generators.registry import registry
    from backend.app.outpaint import OUTPAINT_STRENGTH

    spec = next(s for s in registry() if s["id"] == "outpaint")
    strength = next(c for c in spec["controls"] if c["name"] == "strength")
    assert strength["default"] == OUTPAINT_STRENGTH
    assert strength["hint_key"] == "strength_outpaint"


def test_resumed_outpaint_defaults_legacy_null_numbers(tmp_path, monkeypatch):
    """A pre-sanitization queued row must resume instead of casting None."""
    from backend.app import outpaint
    from backend.app.generators import base, local_image
    from backend.app.routers import images

    source = tmp_path / "source.png"
    Image.new("RGB", (64, 64), (40, 50, 60)).save(source)
    seen = {}

    def generate(**kwargs):
        seen.update(kwargs)
        return Image.new("RGB", (kwargs["width"], kwargs["height"]), (80, 90, 100))

    monkeypatch.setattr(local_image, "resolve_model", lambda variant: "acme/model")
    monkeypatch.setattr(local_image, "generate", generate)
    monkeypatch.setattr(local_image, "effective_step_count", lambda mode, steps, strength, model: steps)
    monkeypatch.setattr(base, "dims_for", lambda **kwargs: (96, 96))
    monkeypatch.setattr(outpaint, "refine_priming", lambda canvas, plan: canvas)
    monkeypatch.setattr(images, "apply_image_post", lambda image, *args, **kwargs: (image, []))
    monkeypatch.setattr(images, "image_meta", lambda *args, **kwargs: {})
    monkeypatch.setattr(images, "persist_image", lambda *args, **kwargs: 17)

    result = images._outpaint_handler(1, {
        "image_path": str(source),
        "prompt": "continue",
        "negative_prompt": "",
        "seed": 4,
        "batch": 1,
        "quality": "Standard",
        "direction": "all",
        "expand_pct": None,
        "steps": None,
        "guidance": None,
        "strength": None,
    }, lambda *args, **kwargs: None)

    assert result == {"asset_ids": [17]}
    assert seen["steps"] == 9
    assert seen["guidance"] == 1.0
    assert seen["strength"] == outpaint.OUTPAINT_STRENGTH


# ---- priming --------------------------------------------------------------
def test_refine_priming_is_a_no_op_when_nothing_grew():
    """A plan that adds no pixels has nothing to invent."""
    from backend.app.outpaint import Plan, refine_priming

    img = Image.new("RGB", (64, 64), (5, 5, 5))
    flat = Plan(canvas_w=64, canvas_h=64, paste_x=0, paste_y=0, src_w=64, src_h=64)
    assert refine_priming(img, flat) is img


def test_refine_priming_degrades_to_the_input_when_cv2_is_missing(monkeypatch):
    """Priming is a quality step. It must never be able to fail a generation."""
    import builtins

    from backend.app.outpaint import build, plan, refine_priming

    real_import = builtins.__import__

    def no_cv2(name, *a, **kw):
        if name == "cv2":
            raise ImportError("no cv2 here")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_cv2)
    src = Image.new("RGB", (64, 64), (9, 9, 9))
    p = plan(64, 64, "all", 25)
    canvas, _ = build(src, p)
    assert refine_priming(canvas, p) is canvas


def test_build_stays_free_of_the_imaging_stack():
    """The reason priming lives in its own function: plan/build/composite must be
    testable with no cv2 and no numpy, which is what lets CI run without CUDA."""
    import sys

    from backend.app.outpaint import build, composite, plan

    src = Image.new("RGB", (48, 32), (3, 4, 5))
    p = plan(48, 32, "all", 25)
    canvas, _mask = build(src, p)
    composite(canvas, src, p)
    assert "cv2" not in sys.modules
    assert "numpy" not in sys.modules
