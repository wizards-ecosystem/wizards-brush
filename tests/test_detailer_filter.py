"""Detection size filtering, as a fraction of the image rather than in pixels."""
from __future__ import annotations

from PIL import Image

from backend.app.generators.detailer import (
    MAX_REGION_FRACTION,
    MIN_REGION_FRACTION,
    _filter_by_size,
)


def box(x0, y0, w, h):
    return (x0, y0, x0 + w, y0 + h)


def test_the_same_face_is_judged_differently_at_different_resolutions():
    """The whole point. A 64px face is background clutter in a 2048px render and
    the subject in a 256px one — an absolute threshold cannot express that."""
    face = box(0, 0, 64, 64)
    assert _filter_by_size([face], 256, 256) == [face], "the subject"
    assert _filter_by_size([face], 2048, 2048) == [], "clutter"


def test_a_region_below_the_floor_is_dropped():
    tiny = box(0, 0, 10, 10)
    assert _filter_by_size([tiny], 1024, 1024) == []


def test_a_region_covering_the_whole_image_is_dropped():
    """Detailing the entire frame is just a second generation pass."""
    whole = box(0, 0, 1000, 1000)
    assert _filter_by_size([whole], 1024, 1024) == []


def test_a_large_but_not_total_region_is_kept():
    big = box(0, 0, 700, 700)
    assert _filter_by_size([big], 1024, 1024) == [big]


def test_a_wide_region_is_kept_if_only_one_axis_is_oversized():
    """A full-width band is not the whole image; both axes must exceed the cap."""
    band = box(0, 400, 1000, 200)
    assert _filter_by_size([band], 1024, 1024) == [band]


def test_results_are_largest_first_so_truncation_keeps_what_matters():
    small, medium, large = box(0, 0, 100, 100), box(0, 0, 300, 300), box(0, 0, 600, 600)
    got = _filter_by_size([small, large, medium], 1024, 1024)
    assert got == [large, medium, small]


def test_the_thresholds_are_fractions_not_pixels():
    assert 0 < MIN_REGION_FRACTION < 1
    assert 0 < MAX_REGION_FRACTION <= 1


def test_an_empty_detection_list_is_handled():
    assert _filter_by_size([], 1024, 1024) == []


def test_a_zero_sized_image_does_not_divide_by_zero():
    assert _filter_by_size([box(0, 0, 10, 10)], 0, 0) == []


def test_refine_uses_the_selected_models_recipe(monkeypatch):
    from backend.app.generators import detailer, local_image

    monkeypatch.setattr(detailer, "detect_regions", lambda *_a, **_kw: [box(200, 200, 100, 100)])
    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return kwargs["image"]

    monkeypatch.setattr(local_image, "generate", fake_generate)
    _out, count = detailer.refine(
        Image.new("RGB", (768, 768)), prompt="portrait", negative="blurry",
        model="custom/sdxl", steps=30, guidance=3.5,
    )
    assert count == 1
    assert captured["steps"] == 30
    assert captured["guidance"] == 3.5
    assert captured["negative_prompt"] == "blurry"
    assert captured["model"] == "custom/sdxl"
