"""Refine facade helpers: region inference, not a persisted job kind."""
from __future__ import annotations

from backend.app.refine import infer_region, is_restyle, refine_defaults


def test_ponytail_is_hair_not_a_restyle():
    assert infer_region("a low nape ponytail") == "hair"
    assert not is_restyle("a low nape ponytail")


def test_shirt_is_upper_clothes():
    assert infer_region("terracotta knit sweater") == "upper-clothes"


def test_scene_restyle_has_no_region():
    assert infer_region("restyle as a painting of the scene") is None
    assert is_restyle("restyle as a painting of the scene")


def test_refine_defaults_lock_identity_and_photoreal():
    params = refine_defaults("closed-mouth smile")
    assert params["input_fidelity"] == "high"
    assert params["finish"] == "photoreal"
    assert params["quality"] == "Draft"
    assert params["speed_mode"] is True
