"""Required inputs are validated with errors, not assertions.

`python -O` strips `assert`. A missing source image guarded only by an assert
becomes an AttributeError somewhere inside diffusers in exactly the build where a
clear message matters most — and even with asserts enabled, a bare AssertionError
is not something a user can act on.

The check lives in a torch-free function on purpose, which is what lets it be
tested here at all: `generate()` imports torch, and this suite asserts the heavy
stack never loads.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.app.generators.local_image import (
    _missing_message,
    _need,
    _require_inputs,
)

ROOT = Path(__file__).resolve().parents[1]


class _Img:
    """Stands in for a PIL image. Only its not-None-ness is under test."""


def test_txt2img_needs_nothing_attached():
    _require_inputs("txt2img", None, None)  # must not raise


def test_img2img_without_a_source_image_says_which_input_is_missing():
    with pytest.raises(ValueError, match="img2img needs image, but image was not"):
        _require_inputs("img2img", None, None)


def test_inpaint_reports_both_missing_inputs_at_once():
    """One request, one error. Reporting only the first would make a user fix,
    resubmit, and wait for a model load to be told about the second."""
    with pytest.raises(ValueError, match="image and mask were not attached"):
        _require_inputs("inpaint", None, None)


def test_inpaint_names_only_what_is_actually_missing():
    with pytest.raises(ValueError, match="mask was not attached"):
        _require_inputs("inpaint", _Img(), None)
    with pytest.raises(ValueError, match="image was not attached"):
        _require_inputs("inpaint", None, _Img())


def test_a_rerun_whose_upload_was_swept_is_named_as_the_likely_cause():
    """The realistic way this happens: sweep_orphan_files removes uploads after
    30 minutes, so a rerun of an older img2img job has a path and no file."""
    with pytest.raises(ValueError, match="rerun, the original upload may have been swept"):
        _require_inputs("img2img", None, None)


def test_both_checks_share_one_message():
    """`_need` re-checks at the point of use so the type checker can see the
    guarantee. If its wording drifted from `_require_inputs`, the same failure
    would read differently depending on which check caught it."""
    with pytest.raises(ValueError) as early:
        _require_inputs("img2img", None, None)
    with pytest.raises(ValueError) as late:
        _need(None, "image", "img2img")
    assert str(early.value) == str(late.value) == _missing_message("img2img", ["image"])


def test_need_passes_a_present_value_straight_through():
    img = _Img()
    assert _need(img, "image", "img2img") is img


def test_no_required_input_is_guarded_by_a_bare_assert():
    """The guard that stops this regressing. Type-narrowing asserts on invariants
    that genuinely hold ("a committed row has a primary key") are fine and stay;
    what must not come back is an assert standing between a user's request and a
    confusing failure deep in the stack."""
    tree = ast.parse((ROOT / "backend/app/generators/local_image.py").read_text("utf-8"))
    offenders = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Assert)
        and any(isinstance(n, ast.Name) and n.id in {"image", "mask"}
                for n in ast.walk(node.test))
    ]
    assert not offenders, f"user inputs guarded by assert at lines {offenders}"
