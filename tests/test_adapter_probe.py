"""Adapters must be identified by the naming their TRAINERS emit, not only by
the naming diffusers uses.

Every real Z-Image LoRA probed as "unknown", because the fingerprint matched
diffusers' `transformer_blocks.N.attn` while ai-toolkit and the sd-scripts
lineage emit `layers.N.attention`. An unknown family is not inert: `compatible()`
answers "no judgement" — i.e. True — so the picker offered every adapter for
every model, which is exactly the silent mismatch the probe exists to prevent.
"""
from __future__ import annotations

from typing import Any

from backend.app.modelprobe import compatible, probe_header

_ZIMAGE_AITOOLKIT: dict[str, Any] = {
    "diffusion_model.layers.0.adaLN_modulation.0.lora_A.weight": {},
    "diffusion_model.layers.0.attention.to_k.lora_A.weight": {},
    "diffusion_model.layers.0.attention.to_k.lora_B.weight": {},
}
# Flux as diffusers names it — what most trainers now emit.
_FLUX_DIFFUSERS: dict[str, Any] = {
    "transformer.single_transformer_blocks.0.attn.to_k.lora_A.weight": {},
    "transformer.transformer_blocks.0.attn.to_q.lora_A.weight": {},
}
# Flux in the original BFL/ComfyUI naming.
_FLUX: dict[str, Any] = {
    "double_blocks.0.img_attn.qkv.lora_A.weight": {},
    "vector_in.in_layer.lora_A.weight": {},
}


def test_zimage_adapter_in_trainer_naming_is_identified():
    got = probe_header(dict(_ZIMAGE_AITOOLKIT))
    assert got.family == "zimage"
    assert got.is_adapter


def test_identified_zimage_adapter_is_refused_for_other_families():
    """The point of identifying it: it must stop being offered for Flux."""
    assert compatible("zimage", "zimage")
    assert not compatible("zimage", "flux")
    assert not compatible("zimage", "sdxl")


def test_declared_metadata_is_only_a_fallback():
    """Metadata answers when the shapes match nothing we know — and only then."""
    header = {"__metadata__": {"ss_base_model_version": "zimage"},
              "some.unrecognised.naming.lora_A.weight": {}}
    assert probe_header(header).family == "zimage"


def test_tensor_names_beat_wrong_metadata():
    """The reason metadata cannot be trusted first: a real Flux LoRA in this
    library declares `ss_base_model_version: "sd_1.5"`, a default its trainer
    never updated. Weights cannot be wrong about their own shape."""
    header = {"__metadata__": {"ss_base_model_version": "sdxl"}, **_FLUX_DIFFUSERS}
    assert probe_header(header).family == "flux"


def test_flux_in_diffusers_naming_is_identified():
    """`load_lora_weights` consumes this format directly, so it is the one that
    matters most — and it was the one the fingerprint did not know."""
    got = probe_header(dict(_FLUX_DIFFUSERS))
    assert got.family == "flux"
    assert got.is_adapter
    assert not compatible("flux", "zimage")


def test_declared_metadata_is_read_from_each_supported_key():
    for key in ("ss_base_model_version", "modelspec.architecture", "base_model"):
        header = {"__metadata__": {key: "flux"}, "x.lora_A.weight": {}}
        assert probe_header(header).family == "flux", key


def test_unparseable_metadata_falls_through_rather_than_raising():
    """Metadata is written by third-party tools: it may be absent, a non-dict,
    or name something we do not know. None of that may break the listing."""
    for meta in (None, "not-a-dict", {"ss_base_model_version": ""},
                 {"ss_base_model_version": "some-model-we-never-heard-of"}):
        header = {"__metadata__": meta, **_FLUX}
        assert probe_header(header).family == "flux"  # falls back to fingerprint


def test_flux_adapter_still_identified_and_not_confused_with_zimage():
    """Guard against the new Z-Image row over-matching its neighbours."""
    got = probe_header(dict(_FLUX))
    assert got.family == "flux"
    assert not compatible("flux", "zimage")
