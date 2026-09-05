"""Metadata embedded in the files we write: provenance and interop."""
from __future__ import annotations

from PIL import Image

from backend.app.metadata import (
    IPTC_NS,
    NATIVE_CHUNK,
    digital_source_type,
    native_metadata_text,
    parameters_text,
    parse_a1111,
    png_text_chunks,
    read_image_metadata,
    xmp_packet,
)


# ---- provenance -----------------------------------------------------------
def test_a_pure_generation_is_declared_as_trained_algorithmic_media():
    assert digital_source_type("image_local") == IPTC_NS + "trainedAlgorithmicMedia"


def test_an_edit_is_declared_as_a_composite():
    """An outpaint keeps the original photograph in the middle. Claiming the whole
    file is synthetic would be false — the composite value exists for this."""
    for kind in ("inpaint", "outpaint", "img2img", "image_edit", "upscale"):
        assert digital_source_type(kind).endswith("compositeWithTrainedAlgorithmicMedia"), kind


def test_production_generator_ids_for_edits_are_declared_as_composites():
    for generator in (
        "local_image:img2img", "local_image:inpaint", "local_image:outpaint", "colab_edit",
    ):
        assert digital_source_type(generator).endswith(
            "compositeWithTrainedAlgorithmicMedia"
        ), generator


def test_an_unknown_kind_defaults_to_fully_generated():
    assert digital_source_type("something_new").endswith("trainedAlgorithmicMedia")


def test_the_xmp_packet_is_well_formed_and_carries_the_field():
    import xml.etree.ElementTree as ET

    packet = xmp_packet("image_local")
    body = packet[packet.index("<x:xmpmeta"):packet.index("</x:xmpmeta>") + len("</x:xmpmeta>")]
    ET.fromstring(body)   # raises if malformed
    assert "DigitalSourceType" in packet
    assert packet.startswith("<?xpacket begin=")
    assert packet.rstrip().endswith("<?xpacket end=\"w\"?>")


# ---- A1111 interop --------------------------------------------------------
def test_the_parameters_block_has_the_positional_layout_readers_expect():
    """Format is load-bearing: prompt first, negative second with its prefix,
    settings on one comma-separated line. Deviating breaks the parsers."""
    text = parameters_text({
        "prompt": "a castle", "negative_prompt": "blurry", "steps": 8,
        "guidance": 1.0, "seed": 42, "width": 1024, "height": 768,
        "sampler": "default",
    })
    lines = text.split("\n")
    assert lines[0] == "a castle"
    assert lines[1] == "Negative prompt: blurry"
    assert "Steps: 8" in lines[2]
    assert "Seed: 42" in lines[2]
    assert "Size: 1024x768" in lines[2]
    assert "Generator: The Wizard's Brush" in lines[2]


def test_a_derivative_parameters_block_keeps_the_ancestor_recipe_size():
    text = parameters_text({
        "prompt": "a castle", "width": 4096, "height": 3072,
        "generation_width": 1024, "generation_height": 768,
        "post": [{"operation": "upscale", "scale": 4}],
    })
    assert "Size: 1024x768" in text
    assert "4096x3072" not in text


def test_an_absent_negative_prompt_omits_the_line():
    lines = parameters_text({"prompt": "a castle", "steps": 4}).split("\n")
    assert len(lines) == 2, "no empty negative line"
    assert "Negative prompt" not in lines[1]


def test_loras_are_folded_into_the_prompt_where_readers_look():
    text = parameters_text({
        "prompt": "a castle",
        "loras": [{"path": "styles/anime-v2.safetensors", "weight": 0.8}],
    })
    assert "<lora:anime-v2:0.8>" in text.split("\n")[0]


def test_empty_values_are_left_out_rather_than_written_as_blanks():
    text = parameters_text({"prompt": "x", "sampler": "", "strength": None})
    assert "Sampler:" not in text
    assert "Denoising strength:" not in text


# ---- embedding ------------------------------------------------------------
def test_both_chunks_are_produced():
    chunks = png_text_chunks({"prompt": "x", "seed": 1}, "image_local")
    assert set(chunks) == {"parameters", NATIVE_CHUNK, "XML:com.adobe.xmp"}


def test_reproducibility_and_provenance_can_be_disabled_independently():
    meta = {"prompt": "private prompt", "seed": 1}
    provenance_only = png_text_chunks(
        meta, "image_local", include_metadata=False, include_provenance=True)
    settings_only = png_text_chunks(
        meta, "image_local", include_metadata=True, include_provenance=False)
    assert set(provenance_only) == {"XML:com.adobe.xmp"}
    assert "private prompt" not in next(iter(provenance_only.values()))
    assert set(settings_only) == {"parameters", NATIVE_CHUNK}


def test_a_saved_png_carries_the_chunks(tmp_path):
    from backend.app.utils.io import save_image

    saved = save_image(Image.new("RGB", (32, 32), (1, 2, 3)), tag="meta_probe",
                       meta={"prompt": "a castle", "seed": 7, "steps": 8},
                       kind="image_local")
    try:
        with Image.open(saved.path) as reopened:
            assert "a castle" in reopened.text["parameters"]
            assert "Seed: 7" in reopened.text["parameters"]
            assert "trainedAlgorithmicMedia" in reopened.text["XML:com.adobe.xmp"]
            assert '"schema":"wizards-brush/image"' in reopened.text[NATIVE_CHUNK]
    finally:
        saved.path.unlink(missing_ok=True)


def test_saving_without_metadata_still_works():
    """Metadata is a bonus. Its absence must not change anything."""
    from backend.app.utils.io import save_image

    saved = save_image(Image.new("RGB", (16, 16)), tag="meta_none")
    try:
        assert saved.path.exists()
        with Image.open(saved.path) as reopened:
            assert "parameters" not in reopened.text
    finally:
        saved.path.unlink(missing_ok=True)


def test_a_hostile_meta_value_cannot_stop_a_save(caplog):
    """An odd value in a params dict must never be able to fail a generation."""
    from backend.app.utils.io import save_image

    class Explodes:
        def __str__(self):
            raise RuntimeError("boom")

    saved = save_image(Image.new("RGB", (16, 16)), tag="meta_bad",
                       meta={"prompt": Explodes()}, kind="image_local")
    try:
        assert saved.path.exists()
        assert saved.metadata_error == "RuntimeError"
        assert "metadata embedding failed" in caplog.text
    finally:
        saved.path.unlink(missing_ok=True)


def test_a_metadata_embedding_failure_is_visible_on_the_asset_row(
    client, no_queue, monkeypatch,
):
    from backend.app import db
    from backend.app.routers.common import persist_image
    from backend.app.utils import io as image_io

    monkeypatch.setattr(image_io, "_png_info", lambda meta, kind: (None, "TypeError"))
    job = db.create_job("image_local", {})
    asset_id = persist_image(
        Image.new("RGB", (16, 16)), job_id=job.id, generator="local_image:txt2img",
        meta={"prompt": "x", "seed": 1}, tag="metadata_failure",
    )
    meta = db.get_asset(asset_id).meta
    assert meta["metadata_embed_error"] == "TypeError"
    assert "could not be embedded" in meta["warnings"][0]


# ---- reading --------------------------------------------------------------
def test_native_metadata_round_trips_raw_prompt_and_typed_values():
    import io

    from PIL.PngImagePlugin import PngInfo

    meta = {
        "prompt": "composed prompt, oil painting", "raw_prompt": "a lighthouse",
        "style_ids": ["oil"], "steps": 28, "guidance": 4.0, "seed": 9,
        "model_variant": "quality", "model": "Tongyi-MAI/Z-Image",
    }
    info = PngInfo()
    info.add_text(NATIVE_CHUNK, native_metadata_text(meta, "local_image:txt2img"))
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, "PNG", pnginfo=info)

    result = read_image_metadata(buf.getvalue())
    assert result["scheme"] == "wizards-brush"
    assert result["params"]["prompt"] == "a lighthouse"
    assert result["params"]["style_ids"] == ["oil"]
    assert result["params"]["steps"] == 28
    assert result["params"]["model_variant"] == "quality"
    assert "model" not in result["params"], "repo ids are informative, never raw picker values"


def test_a1111_parser_handles_multiline_prompts_quoting_and_typed_fields(monkeypatch):
    import backend.app.metadata as metadata

    monkeypatch.setattr(
        metadata, "_lora_catalog_by_stem",
        lambda: {"film-look": "looks/film-look.safetensors"},
    )
    text = (
        "a portrait: at sunset, <lora:film-look:0.75>\nsecond prompt line\n"
        "Negative prompt: blur\nextra fingers\n"
        'Steps: 30, Sampler: Euler, CFG scale: 3.5, Seed: 42, Size: 1024x768, '
        'Model: "unknown, checkpoint: v2", Generator: Other UI'
    )
    params, unresolved = parse_a1111(text)
    assert params["prompt"] == "a portrait: at sunset\nsecond prompt line"
    assert params["negative_prompt"] == "blur\nextra fingers"
    assert params["quality"] == "Custom" and params["steps"] == 30
    assert params["sampler"] == "euler" and params["guidance"] == 3.5
    assert (params["width"], params["height"], params["aspect"]) == (1024, 768, "Custom")
    assert params["loras"] == [{"path": "looks/film-look.safetensors", "weight": 0.75}]
    assert unresolved == ["Model: unknown, checkpoint: v2"]


def test_a1111_three_pair_heuristic_preserves_prompt_line_with_a_colon():
    params, unresolved = parse_a1111("first line\nlighting: warm, composition: centered")
    assert params["prompt"] == "first line\nlighting: warm, composition: centered"
    assert unresolved == []


def test_parameter_writer_quotes_values_that_would_break_the_comma_grammar():
    text = parameters_text({
        "prompt": "x", "steps": 8, "seed": 1, "model": "org/model, revision: two",
    })
    assert 'Model: "org/model, revision: two"' in text
    _parsed, unresolved = parse_a1111(text)
    assert any("org/model, revision: two" in value for value in unresolved)
