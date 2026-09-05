"""ADetailer-style auto-detailer: detect faces/hands, then re-render each region
with a masked inpaint pass at low denoise.

Detection uses mediapipe (Apache-2.0, CPU, tiny) via the optional [detailer]
extra; the detector is isolated behind detect_regions() so it can be swapped
(e.g. for a YOLO face model) without touching the refine pipeline. The refine
pass reuses the local inpaint pipeline, so it serializes under GPU_LOCK like
every other local GPU op.
"""
from __future__ import annotations

from collections.abc import Callable

from PIL import Image, ImageDraw, ImageFilter

from .. import log
from .postprocess import ToolUnavailable

logger = log.get("detailer")

Box = tuple[int, int, int, int]  # x0, y0, x1, y1 in pixels

_MIN_CONFIDENCE = 0.5
_DILATE = 0.30          # grow each detection box by 30% per side

# Size limits as a FRACTION of the image, not in pixels.
#
# "Too small to be worth detailing" is a relative judgement. An absolute pixel
# threshold silently changes meaning the moment the user changes resolution: a
# 64px face is background clutter in a 2048px render and the whole subject in a
# 256px one. Filtering by fraction keeps the intent stable across every tier.
# 5% of a side is roughly 50px at a 1024px output, which is about where a detail
# pass stops having enough signal to improve anything: the region is cropped,
# upscaled and re-diffused, and below that there is nothing to refine.
MIN_REGION_FRACTION = 0.05
MAX_REGION_FRACTION = 0.90   # larger than 90%: this is the whole image
_CROP_MIN = 512         # never inpaint a crop smaller than this
_CROP_RENDER = 768      # upscale the crop to this size for the refine pass
_FEATHER_PX = 24        # gaussian feather on the paste mask


def detect_regions(img: Image.Image, targets: tuple[str, ...] = ("face",)) -> list[Box]:
    """Pixel boxes for the requested targets ('face', 'hand'), confidence-filtered."""
    try:
        import mediapipe as mp
        import numpy as np
    except Exception as e:
        raise ToolUnavailable(
            "Auto-detail needs the 'detailer' extra (mediapipe). "
            "Re-run `make setup` to install project-local optional processors."
        ) from e

    rgb = np.asarray(img.convert("RGB"))
    h, w = rgb.shape[:2]
    boxes: list[Box] = []

    if "face" in targets:
        with mp.solutions.face_detection.FaceDetection(
                model_selection=1, min_detection_confidence=_MIN_CONFIDENCE) as det:
            res = det.process(rgb)
            for d in res.detections or []:
                bb = d.location_data.relative_bounding_box
                boxes.append((int(bb.xmin * w), int(bb.ymin * h),
                              int((bb.xmin + bb.width) * w), int((bb.ymin + bb.height) * h)))

    if "hand" in targets:
        with mp.solutions.hands.Hands(
                static_image_mode=True, max_num_hands=4,
                min_detection_confidence=_MIN_CONFIDENCE) as det:
            res = det.process(rgb)
            for lm in res.multi_hand_landmarks or []:
                xs = [p.x for p in lm.landmark]
                ys = [p.y for p in lm.landmark]
                boxes.append((int(min(xs) * w), int(min(ys) * h),
                              int(max(xs) * w), int(max(ys) * h)))

    return _filter_by_size([_clamp_box(b, w, h) for b in boxes], w, h)


def _filter_by_size(boxes: list[Box], w: int, h: int) -> list[Box]:
    """Drop detections that are too small or too large to be worth a pass.

    Sorted largest-first so that when `max_regions` truncates the list, the
    regions that survive are the ones most visible in the output.
    """
    kept: list[Box] = []
    for b in boxes:
        fw, fh = (b[2] - b[0]) / max(1, w), (b[3] - b[1]) / max(1, h)
        if fw < MIN_REGION_FRACTION or fh < MIN_REGION_FRACTION:
            continue
        if fw > MAX_REGION_FRACTION and fh > MAX_REGION_FRACTION:
            continue
        kept.append(b)
    if len(kept) != len(boxes):
        logger.debug("detailer: %d region(s) detected, %d worth refining",
                     len(boxes), len(kept))
    return sorted(kept, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)


def _clamp_box(b: Box, w: int, h: int) -> Box:
    x0, y0, x1, y1 = b
    return max(0, x0), max(0, y0), min(w, x1), min(h, y1)


def _expand_box(b: Box, w: int, h: int) -> Box:
    """Dilate by _DILATE per side, enforce a square >= _CROP_MIN, snap to /16."""
    x0, y0, x1, y1 = b
    bw, bh = x1 - x0, y1 - y0
    cx, cy = x0 + bw / 2, y0 + bh / 2
    side = max(bw, bh) * (1 + 2 * _DILATE)
    side = max(side, _CROP_MIN)
    side = min(side, min(w, h))  # can't crop beyond the image
    side = max(16, (int(side) // 16) * 16)  # floor — rounding UP past min(w,h) would zero-pad the crop
    x0 = int(min(max(0, cx - side / 2), w - side))
    y0 = int(min(max(0, cy - side / 2), h - side))
    return x0, y0, x0 + side, y0 + side


def refine(
    img: Image.Image, *, prompt: str = "", negative: str = "", denoise: float = 0.35,
    targets: tuple[str, ...] = ("face",), max_regions: int = 4, seed: int = 0,
    model: str | None = None, steps: int | None = None, guidance: float | None = None,
    metrics: dict[str, float] | None = None,
    progress_cb: Callable[..., None] | None = None,
) -> tuple[Image.Image, int]:
    """Detect regions and re-render each with a masked low-denoise inpaint pass.
    Returns (result image, regions refined)."""
    from . import local_image, variants

    recipe = variants.by_repo(model or "")
    steps = max(1, int(steps if steps is not None else recipe.refine_steps if recipe else 12))
    guidance = float(guidance if guidance is not None else recipe.guidance if recipe else 1.0)

    boxes = detect_regions(img, targets)[:max_regions]
    if not boxes:
        return img, 0

    out = img.convert("RGB").copy()
    w, h = out.size
    region_prompt = ", ".join(x for x in ("highly detailed face", prompt) if x)

    for n, box in enumerate(boxes):
        if progress_cb:
            progress_cb(n / len(boxes), f"detailing region {n + 1}/{len(boxes)}")
        x0, y0, x1, y1 = _expand_box(box, w, h)
        side = x1 - x0
        crop = out.crop((x0, y0, x1, y1)).resize((_CROP_RENDER, _CROP_RENDER),
                                                 Image.Resampling.LANCZOS)
        # Full-white mask: the whole (already tight) crop is re-rendered at low
        # denoise; the feather below blends it back seamlessly.
        mask = Image.new("L", (_CROP_RENDER, _CROP_RENDER), 255)
        refined = local_image.generate(
            mode="inpaint", prompt=region_prompt, negative_prompt=negative,
            steps=steps, guidance=guidance, seed=seed + n,
            width=_CROP_RENDER, height=_CROP_RENDER,
            image=crop, mask=mask, strength=max(0.1, min(denoise, 0.9)),
            model=model, progress_cb=None,
            metrics=metrics,
        )
        refined = refined.resize((side, side), Image.Resampling.LANCZOS)
        # Feathered paste: soft edges so the refined square doesn't show seams.
        paste_mask = Image.new("L", (side, side), 0)
        d = ImageDraw.Draw(paste_mask)
        pad = _FEATHER_PX
        d.rectangle((pad, pad, side - pad, side - pad), fill=255)
        # Sigma must decay before the crop boundary. sigma=pad left ~22% of the
        # refined square visible at the edge; pad/3 is below 1% there.
        paste_mask = paste_mask.filter(ImageFilter.GaussianBlur(_FEATHER_PX / 3))
        out.paste(refined, (x0, y0), paste_mask)

    if progress_cb:
        progress_cb(1.0, "detail regions complete")

    logger.info("refined %d region(s)", len(boxes))
    return out, len(boxes)
