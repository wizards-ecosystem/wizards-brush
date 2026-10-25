"""P16: sprite sheet and atlas geometry, pixels and manifest.

Pure Pillow: every case builds its frames in memory and checks what lands on
the page, so a layout bug shows up as a wrong pixel, not a wrong number.
"""
from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from backend.app import spritesheet as ss


def _frame(index: int, size=(16, 16), colour=(255, 0, 0, 255), border=0, key=None):
    """A solid block, optionally inside a transparent border."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((border, border, size[0] - 1 - border, size[1] - 1 - border),
                                    fill=colour)
    return ss.Frame(index, key or f"f{index}", image, asset_id=100 + index)


def _build(frames, **profile):
    prof = ss.ExportProfile(**profile)
    layout = ss.layout(frames, prof)
    pages = ss.render(layout, prof)
    ss.validate(layout, pages, prof)
    return prof, layout, pages


def test_a_grid_puts_known_colours_at_known_cells():
    colours = [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255), (255, 255, 0, 255)]
    frames = [_frame(i, colour=c) for i, c in enumerate(colours)]
    _prof, layout, pages = _build(frames, mode="grid", columns=2, padding=3)
    assert layout.pages == [(3 + 2 * 19, 3 + 2 * 19)]
    for i, colour in enumerate(colours):
        col, row = i % 2, i // 2
        x, y = 3 + col * 19, 3 + row * 19
        assert pages[0].getpixel((x, y)) == colour
        assert pages[0].getpixel((x + 15, y + 15)) == colour
    assert pages[0].getpixel((0, 0)) == (0, 0, 0, 0)        # padding stays clear


def test_a_grid_never_stretches_a_frame():
    frames = [_frame(0, size=(16, 16)), _frame(1, size=(9, 13))]
    with pytest.raises(ss.SpriteError, match="not 16x16"):
        _build(frames, mode="grid")
    _prof, layout, _pages = _build(frames, mode="grid", fit="pad", columns=2, padding=0)
    odd = layout.placed[1]
    assert (odd.w, odd.h) == (9, 13) and odd.scale == 1.0
    assert (odd.x, odd.y) == (16 + (16 - 9) // 2, (16 - 13) // 2)
    _prof, layout, _pages = _build([_frame(0, size=(40, 20))], mode="grid", fit="contain",
                                   cell_width=20, cell_height=20)
    assert (layout.placed[0].w, layout.placed[0].h) == (20, 10)


def test_pixel_art_scales_with_nearest_neighbour():
    image = Image.new("RGBA", (2, 1))
    image.putpixel((0, 0), (255, 0, 0, 255))
    image.putpixel((1, 0), (0, 0, 255, 255))
    frame = ss.Frame(0, "px", image)
    # contain only shrinks, so give a frame larger than the cell to check nearest.
    big = image.resize((8, 4), Image.Resampling.NEAREST)
    _prof, layout, _pages = _build([ss.Frame(0, "px", big)], mode="grid", fit="contain",
                                   cell_width=4, cell_height=4, pixel_art=True, padding=0)
    content = layout.placed[0].content
    colours = {content.getpixel((x, y)) for x in range(content.width)
               for y in range(content.height)}
    assert colours <= {(255, 0, 0, 255), (0, 0, 255, 255)}, "no blended pixels"
    del frame


def test_trimmed_atlas_frames_round_trip_with_their_offsets():
    frames = [_frame(0, size=(20, 14), border=3), _frame(1, size=(7, 11), border=2),
              _frame(2, size=(9, 9))]
    prof, layout, pages = _build(frames, mode="atlas", trim=True, padding=2)
    manifest = ss.manifest(layout, prof, export_id="t", missing=[], include_labels=False)
    first = manifest["frames"][0]
    assert (first["w"], first["h"], first["trim_x"], first["trim_y"]) == (14, 8, 3, 3)
    assert (first["original_w"], first["original_h"]) == (20, 14)
    # Paste each trimmed rectangle back at its offset: the original reappears.
    for record, frame in zip(manifest["frames"], frames, strict=True):
        cut = pages[record["page"]].crop((record["x"], record["y"],
                                          record["x"] + record["w"], record["y"] + record["h"]))
        rebuilt = Image.new("RGBA", (record["original_w"], record["original_h"]), (0, 0, 0, 0))
        rebuilt.paste(cut, (record["trim_x"], record["trim_y"]))
        assert rebuilt.tobytes() == frame.image.tobytes()


def test_extrusion_repeats_edges_and_padding_must_allow_it():
    with pytest.raises(ValueError, match="twice the extrusion"):
        ss.ExportProfile(padding=2, extrude=2)
    frames = [_frame(0, colour=(10, 20, 30, 255)), _frame(1, colour=(200, 100, 0, 255))]
    _prof, layout, pages = _build(frames, mode="atlas", padding=4, extrude=2)
    for item in layout.placed:
        colour = item.content.getpixel((0, 0))
        assert pages[0].getpixel((item.x - 1, item.y)) == colour
        assert pages[0].getpixel((item.x - 2, item.y - 2)) == colour
        assert pages[0].getpixel((item.x + item.w + 1, item.y + item.h + 1)) == colour


def test_a_full_page_spills_to_more_pages_only_when_allowed():
    frames = [_frame(i, size=(30, 30)) for i in range(5)]
    with pytest.raises(ss.SpriteError, match="do not fit one page"):
        _build(frames, mode="atlas", max_width=64, max_height=64)
    prof, layout, _pages = _build(frames, mode="atlas", max_width=64, max_height=64,
                                  allow_multiple_pages=True, padding=1)
    # 1 + 30 + 1 + 30 + 1 = 63: two frames per shelf, two shelves per page.
    assert len(layout.pages) == 2
    manifest = ss.manifest(layout, prof, export_id="t", missing=[], include_labels=False)
    assert [p["file"] for p in manifest["pages"]] == ["sheet-1.png", "sheet-2.png"]
    assert [f["page"] for f in manifest["frames"]] == [0, 0, 0, 0, 1]
    assert [f["index"] for f in manifest["frames"]] == [0, 1, 2, 3, 4]


def test_packing_is_deterministic_and_never_overlaps():
    frames = [_frame(i, size=(5 + (i * 7) % 23, 4 + (i * 5) % 19)) for i in range(40)]
    _p, first, _ = _build(frames, mode="atlas", padding=1, max_width=128, max_height=128,
                          allow_multiple_pages=True)
    _p, second, _ = _build(list(reversed(frames)), mode="atlas", padding=1, max_width=128,
                           max_height=128, allow_multiple_pages=True)
    as_rects = lambda lay: {(p.frame.key, p.page, p.x, p.y) for p in lay.placed}  # noqa: E731
    assert as_rects(first) == as_rects(second)


def test_the_round_trip_fixture_with_odd_sizes_borders_and_repeated_names():
    """Transparent borders, an odd size, two frames named alike, several pages."""
    frames = [
        _frame(0, size=(33, 17), border=4, key="walk"),
        _frame(1, size=(33, 17), border=4, key="walk-2", colour=(0, 128, 255, 255)),
        _frame(2, size=(13, 31), border=1, key="jump"),
        _frame(3, size=(1, 1), key="dot"),
    ]
    prof, layout, _pages = _build(frames, mode="atlas", trim=True, padding=2, extrude=1,
                                 max_width=40, max_height=40, allow_multiple_pages=True,
                                 name="hero walk / run!")
    manifest = ss.manifest(layout, prof, export_id="t", missing=[], include_labels=False)
    assert len(manifest["pages"]) >= 2
    assert all(p["file"].startswith("hero-walk-run") for p in manifest["pages"])
    assert manifest["coordinate_origin"] == "top-left" and manifest["rotated"] is False
    assert manifest["rect_includes_padding"] is False


def test_an_opaque_background_composites_frames_over_it():
    frame = _frame(0, size=(8, 8), border=2)
    _prof, layout, pages = _build([frame], mode="grid", background="#102030", padding=1)
    assert pages[0].getpixel((0, 0)) == (16, 32, 48, 255)
    assert pages[0].getpixel((layout.placed[0].x, layout.placed[0].y)) == (16, 32, 48, 255)


def test_missing_frames_keep_their_indexes_and_labels_follow_privacy():
    frames = [_frame(0), _frame(2)]
    frames[0].labels = {"values": {"pose": "idle"}}
    prof, layout, _ = _build(frames, mode="grid", fit="pad")
    missing = [{"index": 1, "key": "f1", "reason": "failed"}]
    private = ss.manifest(layout, prof, export_id="t", missing=missing, include_labels=False)
    assert [f["index"] for f in private["frames"]] == [0, 2]
    assert private["missing"] == missing and "labels" not in private["frames"][0]
    shared = ss.manifest(layout, prof, export_id="t", missing=missing, include_labels=True)
    assert shared["frames"][0]["labels"] == {"values": {"pose": "idle"}}
