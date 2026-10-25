"""Sprite sheets and texture atlases: deterministic layout, pixels and a manifest.

Two modes, both PNG pages with real alpha unless a background colour is asked
for:

* ``grid`` - equal cells in rows and columns. Frames must already share one
  size (``fit: require_match``), or are placed unscaled and centred in a cell
  large enough for the biggest (``pad``), or are scaled down to fit a fixed
  cell keeping their aspect (``contain``). A frame is never stretched.
* ``atlas`` - frames packed on shelves (tallest first, then by frame index),
  optionally trimmed to their visible pixels, across one or more pages of a
  maximum size. Rotation is never used.

Coordinates are pixels from the top-left of the page. A frame rectangle is the
content only: it excludes padding and extrusion. Padding is the clear space
between rectangles and from each page edge; extrusion repeats a frame's edge
pixels outward into that space, which is why padding must be at least twice
the extrusion. After rendering, every rectangle is checked to lie inside its
page and not overlap another, and every frame is cut back out of its page and
compared with what was meant to be drawn there.

Pillow only: no numpy, no GPU, nothing that waits on the generation lock.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from PIL import Image, ImageChops
from pydantic import BaseModel, ConfigDict, Field, model_validator

FORMAT = "wizards-brush/atlas"
FORMAT_VERSION = 1
MAX_FRAMES = 1024
MAX_PAGE = 8192
MAX_PAGES = 16
MAX_TOTAL_PIXELS = 256 * 1024 * 1024
_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


class SpriteError(ValueError):
    """An export that cannot be made as asked. The message is user-facing."""


class ExportProfile(BaseModel):
    """A versioned, previewable description of one sheet or atlas export."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    mode: Literal["grid", "atlas"] = "grid"
    name: str = Field(default="sheet", min_length=1, max_length=64)
    order: Literal["set", "key"] = Field(
        default="set", description="set: the set's own variant order; key: by frame key.")
    # grid
    columns: int | None = Field(default=None, ge=1, le=256)
    cell_width: int | None = Field(default=None, ge=1, le=MAX_PAGE)
    cell_height: int | None = Field(default=None, ge=1, le=MAX_PAGE)
    fit: Literal["require_match", "pad", "contain"] = "require_match"
    # atlas
    max_width: int = Field(default=2048, ge=16, le=MAX_PAGE)
    max_height: int = Field(default=2048, ge=16, le=MAX_PAGE)
    trim: bool = False
    allow_multiple_pages: bool = False
    # both
    padding: int = Field(default=2, ge=0, le=256)
    extrude: int = Field(default=0, ge=0, le=64)
    background: str = Field(default="transparent",
                            description="transparent, or #rrggbb for an opaque page.")
    pixel_art: bool = Field(default=False,
                            description="Nearest-neighbour scaling and no smoothing anywhere.")
    pivot: tuple[float, float] = (0.5, 0.5)
    durations: dict[str, int] = Field(
        default_factory=dict, description="Frame key -> milliseconds. Metadata only; never "
                                          "inferred.")
    partial: bool = Field(default=False, description="Export the frames that exist and list "
                                                     "the rest, instead of refusing.")

    @model_validator(mode="after")
    def _check(self) -> ExportProfile:
        if self.background != "transparent" and not _COLOUR.fullmatch(self.background):
            raise ValueError("background must be transparent or #rrggbb")
        if self.extrude and self.padding < 2 * self.extrude:
            raise ValueError("padding must be at least twice the extrusion, or extruded edges "
                             "of neighbouring frames would overlap")
        if (self.cell_width is None) != (self.cell_height is None):
            raise ValueError("give both cell_width and cell_height, or neither")
        if self.fit == "contain" and self.cell_width is None:
            raise ValueError("fit contain scales frames into a fixed cell: give its size")
        if not all(0.0 <= p <= 1.0 for p in self.pivot):
            raise ValueError("pivot is a fraction of the frame, 0 to 1 on each axis")
        if any(not 0 < d <= 600_000 for d in self.durations.values()):
            raise ValueError("durations are 1 to 600000 milliseconds")
        return self


@dataclass
class Frame:
    """One frame to place. `index` is its position in the requested order,
    kept even when frames before it are missing."""

    index: int
    key: str
    image: Image.Image
    asset_id: int | None = None
    labels: dict[str, Any] = field(default_factory=dict)


@dataclass
class Placed:
    frame: Frame
    content: Image.Image        # exactly what is drawn at the rectangle
    page: int
    x: int
    y: int
    trim_x: int = 0
    trim_y: int = 0
    scale: float = 1.0

    @property
    def w(self) -> int:
        return self.content.width

    @property
    def h(self) -> int:
        return self.content.height


@dataclass
class Layout:
    pages: list[tuple[int, int]]
    placed: list[Placed]
    cell: tuple[int, int] | None = None
    columns: int | None = None


# ---- frame preparation -----------------------------------------------------------------
def _rgba(image: Image.Image) -> Image.Image:
    return image if image.mode == "RGBA" else image.convert("RGBA")


def _trimmed(image: Image.Image) -> tuple[Image.Image, int, int]:
    """Cropped to visible pixels. A fully transparent frame keeps one pixel so
    it still has a rectangle; its trim offset says where that pixel was."""
    box = image.getchannel("A").getbbox()
    if box is None:
        return image.crop((0, 0, 1, 1)), 0, 0
    return image.crop(box), box[0], box[1]


def _resample(profile: ExportProfile) -> Image.Resampling:
    return Image.Resampling.NEAREST if profile.pixel_art else Image.Resampling.LANCZOS


# ---- layout ---------------------------------------------------------------------------------
def layout(frames: list[Frame], profile: ExportProfile) -> Layout:
    """Where every frame goes. Raises SpriteError when it cannot be laid out."""
    if not frames:
        raise SpriteError("there are no frames to export")
    if len(frames) > MAX_FRAMES:
        raise SpriteError(f"at most {MAX_FRAMES} frames per export")
    prepared = [(f, _rgba(f.image)) for f in frames]
    return _grid(prepared, profile) if profile.mode == "grid" else _atlas(prepared, profile)


def _grid(prepared: list[tuple[Frame, Image.Image]], profile: ExportProfile) -> Layout:
    sizes = {img.size for _f, img in prepared}
    if profile.cell_width is not None and profile.cell_height is not None:
        cell = (profile.cell_width, profile.cell_height)
    else:
        cell = (max(w for w, _h in sizes), max(h for _w, h in sizes))
    if profile.fit == "require_match":
        wrong = [f.key for f, img in prepared if img.size != cell]
        if wrong:
            raise SpriteError(
                f"{len(wrong)} frame(s) are not {cell[0]}x{cell[1]} (e.g. {wrong[0]}); choose "
                "fit pad or contain, or make the frames one size")
    count = len(prepared)
    columns = profile.columns or math.ceil(math.sqrt(count))
    rows = math.ceil(count / columns)
    pad = profile.padding
    width = pad + columns * (cell[0] + pad)
    height = pad + rows * (cell[1] + pad)
    _check_page(width, height)
    placed: list[Placed] = []
    for position, (frame, img) in enumerate(prepared):
        col, row = position % columns, position // columns
        cx, cy = pad + col * (cell[0] + pad), pad + row * (cell[1] + pad)
        scale = 1.0
        if img.width > cell[0] or img.height > cell[1]:
            if profile.fit != "contain":
                raise SpriteError(f"frame {frame.key} is {img.width}x{img.height}, larger than "
                                  f"the {cell[0]}x{cell[1]} cell; use fit contain to scale it")
            scale = min(cell[0] / img.width, cell[1] / img.height)
            size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
            img = img.resize(size, _resample(profile))
        # Centred in its cell; the rectangle is where the pixels really are.
        x = cx + (cell[0] - img.width) // 2
        y = cy + (cell[1] - img.height) // 2
        placed.append(Placed(frame, img, 0, x, y, scale=scale))
    return Layout([(width, height)], placed, cell=cell, columns=columns)


def _atlas(prepared: list[tuple[Frame, Image.Image]], profile: ExportProfile) -> Layout:
    pad = profile.padding
    items: list[tuple[Frame, Image.Image, int, int]] = []
    for frame, img in prepared:
        content, tx, ty = _trimmed(img) if profile.trim else (img, 0, 0)
        if content.width + 2 * pad > profile.max_width or content.height + 2 * pad > profile.max_height:
            raise SpriteError(f"frame {frame.key} ({content.width}x{content.height}) does not "
                              f"fit a {profile.max_width}x{profile.max_height} page with "
                              f"{pad}px padding")
        items.append((frame, content, tx, ty))
    # Shelf packing: tallest first, ties by frame index, so the same frames
    # always land in the same places.
    items.sort(key=lambda it: (-it[1].height, -it[1].width, it[0].index))
    pages: list[list[Placed]] = [[]]
    x = y = pad
    shelf = 0
    used_w = [0]
    used_h = [0]
    for frame, content, tx, ty in items:
        if x + content.width + pad > profile.max_width:      # next shelf
            x, y = pad, y + shelf + pad
            shelf = 0
        if y + content.height + pad > profile.max_height:    # next page
            if not profile.allow_multiple_pages:
                raise SpriteError("the frames do not fit one page; allow multiple pages or "
                                  "raise the maximum size")
            if len(pages) >= MAX_PAGES:
                raise SpriteError(f"at most {MAX_PAGES} pages per export")
            pages.append([])
            used_w.append(0)
            used_h.append(0)
            x = y = pad
            shelf = 0
        page = len(pages) - 1
        pages[page].append(Placed(frame, content, page, x, y, tx, ty))
        used_w[page] = max(used_w[page], x + content.width + pad)
        used_h[page] = max(used_h[page], y + content.height + pad)
        x += content.width + pad
        shelf = max(shelf, content.height)
    sizes = [(used_w[i], used_h[i]) for i in range(len(pages))]
    for w, h in sizes:
        _check_page(w, h)
    placed = sorted((p for page in pages for p in page), key=lambda p: p.frame.index)
    return Layout(sizes, placed)


def _check_page(width: int, height: int) -> None:
    if width > MAX_PAGE or height > MAX_PAGE:
        raise SpriteError(f"a {width}x{height} page is larger than {MAX_PAGE}x{MAX_PAGE}; use "
                          "more columns, smaller cells or an atlas with several pages")


# ---- rendering ------------------------------------------------------------------------------
def render(result: Layout, profile: ExportProfile) -> list[Image.Image]:
    """Draw every page. Raises SpriteError if the pages would be too large."""
    total = sum(w * h for w, h in result.pages)
    if total > MAX_TOTAL_PIXELS:
        raise SpriteError("the export would be larger than the export limit; split it")
    opaque = profile.background != "transparent"
    fill = _bg(profile) if opaque else (0, 0, 0, 0)
    pages = [Image.new("RGBA", size, fill) for size in result.pages]
    n = profile.extrude
    for item in result.placed:
        tile = _extruded(item.content, n) if n else item.content
        where = (item.x - n, item.y - n)
        # A transparent page takes the pixels exactly; an opaque one shows
        # each frame over its background colour.
        if opaque:
            pages[item.page].alpha_composite(tile, where)
        else:
            pages[item.page].paste(tile, where)
    return pages


def _extruded(content: Image.Image, n: int) -> Image.Image:
    """The content with its outermost pixels repeated n times on every side."""
    w, h = content.size
    out = Image.new("RGBA", (w + 2 * n, h + 2 * n))
    out.paste(content, (n, n))
    near = Image.Resampling.NEAREST
    edges = (
        ((0, 0, w, 1), (w, n), (n, 0)), ((0, h - 1, w, h), (w, n), (n, h + n)),
        ((0, 0, 1, h), (n, h), (0, n)), ((w - 1, 0, w, h), (n, h), (w + n, n)),
        ((0, 0, 1, 1), (n, n), (0, 0)), ((w - 1, 0, w, 1), (n, n), (w + n, 0)),
        ((0, h - 1, 1, h), (n, n), (0, h + n)), ((w - 1, h - 1, w, h), (n, n), (w + n, h + n)),
    )
    for box, size, where in edges:
        out.paste(content.crop(box).resize(size, near), where)
    return out


# ---- validation -----------------------------------------------------------------------------
def validate(result: Layout, pages: list[Image.Image], profile: ExportProfile) -> None:
    """Every rectangle inside its page, none overlapping, every frame intact."""
    by_page: dict[int, list[Placed]] = {}
    for item in result.placed:
        width, height = result.pages[item.page]
        if item.x < 0 or item.y < 0 or item.x + item.w > width or item.y + item.h > height:
            raise SpriteError(f"frame {item.frame.key} falls outside page {item.page}")
        by_page.setdefault(item.page, []).append(item)
    grow = profile.extrude
    for items in by_page.values():
        boxes = sorted((p.x - grow, p.y - grow, p.x + p.w + grow, p.y + p.h + grow, p.frame.key)
                        for p in items)
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                if b[0] >= a[2]:
                    break
                if a[1] < b[3] and b[1] < a[3]:
                    raise SpriteError(f"frames {a[4]} and {b[4]} overlap")
    opaque = profile.background != "transparent"
    for item in result.placed:
        cut = pages[item.page].crop((item.x, item.y, item.x + item.w, item.y + item.h))
        expected = item.content
        if opaque:
            expected = Image.alpha_composite(Image.new("RGBA", expected.size, _bg(profile)),
                                             expected)
        if ImageChops.difference(cut, expected).getbbox() is not None:
            raise SpriteError(f"frame {item.frame.key} did not survive a round trip")


def _bg(profile: ExportProfile) -> tuple[int, int, int, int]:
    hexed = profile.background
    return int(hexed[1:3], 16), int(hexed[3:5], 16), int(hexed[5:7], 16), 255


# ---- manifest -------------------------------------------------------------------------------
def page_name(profile: ExportProfile, index: int, count: int) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", profile.name).strip("-.") or "sheet"
    return f"{base}.png" if count == 1 else f"{base}-{index + 1}.png"


def manifest(result: Layout, profile: ExportProfile, *, export_id: str,
             missing: list[dict[str, Any]], include_labels: bool) -> dict[str, Any]:
    count = len(result.pages)
    frames = []
    for item in result.placed:
        f = item.frame
        source = f.image
        record: dict[str, Any] = {
            "index": f.index, "key": f.key, "asset_id": f.asset_id, "page": item.page,
            "x": item.x, "y": item.y, "w": item.w, "h": item.h,
            "original_w": source.width, "original_h": source.height,
            "trim_x": item.trim_x, "trim_y": item.trim_y,
            "scale": round(item.scale, 6),
            "pivot": {"x": profile.pivot[0], "y": profile.pivot[1]},
        }
        if f.key in profile.durations:
            record["duration_ms"] = profile.durations[f.key]
        if include_labels and f.labels:
            record["labels"] = f.labels
        frames.append(record)
    out: dict[str, Any] = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "export_id": export_id,
        "mode": profile.mode, "coordinate_origin": "top-left", "units": "px",
        "rotated": False, "rect_includes_padding": False,
        "padding": profile.padding, "extrude": profile.extrude,
        "pages": [{"file": page_name(profile, i, count), "width": w, "height": h}
                  for i, (w, h) in enumerate(result.pages)],
        "frames": frames,
        "missing": missing,
        "profile": profile.model_dump(mode="json"),
    }
    if result.cell is not None:
        out["grid"] = {"columns": result.columns, "cell_width": result.cell[0],
                       "cell_height": result.cell[1]}
    return out


def preview(result: Layout, missing: list[dict[str, Any]]) -> dict[str, Any]:
    """What an export would produce, without drawing it."""
    return {
        "pages": [{"width": w, "height": h} for w, h in result.pages],
        "frames": [{"index": p.frame.index, "key": p.frame.key, "page": p.page, "x": p.x,
                    "y": p.y, "w": p.w, "h": p.h} for p in result.placed],
        "missing": missing,
        "grid": ({"columns": result.columns, "cell_width": result.cell[0],
                  "cell_height": result.cell[1]} if result.cell else None),
    }
