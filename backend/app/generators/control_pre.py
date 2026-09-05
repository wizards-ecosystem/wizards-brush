"""Control-image preprocessors for the local ControlNet generator.

canny uses cv2 (already present via mediapipe); depth/pose need the optional
[control] extra (controlnet_aux). mode="none" passes the input through — for
users who bring a ready-made control map.
"""
from __future__ import annotations

from typing import Any

from PIL import Image

from ..config import settings
from ..model_sources import revision_for
from .postprocess import ToolUnavailable

MODES = ["canny", "depth", "pose", "none"]

# Standard deviation below which a control map carries no usable signal. A truly
# empty map is exactly 0; the small margin covers a handful of stray pixels.
_BLANK_STD = 1.0

_BLANK_HELP = {
    "pose": "no person was detected, so the pose map is empty. Pose control needs "
            "a photo with a visible human figure — try canny or depth for a scene.",
    "canny": "no edges were found. The image may be blank, or very low contrast.",
    "depth": "the depth map came out uniform, which usually means a flat or "
             "featureless input.",
}


def preprocess(img: Image.Image, mode: str) -> Image.Image:
    """Control map for `mode`. Raises rather than returning an empty map.

    An empty map is the quiet failure this guards: ControlNet accepts it happily
    and contributes nothing, so the user gets an ordinary unguided generation
    and no reason why. Pose on a landscape hits this every time — OpenPose finds
    no person and returns pure black.
    """
    img = img.convert("RGB")
    if mode == "none":
        return img          # a ready-made map; not ours to second-guess
    if mode == "canny":
        out = _canny(img)
    elif mode in ("depth", "pose"):
        out = _aux(img, mode)
    else:
        raise ToolUnavailable(f"unknown control mode '{mode}'")
    _reject_if_blank(out, mode)
    return out


def _reject_if_blank(img: Image.Image, mode: str) -> None:
    try:
        from PIL import ImageStat

        std = max(ImageStat.Stat(img.convert("L")).stddev)
    except Exception:  # noqa: BLE001 — a failed check must not block a good map
        return
    if std < _BLANK_STD:
        raise ToolUnavailable(
            f"{mode} produced an empty control map: "
            f"{_BLANK_HELP.get(mode, 'the input carried no usable signal.')}"
        )


def _canny(img: Image.Image) -> Image.Image:
    try:
        import cv2
        import numpy as np
    except Exception as e:
        raise ToolUnavailable("canny needs opencv (installed with the 'detailer' extra)") from e
    arr = cv2.Canny(np.asarray(img), 100, 200)
    return Image.fromarray(arr).convert("RGB")


_AUX: dict[str, Any] = {"depth": None, "pose": None}


def _aux(img: Image.Image, mode: str) -> Image.Image:
    try:
        # Probe the exact classes used below — probing a different class would
        # pass on a controlnet_aux version that then fails at load time.
        from controlnet_aux import MidasDetector, OpenposeDetector
    except Exception as e:
        raise ToolUnavailable(
            f"{mode} preprocessing needs the 'control' extra (controlnet_aux). "
            "Re-run `make setup` to install project-local optional processors."
        ) from e
    if _AUX[mode] is None:
        cls = MidasDetector if mode == "depth" else OpenposeDetector
        _AUX[mode] = cls.from_pretrained(
            "lllyasviel/Annotators", cache_dir=str(settings.hf_hub_path),
            revision=revision_for("lllyasviel/Annotators"),
        )
    return _AUX[mode](img).convert("RGB")
