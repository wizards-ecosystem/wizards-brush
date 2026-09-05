"""Mask-aware inpaint geometry and lossless source compositing.

Diffusion works best in a bounded model-native pixel bucket. Editing works best
when the user's original canvas, framing, and unpainted pixels do not move. This
module is the bridge: it may crop a padded region for diffusion, then maps the
result back and changes only pixels selected by the original mask.
"""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageFilter, ImageOps


@dataclass(frozen=True)
class InpaintPlan:
    source: Image.Image
    mask: Image.Image
    generation_mask: Image.Image
    crop: tuple[int, int, int, int]
    regional: bool

    @property
    def input_image(self) -> Image.Image:
        return self.source.crop(self.crop)

    @property
    def input_mask(self) -> Image.Image:
        return self.generation_mask.crop(self.crop)

    @property
    def crop_size(self) -> tuple[int, int]:
        left, top, right, bottom = self.crop
        return right - left, bottom - top

    def composite(self, generated: Image.Image, blur: int = 4) -> Image.Image:
        """Place a generated crop back without touching unmasked source pixels."""
        generated = ImageOps.exif_transpose(generated).convert("RGB")
        if generated.size != self.crop_size:
            generated = generated.resize(self.crop_size, Image.Resampling.LANCZOS)

        left, top, right, bottom = self.crop
        original_crop = self.source.crop(self.crop)
        hard = self.mask.crop(self.crop)
        if blur > 0:
            # Blur inward only. A raw Gaussian mask leaks outside the painted
            # area and subtly rewrites pixels the user never selected.
            soft = hard.filter(ImageFilter.GaussianBlur(radius=min(int(blur), 128)))
            blend_mask = ImageChops.multiply(soft, hard)
        else:
            blend_mask = hard
        edited = Image.composite(generated, original_crop, blend_mask)
        out = self.source.copy()
        out.paste(edited, (left, top, right, bottom))
        return out


def _grow(mask: Image.Image, pixels: int) -> Image.Image:
    pixels = max(0, min(int(pixels), 128))
    if not pixels:
        return mask
    # MaxFilter's radius must be odd. A diameter of 2*n+1 grows by n pixels.
    return mask.filter(ImageFilter.MaxFilter(2 * pixels + 1))


def _expanded_box(
    box: tuple[int, int, int, int], canvas: tuple[int, int], padding: int,
) -> tuple[int, int, int, int]:
    """Pad and snap crop edges outward to /16 without leaving the canvas."""
    width, height = canvas
    left, top, right, bottom = box
    padding = max(0, min(int(padding), 1024))
    left, top = max(0, left - padding), max(0, top - padding)
    right, bottom = min(width, right + padding), min(height, bottom + padding)

    left = max(0, (left // 16) * 16)
    top = max(0, (top // 16) * 16)
    right = min(width, ((right + 15) // 16) * 16)
    bottom = min(height, ((bottom + 15) // 16) * 16)
    return left, top, right, bottom


def plan_inpaint(
    source: Image.Image, mask: Image.Image, *, regional: bool,
    padding: int = 64, grow: int = 4,
) -> InpaintPlan:
    """Normalize a mask and choose either a full-canvas or padded region crop."""
    source = ImageOps.exif_transpose(source).convert("RGB")
    mask = ImageOps.exif_transpose(mask).convert("L")
    if mask.size != source.size:
        # Masks are categorical geometry; LANCZOS creates grey selections and
        # shifts their edge. Nearest preserves what was actually painted.
        mask = mask.resize(source.size, Image.Resampling.NEAREST)
    mask = mask.point(lambda value: 255 if value > 8 else 0)
    selected = mask.getbbox()
    if selected is None:
        raise ValueError("The inpaint mask is empty. Paint the area you want to regenerate.")

    generation_mask = _grow(mask, grow)
    crop = ((0, 0, *source.size) if not regional else
            _expanded_box(generation_mask.getbbox() or selected, source.size, padding))
    return InpaintPlan(source, mask, generation_mask, crop, regional)
