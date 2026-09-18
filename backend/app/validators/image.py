"""The built-in checks: readability, format, size and transparency.

Pure Pillow. Transparency is read from the real alpha channel, so a picture of a
checkerboard is exactly what it looks like — an opaque image — and fails
`alpha="required"`.
"""
from __future__ import annotations

from PIL import Image

from . import Result, ValidationContext, register


def _alpha(ctx: ValidationContext) -> Image.Image | None:
    """The alpha channel, or None when the file has none."""
    img = ctx.image
    if img is None:
        return None
    if img.mode in ("RGBA", "LA", "La", "RGBa"):
        return img.getchannel("A")
    if img.mode == "PA" or (img.mode in ("P", "L", "RGB") and "transparency" in img.info):
        return img.convert("RGBA").getchannel("A")
    return None


def _transparent_count(alpha: Image.Image, threshold: int) -> int:
    return sum(alpha.histogram()[: threshold + 1])


def check_file(ctx: ValidationContext) -> Result:
    path = ctx.path
    if not path.is_file():
        return Result("file", "fail", "the output file does not exist", {"path": path.name})
    try:
        with Image.open(path) as probe:
            probe.verify()          # structure and checksums, without decoding
        img = Image.open(path)
        img.load()                  # verify() leaves the image unusable; decode for real
    except Exception as error:  # noqa: BLE001 — any decoder failure is the finding
        return Result("file", "fail", f"the output file is not a readable image: {error}",
                      {"path": path.name})
    ctx.image, ctx.image_format = img, str(img.format or "")
    return Result("file", "pass", f"{img.format} {img.width}×{img.height} ({img.mode})",
                  {"format": img.format, "width": img.width, "height": img.height,
                   "mode": img.mode, "bytes": path.stat().st_size})


def check_format(ctx: ValidationContext) -> Result | None:
    want = ctx.spec.format
    if want is None:
        return None
    got = ctx.image_format.upper()
    if got == want:
        return Result("format", "pass", f"format is {want}", {"expected": want, "actual": got})
    return Result("format", "fail", f"expected {want}, got {got or 'unknown'}",
                  {"expected": want, "actual": got})


def check_dimensions(ctx: ValidationContext) -> Result | None:
    spec, img = ctx.spec, ctx.image
    if img is None or (spec.width is None and spec.height is None):
        return None
    want_w = spec.width if spec.width is not None else img.width
    want_h = spec.height if spec.height is not None else img.height
    details = {"expected": {"width": spec.width, "height": spec.height},
               "actual": {"width": img.width, "height": img.height}}
    if (img.width, img.height) == (want_w, want_h):
        return Result("dimensions", "pass", f"{img.width}×{img.height} as expected", details)
    expected = f"{spec.width or '*'}×{spec.height or '*'}"
    return Result("dimensions", "fail", f"expected {expected}, got {img.width}×{img.height}",
                  details)


def check_alpha(ctx: ValidationContext) -> Result | None:
    mode = ctx.spec.alpha
    if mode == "any" or ctx.image is None:
        return None
    alpha = _alpha(ctx)
    if mode == "required":
        if alpha is None:
            return Result("alpha", "fail", "an alpha channel is required and the file has none",
                          {"mode": ctx.image.mode})
        low, _high = alpha.getextrema()
        if low == 255:
            return Result("alpha", "warn", "the file has an alpha channel but every pixel is opaque",
                          {"mode": ctx.image.mode})
        return Result("alpha", "pass", "genuine alpha channel present", {"mode": ctx.image.mode})
    # forbidden: an alpha channel is fine as long as nothing is see-through.
    if alpha is None:
        return Result("alpha", "pass", "fully opaque (no alpha channel)", {"mode": ctx.image.mode})
    low, _high = alpha.getextrema()
    if low == 255:
        return Result("alpha", "pass", "alpha channel present but fully opaque",
                      {"mode": ctx.image.mode})
    return Result("alpha", "fail", "transparency is not allowed and some pixels are transparent",
                  {"mode": ctx.image.mode, "min_alpha": low})


def check_corners(ctx: ValidationContext) -> Result | None:
    if not ctx.spec.corners_transparent or ctx.image is None:
        return None
    alpha = _alpha(ctx)
    if alpha is None:
        return Result("corners", "fail", "corner transparency needs an alpha channel")
    w, h = alpha.size
    corners = {"top_left": (0, 0), "top_right": (w - 1, 0),
               "bottom_left": (0, h - 1), "bottom_right": (w - 1, h - 1)}
    values = {name: int(alpha.getpixel(xy)) for name, xy in corners.items()}  # type: ignore[arg-type]
    opaque = [name for name, value in values.items() if value > ctx.spec.transparent_threshold]
    if opaque:
        return Result("corners", "fail", f"corner(s) not transparent: {', '.join(opaque)}",
                      {"alpha": values, "threshold": ctx.spec.transparent_threshold})
    return Result("corners", "pass", "all four corners are transparent",
                  {"alpha": values, "threshold": ctx.spec.transparent_threshold})


def check_coverage(ctx: ValidationContext) -> Result | None:
    spec = ctx.spec
    if (spec.min_transparent_fraction is None and spec.max_transparent_fraction is None) \
            or ctx.image is None:
        return None
    alpha = _alpha(ctx)
    if alpha is None:
        return Result("coverage", "fail", "transparent coverage needs an alpha channel")
    total = alpha.width * alpha.height
    fraction = _transparent_count(alpha, spec.transparent_threshold) / total if total else 0.0
    details = {"transparent_fraction": round(fraction, 4),
               "min": spec.min_transparent_fraction, "max": spec.max_transparent_fraction}
    if spec.min_transparent_fraction is not None and fraction < spec.min_transparent_fraction:
        return Result("coverage", "fail",
                      f"{fraction:.1%} transparent, below the {spec.min_transparent_fraction:.1%} "
                      "minimum", details)
    if spec.max_transparent_fraction is not None and fraction > spec.max_transparent_fraction:
        return Result("coverage", "fail",
                      f"{fraction:.1%} transparent, above the {spec.max_transparent_fraction:.1%} "
                      "maximum — the subject may have been removed", details)
    return Result("coverage", "pass", f"{fraction:.1%} transparent", details)


def check_margins(ctx: ValidationContext) -> Result | None:
    """Visible content keeps a clear border. Only decidable with real alpha:
    without it, where the subject ends is a guess, and a guess is reported as a
    warning rather than passed or failed."""
    margin = ctx.spec.safe_margin
    if margin is None or ctx.image is None:
        return None
    alpha = _alpha(ctx)
    if alpha is None:
        return Result("margins", "warn",
                      "safe margins can only be measured on an image with transparency")
    visible = alpha.point(lambda a: 255 if a > ctx.spec.transparent_threshold else 0)
    box = visible.getbbox()
    if box is None:
        return Result("margins", "fail", "nothing visible: the image is fully transparent")
    left, top, right, bottom = box
    gaps = {"left": left, "top": top, "right": alpha.width - right,
            "bottom": alpha.height - bottom}
    tight = [side for side, gap in gaps.items() if gap < margin]
    details = {"required": margin, "margins": gaps, "content_box": list(box)}
    if tight:
        return Result("margins", "fail",
                      f"content is closer than {margin}px to the {', '.join(tight)} edge(s)",
                      details)
    return Result("margins", "pass", f"content keeps at least {margin}px from every edge",
                  details)


for _name, _check in (("file", check_file), ("format", check_format),
                      ("dimensions", check_dimensions), ("alpha", check_alpha),
                      ("corners", check_corners), ("coverage", check_coverage),
                      ("margins", check_margins)):
    register(_name, _check)
