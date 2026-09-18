"""Output validation: deterministic, Pillow-only checks with structured results."""
from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from backend.app import validators
from backend.app.validators import ValidationSpec


def _cutout(tmp_path, name="cutout.png", size=(64, 64), box=(16, 16, 48, 48)):
    """A real transparent PNG: opaque square in the middle, clear border."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle(box, fill=(200, 40, 40, 255))
    path = tmp_path / name
    img.save(path)
    return path


def _opaque(tmp_path, name="opaque.png", size=(64, 64), mode="RGB"):
    path = tmp_path / name
    Image.new(mode, size, (10, 20, 30) if mode == "RGB" else (10, 20, 30, 255)).save(path)
    return path


def _by_name(results):
    return {r.validator: r for r in results}


def test_empty_spec_still_checks_the_file_is_readable(tmp_path):
    verdict, results = validators.run(_opaque(tmp_path))
    assert verdict == "passed"
    assert [r.validator for r in results] == ["file"]
    assert results[0].details["format"] == "PNG"


def test_missing_and_corrupt_files_fail_and_stop(tmp_path):
    verdict, results = validators.run(tmp_path / "gone.png", {"alpha": "required"})
    assert verdict == "failed" and [r.validator for r in results] == ["file"]
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not really")
    verdict, results = validators.run(broken, {"width": 10})
    assert verdict == "failed" and "not a readable image" in results[0].message


def test_format_and_dimensions(tmp_path):
    path = _opaque(tmp_path, size=(64, 32))
    verdict, results = validators.run(path, {"format": "PNG", "width": 64, "height": 32})
    assert verdict == "passed"
    verdict, results = validators.run(path, {"format": "JPEG", "width": 64, "height": 64})
    got = _by_name(results)
    assert verdict == "failed"
    assert got["format"].status == "fail" and got["format"].details["actual"] == "PNG"
    assert got["dimensions"].message == "expected 64×64, got 64×32"
    # Only one side constrained.
    assert validators.run(path, {"width": 64})[0] == "passed"


def test_alpha_required(tmp_path):
    assert validators.run(_cutout(tmp_path), {"alpha": "required"})[0] == "passed"
    verdict, results = validators.run(_opaque(tmp_path), {"alpha": "required"})
    assert verdict == "failed" and "has none" in results[-1].message
    # An alpha channel that is opaque everywhere is present but not genuine.
    verdict, results = validators.run(_opaque(tmp_path, "rgba.png", mode="RGBA"),
                                      {"alpha": "required"})
    assert verdict == "warned" and "every pixel is opaque" in results[-1].message


def test_a_checkerboard_picture_is_not_transparency(tmp_path):
    """A 'transparent-looking' background painted into RGB pixels is opaque."""
    img = Image.new("RGB", (32, 32), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for x in range(0, 32, 8):
        for y in range(0, 32, 8):
            if (x + y) // 8 % 2:
                draw.rectangle((x, y, x + 7, y + 7), fill=(204, 204, 204))
    path = tmp_path / "checker.png"
    img.save(path)
    verdict, _ = validators.run(path, {"alpha": "required", "corners_transparent": True})
    assert verdict == "failed"


def test_alpha_forbidden(tmp_path):
    assert validators.run(_opaque(tmp_path), {"alpha": "forbidden"})[0] == "passed"
    assert validators.run(_opaque(tmp_path, "a.png", mode="RGBA"), {"alpha": "forbidden"})[0] == \
        "passed"
    verdict, results = validators.run(_cutout(tmp_path), {"alpha": "forbidden"})
    assert verdict == "failed" and results[-1].details["min_alpha"] == 0


def test_corner_transparency(tmp_path):
    assert validators.run(_cutout(tmp_path), {"corners_transparent": True})[0] == "passed"
    corner = _cutout(tmp_path, "corner.png", box=(0, 0, 20, 20))
    verdict, results = validators.run(corner, {"corners_transparent": True})
    assert verdict == "failed" and "top_left" in results[-1].message
    verdict, _ = validators.run(_opaque(tmp_path), {"corners_transparent": True})
    assert verdict == "failed"


def test_transparent_coverage_bounds(tmp_path):
    path = _cutout(tmp_path)  # 33x33 opaque of 64x64 -> ~73% transparent
    verdict, results = validators.run(path, {"min_transparent_fraction": 0.5})
    assert verdict == "passed"
    assert 0.7 < results[-1].details["transparent_fraction"] < 0.76
    assert validators.run(path, {"min_transparent_fraction": 0.9})[0] == "failed"
    verdict, results = validators.run(path, {"max_transparent_fraction": 0.5})
    assert verdict == "failed" and "may have been removed" in results[-1].message


def test_safe_margins(tmp_path):
    path = _cutout(tmp_path)  # content box 16..48 inside 64
    verdict, results = validators.run(path, {"safe_margin": 12})
    assert verdict == "passed" and results[-1].details["margins"]["left"] == 16
    verdict, results = validators.run(path, {"safe_margin": 20})
    assert verdict == "failed" and results[-1].details["required"] == 20
    # Without alpha the edge of the content is a guess: say so, do not pass/fail.
    verdict, results = validators.run(_opaque(tmp_path), {"safe_margin": 4})
    assert verdict == "warned"
    empty = tmp_path / "empty.png"
    Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(empty)
    assert validators.run(empty, {"safe_margin": 1})[0] == "failed"


def test_results_are_structured_and_serialisable(tmp_path):
    verdict, results = validators.run(_cutout(tmp_path), {
        "format": "PNG", "width": 64, "height": 64, "alpha": "required",
        "corners_transparent": True, "min_transparent_fraction": 0.2, "safe_margin": 8,
    })
    assert verdict == "passed"
    assert [r.validator for r in results] == [
        "file", "format", "dimensions", "alpha", "corners", "coverage", "margins"]
    for r in results:
        assert set(r.as_dict()) == {"validator", "status", "message", "details"}


def test_spec_rejects_unknown_fields_and_bad_ranges():
    with pytest.raises(ValueError):
        ValidationSpec.model_validate({"alpha": "sometimes"})
    with pytest.raises(ValueError):
        ValidationSpec.model_validate({"similarity": 0.9})
    with pytest.raises(ValueError):
        ValidationSpec.model_validate({"min_transparent_fraction": 1.5})
    assert ValidationSpec().is_empty()


def test_new_checks_plug_in_without_touching_storage(tmp_path, monkeypatch):
    """The extension point: one registered function, recorded like the rest."""
    monkeypatch.setattr(validators, "_REGISTRY", dict(validators._REGISTRY))
    seen = {}

    def similarity(ctx):
        seen["sources"] = ctx.source_asset_ids
        return validators.Result("similarity", "warn", "not implemented here", {"score": None})

    validators.register("similarity", similarity)
    verdict, results = validators.run(_opaque(tmp_path), {}, source_asset_ids=(7,))
    assert verdict == "warned" and results[-1].validator == "similarity"
    assert seen["sources"] == (7,)


def test_file_always_runs_first_and_a_buggy_check_cannot_crash(tmp_path, monkeypatch):
    def boom(ctx):
        raise RuntimeError("bug")

    monkeypatch.setattr(validators, "_REGISTRY", {"boom": boom, **validators._REGISTRY})
    verdict, results = validators.run(_opaque(tmp_path))
    assert results[0].validator == "file"
    assert verdict == "failed" and "could not run" in _by_name(results)["boom"].message
