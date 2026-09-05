"""Torch-free contracts for tiled Real-ESRGAN output planning and blending."""
from __future__ import annotations

import pytest

from backend.app.generators.postprocess import (
    MAX_UPSCALE_SIDE,
    ToolUnavailable,
    _blend_mask,
    upscale_target_size,
)


def test_upscale_size_is_exact_below_the_ceiling():
    assert upscale_target_size(768, 1024, 4) == (3072, 4096)
    assert upscale_target_size(640, 960, 2) == (1280, 1920)


def test_upscale_size_clamps_proportionally_at_the_ceiling():
    assert upscale_target_size(1280, 1280, 4) == (MAX_UPSCALE_SIDE, MAX_UPSCALE_SIDE)
    assert upscale_target_size(1024, 1536, 4) == (2731, MAX_UPSCALE_SIDE)


def test_an_already_ceiling_sized_image_is_refused_instead_of_downscaled():
    with pytest.raises(ToolUnavailable, match="already"):
        upscale_target_size(MAX_UPSCALE_SIDE, 2048, 2)


def test_overlap_mask_fades_from_old_tile_to_new_tile():
    mask = _blend_mask((16, 12), left=8, top=0)
    assert mask.getpixel((0, 6)) == 0
    assert 100 < mask.getpixel((4, 6)) < 200
    assert mask.getpixel((7, 6)) == 255
    assert mask.getpixel((15, 6)) == 255


def test_corner_multiplies_horizontal_and_vertical_ramps():
    mask = _blend_mask((10, 10), left=5, top=5)
    assert mask.getpixel((0, 0)) == 0
    assert mask.getpixel((9, 0)) == 0
    assert mask.getpixel((0, 9)) == 0
    assert mask.getpixel((9, 9)) == 255
