"""Header-only model identification: what a file is, without loading weights."""
from __future__ import annotations

import json
import struct
from pathlib import Path

from backend.app.modelprobe import (
    compatible,
    family_of_model,
    probe_file,
    probe_header,
    read_header,
)


def _header(keys: dict[str, dict]) -> dict:
    return {"__metadata__": {"format": "pt"}, **keys}


def _tensor(shape=(4, 4)):
    return {"dtype": "F16", "shape": list(shape), "data_offsets": [0, 32]}


def _write(path: Path, header: dict) -> Path:
    blob = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(blob)) + blob + b"\x00" * 32)
    return path


# ---- format handling ------------------------------------------------------
def test_reads_a_valid_header(tmp_path):
    f = _write(tmp_path / "m.safetensors", _header({"a.weight": _tensor()}))
    assert read_header(f) is not None


def test_rejects_a_non_safetensors_file(tmp_path):
    f = tmp_path / "not.safetensors"
    f.write_bytes(b"this is a png, actually")
    assert read_header(f) is None
    assert probe_file(f).family == "unknown"


def test_rejects_an_absurd_length_prefix(tmp_path):
    """A hostile or corrupt file must not hand us a huge allocation."""
    f = tmp_path / "huge.safetensors"
    f.write_bytes(struct.pack("<Q", 1 << 60) + b"{}")
    assert read_header(f) is None


def test_rejects_a_truncated_file(tmp_path):
    f = tmp_path / "cut.safetensors"
    blob = json.dumps(_header({"a": _tensor()})).encode()
    f.write_bytes(struct.pack("<Q", len(blob)) + blob[:5])
    assert read_header(f) is None


def test_a_missing_file_is_unknown_not_an_exception(tmp_path):
    assert probe_file(tmp_path / "nope.safetensors").family == "unknown"


# ---- architecture fingerprints -------------------------------------------
def test_sdxl_needs_label_emb_and_sd_must_not_have_it():
    """The pair that shows why forbidden patterns exist: both have input_blocks."""
    sdxl = probe_header(_header({
        "input_blocks.0.weight": _tensor(), "middle_block.0.weight": _tensor(),
        "label_emb.0.weight": _tensor(),
    }))
    sd = probe_header(_header({
        "input_blocks.0.weight": _tensor(), "middle_block.0.weight": _tensor(),
    }))
    assert sdxl.family == "sdxl"
    assert sd.family == "sd"


def test_flux_and_flux2_are_distinguished():
    flux = probe_header(_header({
        "double_blocks.0.img_attn.qkv.weight": _tensor(), "vector_in.weight": _tensor(),
    }))
    flux2 = probe_header(_header({
        "double_blocks.0.img_attn.qkv.weight": _tensor(),
        "stream_modulation.weight": _tensor(),
    }))
    assert flux.family == "flux"
    assert flux2.family == "flux2"


def test_qwen_is_recognised():
    p = probe_header(_header({
        "transformer_blocks.0.img_mlp.net.0.weight": _tensor(),
        "time_text_embed.timestep_embedder.weight": _tensor(),
    }))
    assert p.family == "qwen"
    assert p.display == "Qwen Image"


def test_an_unrecognised_model_reports_unknown_rather_than_guessing():
    p = probe_header(_header({"some.novel.architecture.weight": _tensor()}))
    assert p.family == "unknown"
    assert not p.known


def test_empty_header_is_an_error_not_a_crash():
    assert probe_header({"__metadata__": {}}).error


# ---- adapters -------------------------------------------------------------
def test_detects_a_lora_and_reads_its_rank():
    p = probe_header(_header({
        "lora_unet_double_blocks_0_img_attn.lora_down.weight": _tensor((16, 3072)),
        "lora_unet_double_blocks_0_img_attn.lora_up.weight": _tensor((3072, 16)),
        "double_blocks.0.img_attn.weight": _tensor(),
        "vector_in.weight": _tensor(),
    }))
    assert p.is_adapter
    assert p.rank == 16


def test_detects_the_wider_adapter_family():
    """LoHa, LoKr and DoRA are adapters too, with different key suffixes."""
    for suffix in (".hada_w1_a", ".lokr_w1", ".dora_scale"):
        p = probe_header(_header({f"block.0{suffix}": _tensor()}))
        assert p.is_adapter, suffix


def test_detects_oft_ia3_and_glora_as_adapters():
    cases = {
        "OFT": {"block.0.oft_diag": _tensor()},
        "IA3": {"block.0.on_input": _tensor()},
        "GLoRA": {
            "block.0.a1.weight": _tensor(), "block.0.a2.weight": _tensor(),
            "block.0.b1.weight": _tensor(), "block.0.b2.weight": _tensor(),
        },
    }
    for expected, tensors in cases.items():
        probe = probe_header(_header(tensors))
        assert probe.is_adapter, expected
        assert probe.adapter_type == expected


def test_a_full_model_is_not_an_adapter():
    p = probe_header(_header({"input_blocks.0.weight": _tensor(),
                              "middle_block.0.weight": _tensor()}))
    assert not p.is_adapter


def test_training_prefixes_are_stripped_before_matching():
    """Trainers wrap the same architecture in different prefixes."""
    p = probe_header(_header({
        "lora_unet_input_blocks.0.lora_down.weight": _tensor(),
        "lora_unet_middle_block.0.lora_up.weight": _tensor(),
    }))
    assert p.family == "sd"


# ---- compatibility --------------------------------------------------------
def test_matching_families_are_compatible():
    assert compatible("flux", "flux")
    assert compatible("zimage", "zimage")


def test_mismatched_families_are_not():
    assert not compatible("flux", "zimage")
    assert not compatible("sdxl", "sd")


def test_unknown_on_either_side_is_treated_as_compatible():
    """No evidence of a mismatch is not evidence of one. We do not hide or warn
    about a file just because we failed to recognise its format."""
    assert compatible("unknown", "zimage")
    assert compatible("flux", "unknown")
    assert compatible("unknown", "unknown")


def test_chroma_accepts_flux_adapters():
    assert compatible("flux", "chroma")
    assert not compatible("chroma", "flux")


def test_model_family_is_guessed_from_the_repo_id():
    assert family_of_model("Tongyi-MAI/Z-Image-Turbo") == "zimage"
    assert family_of_model("Qwen/Qwen-Image") == "qwen"
    assert family_of_model("black-forest-labs/FLUX.1-dev") == "flux"
    assert family_of_model("some/unheard-of-model") == "unknown"
