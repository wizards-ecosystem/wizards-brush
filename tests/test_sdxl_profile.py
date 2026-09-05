"""SDXL needs different numbers from Qwen-Image on the same device.

The Colab lane is one "device" serving whatever checkpoint is configured. Sizing
it from the hardware budget alone pushed an SDXL checkpoint to 1.60 MP at the
High tier — well past every bucket it was trained on — which is what makes SDXL
render the subject twice rather than render it larger.
"""
from __future__ import annotations

import pytest

from backend.app.generators.base import dims_for
from backend.app.presets import QUALITY_STEPS

SDXL_BUCKET_MP = 1.05
TOLERANCE = 0.18  # /16 snapping moves the area a little


def _mp(wh: tuple[int, int]) -> float:
    return wh[0] * wh[1] / 1_000_000


@pytest.mark.parametrize("aspect", ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2"])
def test_sdxl_holds_its_trained_area_across_every_aspect(aspect):
    """SDXL varies aspect at constant area. Every offered ratio must land near
    the ~1.05 MP bucket, not scale up with the ratio."""
    got = _mp(dims_for(aspect=aspect, tier="High", device="a100", family="sdxl"))
    assert abs(got - SDXL_BUCKET_MP) < TOLERANCE, f"{aspect} -> {got:.2f} MP"


def test_sdxl_high_tier_does_not_exceed_standard_area():
    """For SDXL the High tier buys steps, not pixels. If High ever grows past
    Standard again, the duplicated-subject bug is back."""
    std = _mp(dims_for(aspect="1:1", tier="Standard", device="a100", family="sdxl"))
    high = _mp(dims_for(aspect="1:1", tier="High", device="a100", family="sdxl"))
    assert high <= std + 0.01


def test_non_sdxl_keeps_the_wider_device_budget():
    """Architecture overrides must not leak into the generic device fallback."""
    qwen = _mp(dims_for(aspect="1:1", tier="High", device="a100", family="qwen"))
    sdxl = _mp(dims_for(aspect="1:1", tier="High", device="a100", family="sdxl"))
    assert qwen > sdxl
    # Qwen's published 2:3 bucket is slightly larger than the generic 1.60 MP
    # budget. An unknown family must still use that generic row, not Qwen's
    # specific documented sizing or SDXL's trained-area ceiling.
    generic = _mp(dims_for(aspect="1:1", tier="High", device="a100", family=""))
    assert generic == pytest.approx(1.6, abs=0.03)
    assert sdxl < generic < qwen


def test_sdxl_step_tier_exists_and_is_ordered():
    tiers = QUALITY_STEPS["a100_sdxl"]
    assert tiers["Standard"] == 30  # what SDXL checkpoints are published with
    assert tiers["Draft"] <= tiers["Standard"] <= tiers["High"]
    # Lower than the generic a100 row: SDXL flattens out well before 45.
    assert tiers["High"] < QUALITY_STEPS["a100"]["High"]


def test_explicit_dimensions_are_still_capped_by_family():
    """A custom width/height must not escape the family's ceiling."""
    w, h = dims_for(device="a100", width=4096, height=4096, family="sdxl")
    assert max(w, h) <= 1536


def test_sdxl_keeps_its_bucket_on_the_local_card_too():
    """The local row is probed from VRAM and is tuned for a quantized 6-12B DiT
    (0.92 MP at High on a 16 GB card). SDXL is a 2.6B UNet that fits several
    times over, so inheriting that budget would render it BELOW its trained
    bucket for no memory reason. The architecture row has to win here as well."""
    dit = _mp(dims_for(aspect="1:1", tier="High", device="local"))
    sdxl = _mp(dims_for(aspect="1:1", tier="High", device="local", family="sdxl"))
    assert sdxl > dit
    assert abs(sdxl - SDXL_BUCKET_MP) < TOLERANCE


def test_local_non_sdxl_still_follows_the_hardware_probe():
    """The override must not disturb the models the probe exists for."""
    probed = _mp(dims_for(aspect="1:1", tier="High", device="local"))
    for family in ("zimage", "chroma", "flux", ""):
        assert _mp(dims_for(aspect="1:1", tier="High", device="local",
                            family=family)) == probed
