from __future__ import annotations

from PIL import Image

from backend.app.inpaint import plan_inpaint


def _mask(size=(96, 64), box=(40, 24, 56, 40)) -> Image.Image:
    mask = Image.new("L", size, 0)
    mask.paste(255, box)
    return mask


def test_regional_plan_crops_with_context_and_alignment():
    source = Image.new("RGB", (96, 64), "navy")
    plan = plan_inpaint(source, _mask(), regional=True, padding=8, grow=0)

    assert plan.crop == (32, 16, 64, 48)
    assert plan.input_image.size == plan.input_mask.size == (32, 32)


def test_whole_image_plan_keeps_the_full_canvas():
    source = Image.new("RGB", (96, 64), "navy")
    plan = plan_inpaint(source, _mask(), regional=False)

    assert plan.crop == (0, 0, 96, 64)
    assert plan.input_image.size == source.size


def test_composite_never_changes_pixels_outside_original_mask():
    source = Image.new("RGB", (96, 64), (10, 20, 30))
    plan = plan_inpaint(source, _mask(), regional=True, padding=16, grow=8)
    generated = Image.new("RGB", (512, 512), (240, 10, 10))

    out = plan.composite(generated, blur=6)

    assert out.size == source.size
    assert out.getpixel((0, 0)) == (10, 20, 30)
    assert out.getpixel((39, 32)) == (10, 20, 30), "grown generation mask must not leak"
    assert out.getpixel((48, 32)) != (10, 20, 30), "painted center must be regenerated"


def test_mask_is_nearest_resized_and_empty_mask_is_actionable():
    import pytest

    source = Image.new("RGB", (32, 16), "black")
    tiny = Image.new("L", (2, 1), 0)
    tiny.putpixel((1, 0), 255)
    plan = plan_inpaint(source, tiny, regional=False, grow=0)
    assert plan.mask.getpixel((4, 8)) == 0
    assert plan.mask.getpixel((28, 8)) == 255

    with pytest.raises(ValueError, match="mask is empty"):
        plan_inpaint(source, Image.new("L", source.size, 0), regional=True)
