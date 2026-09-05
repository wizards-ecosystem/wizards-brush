"""Two lanes, two model catalogues, one table.

The lanes deliberately share variant NAMES ("quality" means Z-Image base locally
and Qwen-Image on the A100), so every lookup has to be lane-scoped. A leak in
either direction sends a job to the wrong device's model.
"""
from __future__ import annotations

import pytest

from backend.app.generators import variants
from backend.app.presets import QUALITY_STEPS


def test_lanes_do_not_leak_into_each_other():
    local = {v.name for v in variants.available("local")}
    colab = {v.name for v in variants.available("colab")}
    assert local and colab
    # "quality" exists on both and must resolve differently.
    shared = local & colab
    for name in shared:
        assert variants.resolve(name, lane="local") != variants.resolve(name, lane="colab")


def test_every_variant_resolves_on_its_own_lane_only():
    for lane in variants.LANES:
        for v in variants.available(lane):
            assert variants.get(v.name, lane=lane).lane == lane
            assert variants.resolve(v.name, lane=lane) == variants.repo_of(v)


def test_unknown_variant_falls_back_within_its_lane():
    """Never across lanes: a colab job with a stale local variant name must run
    a colab model, not reach for the local card's."""
    colab_repos = {variants.repo_of(v) for v in variants.available("colab")}
    assert variants.resolve("no-such-model", lane="colab") in colab_repos
    assert variants.resolve("turbo", lane="colab") in colab_repos  # a LOCAL name
    local_repos = {variants.repo_of(v) for v in variants.available("local")}
    assert variants.resolve("alt", lane="local") in local_repos


def test_aliases_cannot_collide_with_live_names():
    canonical = {(v.lane, v.name) for v in variants.VARIANTS}
    aliases = [(v.lane, alias) for v in variants.VARIANTS for alias in v.aliases]
    assert not canonical.intersection(aliases)
    assert len(aliases) == len(set(aliases))


def test_variant_recipes_are_complete():
    for v in variants.VARIANTS:
        assert v.note and v.refine_steps > 0
        assert 0 < v.cfg_truncation <= 1


@pytest.mark.parametrize("lane", ["local", "colab"])
def test_each_lane_has_step_tiers_and_in_range_guidance(lane):
    for v in variants.available(lane):
        assert v.steps_group in QUALITY_STEPS, f"{lane}/{v.name}: no step tier"
        tiers = QUALITY_STEPS[v.steps_group]
        assert tiers["Draft"] <= tiers["Standard"] <= tiers["High"]
        assert 0.0 <= v.guidance <= 12.0


def test_colab_picker_tracks_guidance_per_model():
    """The two A100 models have different CFG regimes, so the slider has to move
    with the picker — the same bug the local lane already had."""
    from backend.app.generators.registry import registry

    spec = next(s for s in registry() if s["id"] == "image_colab")
    guidance = next(c for c in spec["controls"] if c["name"] == "guidance")
    picker = [c for c in spec["controls"] if c["name"] == "model_variant"]
    if not picker:
        pytest.skip("only one colab model configured; no picker to check")
    assert guidance["defaults_by"]["map"] == variants.guidance_map("colab")
    assert set(picker[0]["options"]) == set(variants.options("colab"))
    for cfg in guidance["defaults_by"]["map"].values():
        assert guidance["min"] <= cfg <= guidance["max"]


def test_remote_image_jobs_carry_a_model_key():
    """The A100 holds one 35-58 GB image model at a time, so an interleaved
    queue must be groupable — which needs a key, exactly as the local lane."""
    from backend.app.routers.common import model_key_for

    for name in variants.options("colab"):
        key = model_key_for("image_colab", {"model_variant": name})
        assert key == variants.resolve(name, lane="colab")
    # Kinds with no model choice still opt out rather than inventing a key.
    assert model_key_for("upscale", {}) is None
    assert model_key_for("image_edit", {}) is None


def test_affinity_uses_the_right_device_per_lane():
    """A remote job must never be compared against the local card's resident
    model — that would group the queue by the wrong device entirely."""
    from backend.app import queue as q

    assert q._resident_for_lane("local") is None or isinstance(
        q._resident_for_lane("local"), str)
    # No health snapshot in tests -> no resident remote model, and no crash.
    assert q._resident_for_lane("remote") is None
