"""The remote audit must preserve the per-model recipes it claims to test."""
from __future__ import annotations

from scripts.a100_sampling_matrix import _payload


def test_qwen_audit_uses_its_documented_final_quality_bucket():
    p = _payload("quality", "benchmark prompt", 2, 12345, speed=False)
    assert (p["width"], p["height"], p["aspect"]) == (1056, 1584, "Custom")
    assert (p["steps"], p["guidance"]) == (50, 4.0)


def test_flux2_audit_uses_the_model_card_square_recipe():
    p = _payload("alt", "benchmark prompt", 2, 12345, speed=False)
    assert (p["width"], p["height"], p["aspect"]) == (1024, 1024, "Custom")
    assert (p["steps"], p["guidance"]) == (50, 4.0)
