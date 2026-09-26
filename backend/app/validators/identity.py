"""Identity lock checks: unmasked pixels and optional SFace cosine.

Unmasked equality is pure Pillow. Face identity downloads OpenCV zoo weights
on first use (Apache-2.0); a missing runtime is a warning, not a fail, so a
Variant Set cannot stall CI or a CPU-only install.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageOps

from . import Result, ValidationContext, register


def _open_rgb(path: Path | str | None) -> Image.Image | None:
    if not path:
        return None
    file = Path(path)
    if not file.is_file():
        return None
    with Image.open(file) as img:
        img.load()
        return ImageOps.exif_transpose(img).convert("RGB")


def _source_path(ctx: ValidationContext) -> Path | None:
    raw = ctx.meta.get("source_path") or ctx.meta.get("image_path")
    if raw:
        return Path(str(raw))
    ids = ctx.source_asset_ids
    if not ids:
        return None
    from .. import db

    asset = db.get_asset(int(ids[0]))
    return Path(asset.path) if asset and asset.path else None


def _mask_image(ctx: ValidationContext, size: tuple[int, int]) -> Image.Image | None:
    raw = ctx.meta.get("mask_path")
    if not raw:
        return None
    file = Path(str(raw))
    if not file.is_file():
        return None
    with Image.open(file) as mask:
        mask.load()
        out = ImageOps.exif_transpose(mask).convert("L")
    if out.size != size:
        out = out.resize(size, Image.Resampling.NEAREST)
    return out.point(lambda value: 255 if value > 8 else 0)


def check_unmasked(ctx: ValidationContext) -> Result | None:
    if not ctx.spec.unmasked_match or ctx.image is None:
        return None
    source = _open_rgb(_source_path(ctx))
    if source is None:
        return Result("unmasked_match", "warn",
                      "unmasked-pixel lock needs the source image")
    output = ImageOps.exif_transpose(ctx.image).convert("RGB")
    if source.size != output.size:
        return Result("unmasked_match", "fail",
                      f"source is {source.width}×{source.height} but the output is "
                      f"{output.width}×{output.height}")
    mask = _mask_image(ctx, source.size)
    if mask is None:
        return Result("unmasked_match", "warn",
                      "unmasked-pixel lock needs the original hard mask")
    keep = ImageChops.invert(mask)
    kept_source = ImageChops.composite(source, Image.new("RGB", source.size), keep)
    kept_output = ImageChops.composite(output, Image.new("RGB", output.size), keep)
    diff = ImageChops.difference(kept_source, kept_output)
    bands = diff.getextrema()
    highs: list[int] = []
    for band in bands:
        highs.append(int(band[1] if isinstance(band, tuple) else band))
    worst = max(highs) if highs else 0
    details = {"max_channel_delta": int(worst)}
    if worst > 0:
        return Result("unmasked_match", "fail",
                      "unmasked pixels differ from the source photograph", details)
    return Result("unmasked_match", "pass",
                  "unmasked pixels match the source exactly", details)


def check_face_identity(ctx: ValidationContext) -> Result | None:
    want = ctx.spec.face_identity_min
    if want is None or ctx.image is None:
        return None
    source = _open_rgb(_source_path(ctx))
    if source is None:
        return Result("face_identity", "warn", "face identity needs the source image")
    try:
        from ..generators import identity
        score = identity.compare(source, ImageOps.exif_transpose(ctx.image).convert("RGB"))
    except Exception as error:  # noqa: BLE001
        return Result("face_identity", "warn", f"face identity could not run: {error}")
    if score is None:
        return Result("face_identity", "warn", "no face was found on the source or the output")
    details = {"score": round(score, 4), "minimum": want}
    if score + 1e-9 < want:
        return Result("face_identity", "fail",
                      f"face similarity {score:.2f} is below the {want:.2f} lock", details)
    return Result("face_identity", "pass", f"face similarity {score:.2f}", details)


register("unmasked_match", check_unmasked)
register("face_identity", check_face_identity)
