"""Finishing processors: explicit, recorded steps applied to a finished image.

Two ways to ask for finishing, one pipeline (``routers.common.apply_image_post``):

* the ``finish`` preset, expanded by ``resolve_finish`` into
  ``post_detail``/``post_face``/``post_upscale`` — unchanged, and still the
  stored record of those steps;
* ``finish_steps``, an ordered list of named processors that runs after them.

A processor takes an image and options and returns a finished image plus a
record of what it did. The record lands in the asset's ``post`` history, the
same append-only list the preset steps write, so provenance has one shape. A
processor that fails degrades the way the preset steps always have: the image
from before the step is kept, because the generation that preceded it was the
expensive part, and the failure is written to the asset's ``warnings`` rather
than into ``post`` — the history only ever lists steps that actually happened.

Adding a processor is one :func:`register` call with a stable name (it is
persisted in job params and replayed on rerun: never rename one). Nothing here
knows about Variant Sets; they are one caller among many.

Built-ins:

* ``resize``             — exact canvas size, deterministic (Pillow, contain/cover/stretch).
* ``background_removal`` — a real alpha channel (BiRefNet-lite, optional; see
  ``generators/matting.py``).
* ``upscale``, ``face_restore`` — the existing tools, orderable relative to the
  others and alpha-preserving.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageOps

from . import log

logger = log.get("finishing")

MAX_STEPS = 6

Progress = Callable[[float, str], None]
Apply = Callable[[Image.Image, dict[str, Any], Progress], tuple[Image.Image, dict[str, Any]]]


class FinishingError(ValueError):
    """A finishing request that cannot be run as written. User-facing message."""


def _always_available() -> str | None:
    return None


@dataclass(frozen=True)
class Processor:
    name: str                       # persisted identifier
    label: str
    description: str
    clean: Callable[[dict[str, Any]], dict[str, Any]]   # validate options -> canonical options
    apply: Apply
    unavailable: Callable[[], str | None] = _always_available
    # Option descriptors in the registry's control shape, for the UI.
    options: tuple[dict[str, Any], ...] = field(default_factory=tuple)


PROCESSORS: dict[str, Processor] = {}


def register(processor: Processor) -> None:
    PROCESSORS[processor.name] = processor


def has_alpha(img: Image.Image) -> bool:
    return img.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in img.info


def _keep_alpha(img: Image.Image, run: Callable[[Image.Image], Image.Image]) -> Image.Image:
    """Run an RGB-only tool without losing transparency: the alpha channel is
    carried across, resampled to the tool's output size."""
    if not has_alpha(img):
        return run(img)
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    out = run(rgba.convert("RGB")).convert("RGBA")
    if alpha.size != out.size:
        alpha = alpha.resize(out.size, Image.Resampling.LANCZOS)
    out.putalpha(alpha)
    return out


# ---- resize ---------------------------------------------------------------------
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _dimension(raw: Any, name: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise FinishingError(f"resize needs a whole-number {name}") from None
    if not 1 <= value <= 8192:
        raise FinishingError(f"resize {name} must be between 1 and 8192")
    return value


def _clean_resize(options: dict[str, Any]) -> dict[str, Any]:
    mode = str(options.get("mode", "contain"))
    if mode not in ("contain", "cover", "stretch"):
        raise FinishingError("resize mode must be contain, cover or stretch")
    background = str(options.get("background", "transparent"))
    if background != "transparent" and not _HEX.fullmatch(background):
        raise FinishingError("resize background must be 'transparent' or a #rrggbb colour")
    return {"width": _dimension(options.get("width"), "width"),
            "height": _dimension(options.get("height"), "height"),
            "mode": mode, "background": background.lower()}


def _apply_resize(img: Image.Image, o: dict[str, Any],
                  _progress: Progress) -> tuple[Image.Image, dict[str, Any]]:
    size = (int(o["width"]), int(o["height"]))
    keep_alpha = has_alpha(img)
    src = img.convert("RGBA")
    if o["mode"] == "stretch":
        out = src.resize(size, Image.Resampling.LANCZOS)
    elif o["mode"] == "cover":
        out = ImageOps.fit(src, size, Image.Resampling.LANCZOS)
    else:
        fitted = ImageOps.contain(src, size, Image.Resampling.LANCZOS)
        if o["background"] == "transparent":
            fill: tuple[int, int, int, int] = (0, 0, 0, 0)
            keep_alpha = True
        else:
            hex_ = o["background"].lstrip("#")
            fill = (int(hex_[0:2], 16), int(hex_[2:4], 16), int(hex_[4:6], 16), 255)
            keep_alpha = False  # an opaque canvas cannot show through
        out = Image.new("RGBA", size, fill)
        out.alpha_composite(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
    if not keep_alpha:
        out = out.convert("RGB")
    return out, {"width": size[0], "height": size[1], "mode": o["mode"],
                 "background": o["background"]}


# ---- background removal ---------------------------------------------------------------
def _clean_nothing(options: dict[str, Any]) -> dict[str, Any]:
    if options:
        raise FinishingError(f"this step takes no options (got {', '.join(sorted(options))})")
    return {}


def _background_unavailable() -> str | None:
    from .generators import matting

    return matting.unavailable_reason()


def _apply_background_removal(img: Image.Image, _o: dict[str, Any],
                              progress: Progress) -> tuple[Image.Image, dict[str, Any]]:
    from .generators import matting

    progress(0.0, "removing background")
    out, record = matting.remove_background(img)
    progress(1.0, "background removed")
    return out, record


# ---- the existing tools, orderable and alpha-preserving ----------------------------------
def _clean_upscale(options: dict[str, Any]) -> dict[str, Any]:
    extra = set(options) - {"scale"}
    if extra:
        raise FinishingError(f"upscale takes only 'scale' (got {', '.join(sorted(extra))})")
    try:
        scale = int(options.get("scale", 2))
    except (TypeError, ValueError):
        raise FinishingError("upscale scale must be 2 or 4") from None
    if scale not in (2, 4):
        raise FinishingError("upscale scale must be 2 or 4")
    return {"scale": scale}


def _tools_unavailable(*modules: str) -> Callable[[], str | None]:
    def reason() -> str | None:
        import importlib.util

        missing = [m for m in modules if importlib.util.find_spec(m) is None]
        return (f"needs {', '.join(missing)}, installed by `make setup`" if missing else None)

    return reason


def _apply_upscale(img: Image.Image, o: dict[str, Any],
                   progress: Progress) -> tuple[Image.Image, dict[str, Any]]:
    from .generators import postprocess as pp

    scale = int(o["scale"])
    out = _keep_alpha(img, lambda rgb: pp.upscale_image(rgb, scale=scale, progress_cb=progress))
    return out, {"scale": scale, "max_side": pp.MAX_UPSCALE_SIDE}


def _apply_face_restore(img: Image.Image, _o: dict[str, Any],
                        progress: Progress) -> tuple[Image.Image, dict[str, Any]]:
    from .generators import postprocess as pp

    return _keep_alpha(img, lambda rgb: pp.restore_faces(rgb, progress_cb=progress)), {}


register(Processor(
    "background_removal", "Remove background",
    "Replaces the background with real transparency (PNG alpha) using the MIT-licensed "
    "BiRefNet-lite model. Downloads a 224 MB weight on first use.",
    _clean_nothing, _apply_background_removal, _background_unavailable,
))
register(Processor(
    "resize", "Resize to exact size",
    "Fits the image to an exact canvas. Contain pads with transparency or a colour; "
    "cover crops to fill; stretch ignores the aspect ratio.",
    _clean_resize, _apply_resize,
    options=(
        {"name": "width", "label": "Width", "type": "number", "default": 1024, "min": 1, "max": 8192},
        {"name": "height", "label": "Height", "type": "number", "default": 1024, "min": 1, "max": 8192},
        {"name": "mode", "label": "Fit", "type": "segmented", "default": "contain",
         "options": ["contain", "cover", "stretch"]},
        {"name": "background", "label": "Padding", "type": "select", "default": "transparent",
         "options": ["transparent", "#ffffff", "#000000"]},
    ),
))
register(Processor(
    "upscale", "Upscale (Real-ESRGAN)",
    "Tiled Real-ESRGAN, capped at 4096 px on the long side. Transparency is carried across.",
    _clean_upscale, _apply_upscale, _tools_unavailable("torch", "spandrel"),
    options=({"name": "scale", "label": "Factor", "type": "segmented", "default": "2",
              "options": ["2", "4"]},),
))
register(Processor(
    "face_restore", "Restore faces (GFPGAN)",
    "Detects, restores and blends faces back. Transparency is carried across.",
    _clean_nothing, _apply_face_restore, _tools_unavailable("torch", "spandrel", "facexlib"),
))


# ---- the pipeline ------------------------------------------------------------------------------
def sanitize_steps(raw: Any) -> list[dict[str, Any]]:
    """Validated, canonical `finish_steps`: [{"processor": name, **options}, ...].

    Unknown processors and bad options are errors, not dropped: a finishing
    step that silently disappears is exactly how a set of "transparent" outputs
    comes back opaque. Availability is not checked here — a missing optional
    runtime is reported when the step runs, as a warning on that output.
    """
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list):
        raise FinishingError("finishing must be a list of steps")
    if len(raw) > MAX_STEPS:
        raise FinishingError(f"at most {MAX_STEPS} finishing steps")
    steps: list[dict[str, Any]] = []
    for step in raw:
        if not isinstance(step, dict):
            raise FinishingError("each finishing step must be an object with a 'processor'")
        name = step.get("processor")
        processor = PROCESSORS.get(name) if isinstance(name, str) else None
        if processor is None:
            raise FinishingError(f"unknown finishing processor {name!r}")
        options = {k: v for k, v in step.items() if k != "processor"}
        steps.append({"processor": processor.name, **processor.clean(options)})
    return steps


def run_steps(
    img: Image.Image, steps: list[dict[str, Any]], emit: Callable[[float, str], None],
    warnings: list[str] | None = None,
) -> tuple[Image.Image, list[dict[str, Any]]]:
    """Apply `steps` in order. Returns (image, records of the steps that ran).

    `emit(fraction, message)` reports progress across all steps. Cancel and skip
    propagate; any other failure keeps the previous image and is recorded in
    `warnings`, never in the returned records.
    """
    from .queue import CancelledJob, SkipItem

    records: list[dict[str, Any]] = []
    total = max(1, len(steps))
    for index, step in enumerate(steps):
        name = str(step.get("processor"))
        processor = PROCESSORS.get(name)
        lo, hi = index / total, (index + 1) / total
        if processor is None:
            _warn(warnings, f"Finishing step '{name}' is not known to this version and was skipped.")
            continue
        reason = processor.unavailable()
        if reason:
            _warn(warnings, f"{processor.label} was skipped: {reason}")
            emit(hi, f"{processor.label.lower()} skipped")
            continue
        options = {k: v for k, v in step.items() if k != "processor"}
        emit(lo, processor.label.lower())
        before = img.size

        def within(fraction: float, message: str, _lo: float = lo, _hi: float = hi) -> None:
            emit(_lo + (_hi - _lo) * fraction, message)

        try:
            img, detail = processor.apply(img, options, within)
        except (CancelledJob, SkipItem):
            raise
        except Exception as error:  # noqa: BLE001 — degrade to the unfinished image, never discard it
            logger.warning("finishing step %s skipped: %s", name, error)
            _warn(warnings, f"{processor.label} failed and was skipped: {error}")
            emit(hi, f"{processor.label.lower()} skipped")
            continue
        records.append({"operation": processor.name, **detail,
                        "input_size": {"width": before[0], "height": before[1]},
                        "output_size": {"width": img.width, "height": img.height}})
        emit(hi, f"{processor.label.lower()} complete")
    return img, records


def _warn(warnings: list[str] | None, message: str) -> None:
    if warnings is not None and message not in warnings:
        warnings.append(message)


def describe() -> list[dict[str, Any]]:
    """Every processor, for capability listings. Cheap: no heavy imports."""
    out = []
    for processor in PROCESSORS.values():
        reason = processor.unavailable()
        out.append({"name": processor.name, "label": processor.label,
                    "description": processor.description, "available": reason is None,
                    "unavailable_reason": reason, "options": list(processor.options)})
    return out
