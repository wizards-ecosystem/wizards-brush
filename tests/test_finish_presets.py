"""The Finishing control expands to the post_* flags the pipeline actually reads.

The interesting case is the absent key: jobs queued before this control existed
carry post_upscale/post_face and no `finish` at all, and are replayed verbatim on
rerun. Reading those as the "none" preset would quietly drop steps the user asked
for the first time round.
"""
from __future__ import annotations

import pytest

from backend.app.routers.common import resolve_finish


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        ("none", (False, False, False)),
        ("faces", (True, True, False)),
        ("upscale", (False, False, True)),
        ("faces + upscale", (True, True, True)),
    ],
)
def test_presets_expand_to_steps(preset, expected):
    name, *steps = resolve_finish({"finish": preset})
    assert name == preset
    assert tuple(steps) == expected


def test_preset_ignores_stale_toggles():
    """A draft that visited "custom" keeps its toggle values. Once a preset is
    chosen again, the preset decides — not the values left behind."""
    name, detail, face, upscale = resolve_finish(
        {"finish": "none", "post_detail": True, "post_face": True, "post_upscale": True}
    )
    assert (name, detail, face, upscale) == ("none", False, False, False)


def test_absent_finish_replays_legacy_flags():
    name, detail, face, upscale = resolve_finish({"post_upscale": True, "post_face": True})
    assert name == "custom"
    assert (detail, face, upscale) == (False, True, True)


def test_custom_honours_the_individual_toggles():
    assert resolve_finish({"finish": "custom", "post_detail": True}) == ("custom", True, False, False)


def test_unknown_value_falls_through_rather_than_dropping_steps():
    assert resolve_finish({"finish": "Faces & Upscale", "post_upscale": True})[3] is True


def test_case_and_spacing_are_normalised():
    assert resolve_finish({"finish": "  FACES  "}) == ("faces", True, True, False)
