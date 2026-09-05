"""Outpainting geometry: turn "extend this image" into an inpaint job.

Outpainting is inpainting on a larger canvas — the source is pasted into a
bigger frame and everything else is masked. The interesting part is entirely
geometric, so it lives here as pure functions the tests can exercise without
torch or a GPU; `plan()` decides the numbers and `build()` does the pixels.

Two details that matter for output quality:

  * the mask overlaps the source by a few pixels, so the model re-paints a thin
    band of known content and blends into it rather than leaving a hard seam;
  * the canvas is snapped to a multiple of 16, matching the rest of the app's
    sizing, because the VAE downsamples by 8 and patchifies by 2.
"""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from . import log

logger = log.get("outpaint")

# Directions the UI can extend in, as (left, top, right, bottom) unit vectors.
DIRECTIONS: dict[str, tuple[int, int, int, int]] = {
    "left":   (1, 0, 0, 0),
    "right":  (0, 0, 1, 0),
    "up":     (0, 1, 0, 0),
    "down":   (0, 0, 0, 1),
    "all":    (1, 1, 1, 1),
    "wider":  (1, 0, 1, 0),
    "taller": (0, 1, 0, 1),
}

# Percent of the source's width/height to add per extended edge.
MIN_PCT, MAX_PCT, DEFAULT_PCT = 5, 100, 25
# Denoise strength for the outpaint pass. Low enough that the model respects the
# edge-extended priming — see the note in the handler; a high value is what makes
# an outpaint look like an outpaint.
OUTPAINT_STRENGTH = 0.65
# The model repaints this far into known pixels so the seam blends.
OVERLAP_PX = 24
# Hard ceiling per side, so "all" at 100% on a large input cannot explode.
MAX_SIDE = 2048


def _snap16(v: int, *, at_least: int = 16) -> int:
    """Floor-snap a target without ever snapping below required source pixels."""
    floor = max(16, int(at_least))
    snapped = max(16, (v // 16) * 16)
    return max(snapped, ((floor + 15) // 16) * 16)


@dataclass(frozen=True)
class Plan:
    """Where the source lands on the new canvas, and how big that canvas is."""
    canvas_w: int
    canvas_h: int
    paste_x: int
    paste_y: int
    src_w: int
    src_h: int

    @property
    def grew(self) -> bool:
        return (self.canvas_w, self.canvas_h) != (self.src_w, self.src_h)


def plan(src_w: int, src_h: int, direction: str = "all", pct: int = DEFAULT_PCT) -> Plan:
    """Canvas size and paste offset for extending `direction` by `pct` percent.

    Snapping happens on the canvas, then the paste offset is recomputed from the
    real edges, so the source is never scaled or cropped — outpainting must
    preserve the original pixels exactly.
    """
    pct = max(MIN_PCT, min(int(pct), MAX_PCT))
    left, top, right, bottom = DIRECTIONS.get(direction, DIRECTIONS["all"])
    dx = round(src_w * pct / 100)
    dy = round(src_h * pct / 100)

    pad_l, pad_r = left * dx, right * dx
    pad_t, pad_b = top * dy, bottom * dy

    canvas_w = _snap16(min(src_w + pad_l + pad_r, max(MAX_SIDE, src_w)), at_least=src_w)
    canvas_h = _snap16(min(src_h + pad_t + pad_b, max(MAX_SIDE, src_h)), at_least=src_h)

    # Distribute whatever room the snap/cap actually left, in the requested
    # proportion, so a one-sided extension stays one-sided.
    room_x = max(0, canvas_w - src_w)
    room_y = max(0, canvas_h - src_h)
    paste_x = round(room_x * (pad_l / (pad_l + pad_r))) if (pad_l + pad_r) else room_x // 2
    paste_y = round(room_y * (pad_t / (pad_t + pad_b))) if (pad_t + pad_b) else room_y // 2

    return Plan(canvas_w=canvas_w, canvas_h=canvas_h,
                paste_x=max(0, min(paste_x, room_x)),
                paste_y=max(0, min(paste_y, room_y)),
                src_w=src_w, src_h=src_h)


def build(img: Image.Image, p: Plan, overlap: int = OVERLAP_PX) -> tuple[Image.Image, Image.Image]:
    """(canvas, mask) for the inpaint pipeline.

    The canvas holds the source at its original size on an edge-extended
    background — a flat fill would give the model a hard colour boundary to
    latch onto, while smeared edge pixels read as plausible continuation.
    The mask is white everywhere the model should paint.
    """
    src = img.convert("RGB")
    canvas = _edge_extend(src, p)

    mask = Image.new("L", (p.canvas_w, p.canvas_h), 255)
    keep_x0 = p.paste_x + min(overlap, p.src_w // 4)
    keep_y0 = p.paste_y + min(overlap, p.src_h // 4)
    keep_x1 = p.paste_x + p.src_w - min(overlap, p.src_w // 4)
    keep_y1 = p.paste_y + p.src_h - min(overlap, p.src_h // 4)
    if keep_x1 > keep_x0 and keep_y1 > keep_y0:
        mask.paste(0, (keep_x0, keep_y0, keep_x1, keep_y1))
    return canvas, mask


def refine_priming(canvas: Image.Image, p: Plan, blur: int = 65) -> Image.Image:
    """Improve build()'s priming by continuing image structure into the new area.

    `build()` fills the new region by smearing the border outward. That is the
    right instinct — a flat fill gives the model a hard boundary to latch onto,
    and the boundary survives into the output as a seam — but a smear can only
    extend colour, never structure. A horizon, a wall edge or a gradient stops
    dead at the original frame.

    Navier-Stokes inpainting treats the image as a fluid and propagates
    isophotes (lines of equal intensity) into the unknown region, so those
    continue instead of stopping. Blurring afterwards is deliberate: NS output is
    plausible in the large and wrong in the small, and sharp invented detail is
    worse than none because it competes with what the model wants to draw. The
    blur keeps the low frequencies, which are right, and discards the high ones,
    which are not.

    **Kept out of build() on purpose.** cv2 is a heavy import, and build()/plan()/
    composite() are pure PIL so the geometry can be unit-tested in an environment
    with no imaging stack at all — which is what lets CI run without CUDA. This
    function is called from the generation handler, where cv2 is already loaded.

    Returns the canvas unchanged if cv2 is unavailable or anything goes wrong:
    priming affects quality, never correctness, and build()'s output is already
    a usable canvas.
    """
    if not p.grew:
        return canvas
    try:
        return _navier_stokes(canvas, p, blur)
    except Exception as e:  # noqa: BLE001 — quality step, never fatal
        logger.debug("navier-stokes priming unavailable (%s); keeping edge extend", e)
        return canvas


def _navier_stokes(canvas: Image.Image, p: Plan, blur: int) -> Image.Image:
    """NS-inpaint the region outside the source, blur it, restore the source."""
    import cv2
    import numpy as np
    from PIL import ImageFilter

    rgb = canvas.convert("RGB")
    src_box = (p.paste_x, p.paste_y, p.paste_x + p.src_w, p.paste_y + p.src_h)
    source = rgb.crop(src_box)

    # 255 where pixels are unknown — the region NS has to invent. The smeared
    # content build() left there is not evidence, so it is masked out too.
    hole = Image.new("L", rgb.size, 255)
    hole.paste(0, src_box)

    bgr = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)
    # inpaintRadius is a *local* neighbourhood size, not the fill distance.
    # Larger values are quadratically slower and no better here, where the region
    # being filled dwarfs any sane radius.
    filled = cv2.inpaint(bgr, np.array(hole), 3, cv2.INPAINT_NS)
    out = Image.fromarray(cv2.cvtColor(filled, cv2.COLOR_BGR2RGB))

    blurred = out.filter(ImageFilter.GaussianBlur(blur / 3))
    out.paste(blurred, (0, 0), hole)
    out.paste(source, (p.paste_x, p.paste_y))   # source stays byte-exact
    return out


def _edge_extend(src: Image.Image, p: Plan) -> Image.Image:
    """Source pasted onto a canvas whose new area is a smear of the nearest edge."""
    canvas = Image.new("RGB", (p.canvas_w, p.canvas_h))
    x, y, w, h = p.paste_x, p.paste_y, p.src_w, p.src_h

    # Stretch each border strip outward, then the corners from the strip ends.
    if y > 0:
        canvas.paste(src.crop((0, 0, w, 1)).resize((w, y), Image.Resampling.NEAREST), (x, 0))
    if p.canvas_h - (y + h) > 0:
        strip = src.crop((0, h - 1, w, h)).resize((w, p.canvas_h - (y + h)), Image.Resampling.NEAREST)
        canvas.paste(strip, (x, y + h))
    if x > 0:
        canvas.paste(canvas.crop((x, 0, x + 1, p.canvas_h)).resize((x, p.canvas_h),
                                                                   Image.Resampling.NEAREST), (0, 0))
    right = p.canvas_w - (x + w)
    if right > 0:
        canvas.paste(canvas.crop((x + w - 1, 0, x + w, p.canvas_h)).resize((right, p.canvas_h),
                                                                           Image.Resampling.NEAREST),
                     (x + w, 0))
    if x > 0:
        canvas.paste(src.crop((0, 0, 1, h)).resize((x, h), Image.Resampling.NEAREST), (0, y))
    if right > 0:
        canvas.paste(src.crop((w - 1, 0, w, h)).resize((right, h), Image.Resampling.NEAREST), (x + w, y))

    canvas.paste(src, (x, y))
    return canvas


def composite(generated: Image.Image, src: Image.Image, p: Plan,
              overlap: int = OVERLAP_PX) -> Image.Image:
    """Put the true original pixels back, cross-fading across the overlap band.

    Without this the "preserved" region is not preserved at all: an inpaint
    pipeline encodes and decodes the whole canvas, so the untouched area comes
    back through the VAE slightly shifted in brightness and contrast. Against
    freshly generated surroundings that reads as a visible rectangular seam
    exactly where the source used to be — the one artefact that makes an
    outpaint obviously an outpaint.

    So: interior is byte-identical source, exterior is model output, and the
    overlap band ramps between them so neither edge shows.
    """
    from PIL import ImageDraw, ImageFilter

    out = generated.convert("RGB").copy()
    src = src.convert("RGB")
    x, y, w, h = p.paste_x, p.paste_y, p.src_w, p.src_h

    # Feather no wider than the band build() actually opened, or the ramp would
    # reach into pixels the model was never allowed to touch.
    feather = max(1, min(overlap, w // 4, h // 4))

    # White where the source should win, ramped down over `feather` px at its edge.
    alpha = Image.new("L", (w, h), 0)
    ImageDraw.Draw(alpha).rectangle(
        [feather, feather, w - 1 - feather, h - 1 - feather], fill=255)
    alpha = alpha.filter(ImageFilter.GaussianBlur(feather / 2))

    out.paste(src, (x, y), alpha)
    return out
