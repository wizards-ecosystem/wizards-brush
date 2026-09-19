"""Human and face parsing for region masks: SCHP ATR-18 plus BiSeNet.

ONNX graphs only — publisher Python is never imported. Weights are fetched on
first use at a pinned commit/SHA, the same containment BiRefNet uses.

SCHP (MIT) labels hair, face, upper-clothes and the rest of ATR-18. BiSeNet
(MIT, CelebAMask-HQ) adds mouth and eyes, which ATR does not split out.
"""
from __future__ import annotations

import importlib.util
from collections.abc import Callable
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageOps

from .. import log

logger = log.get("parsing")

# ATR-18 class order (GoGoDuck912 SCHP / pirocheto export).
ATR_CLASSES = (
    "background", "hat", "hair", "sunglasses", "upper-clothes", "skirt",
    "pants", "dress", "belt", "left-shoe", "right-shoe", "face",
    "left-leg", "right-leg", "left-arm", "right-arm", "bag", "scarf",
)
ATR_INDEX = {name: i for i, name in enumerate(ATR_CLASSES)}

# CelebAMask-HQ / zllrunning BiSeNet class order.
FACE_CLASSES = (
    "background", "skin", "l_brow", "r_brow", "l_eye", "r_eye", "eye_g",
    "l_ear", "r_ear", "ear_r", "nose", "mouth", "u_lip", "l_lip", "neck",
    "neck_l", "cloth", "hair", "hat",
)
FACE_INDEX = {name: i for i, name in enumerate(FACE_CLASSES)}

SCHP_REPO = "pirocheto/schp-atr-18"
SCHP_REVISION = "61f4ff3c508b899904e495cefbbaff9cacadeb45"
SCHP_URL = (f"https://huggingface.co/{SCHP_REPO}/resolve/{SCHP_REVISION}/"
            "onnx/schp-atr-18-int8-static.onnx")
SCHP_SHA256 = "4420d8db8c1f266967c89485786b01209f6d405f320fc0f87e8ced49392cefb5"
SCHP_FILE = "schp-atr-18-int8-static.onnx"
SCHP_ID = f"{SCHP_REPO}@{SCHP_REVISION[:12]}"
SCHP_LICENSE = "MIT"
SCHP_SIZE = 512
# preprocessor_config.json on that revision: BGR ImageNet (OpenCV channel order).
SCHP_MEAN = (0.406, 0.456, 0.485)
SCHP_STD = (0.225, 0.224, 0.229)

BISENET_URL = "https://github.com/yakhyo/face-parsing/releases/download/weights/resnet18.onnx"
BISENET_SHA256 = "0d9bd318e46987c3bdbfacae9e2c0f461cae1c6ac6ea6d43bbe541a91727e33f"
BISENET_FILE = "bisenet-face-resnet18.onnx"
BISENET_ID = "yakhyo/face-parsing@weights/resnet18"
BISENET_LICENSE = "MIT"
BISENET_SIZE = 512
BISENET_MEAN = (0.485, 0.456, 0.406)
BISENET_STD = (0.229, 0.224, 0.225)

# Public region names the mask tool and Refine router share.
REGIONS = (
    "hair", "face", "upper-clothes", "clothes", "mouth", "eyes",
    "subject_minus_face",
)

_SESSIONS: dict[str, Any] = {"schp": None, "bisenet": None}
# Tests inject a (kind, labels_image) predictor and never download.
_PREDICT: Callable[[str, Image.Image], Image.Image] | None = None


def unavailable_reason() -> str | None:
    if _PREDICT is not None:
        return None
    for module in ("onnxruntime", "numpy"):
        if importlib.util.find_spec(module) is None:
            return (f"Region masks need {module}, installed with the optional "
                    "processors by `make setup`.")
    return None


def _session(kind: str):
    if _SESSIONS[kind] is not None:
        return _SESSIONS[kind]
    import onnxruntime as ort

    from .postprocess import cpu_tool_threads, download_weight

    if kind == "schp":
        path = download_weight(SCHP_URL, SCHP_FILE, SCHP_SHA256)
    else:
        path = download_weight(BISENET_URL, BISENET_FILE, BISENET_SHA256)
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = cpu_tool_threads()
    sess = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
    _SESSIONS[kind] = sess
    return sess


def _nchw(img: Image.Image, size: int, mean: tuple[float, float, float],
          std: tuple[float, float, float], *, bgr: bool) -> Any:
    import numpy as np

    rgb = ImageOps.exif_transpose(img).convert("RGB").resize((size, size), Image.Resampling.BILINEAR)
    arr = np.asarray(rgb, dtype=np.float32) / 255.0
    if bgr:
        arr = arr[:, :, ::-1]
    arr = (arr - mean) / std
    return np.transpose(arr, (2, 0, 1))[None]


def _labels_from_logits(logits, size: tuple[int, int]) -> Image.Image:
    import numpy as np

    raw = np.asarray(logits)
    if raw.ndim == 4:
        raw = raw[0]
    if raw.shape[0] < raw.shape[-1]:
        ids = raw.argmax(axis=0)
    else:
        ids = raw.argmax(axis=-1)
    small = Image.fromarray(ids.astype("uint8"), "L")
    return small.resize(size, Image.Resampling.NEAREST)


def predict_atr(img: Image.Image) -> Image.Image:
    """Per-pixel ATR-18 class ids at the source size."""
    if _PREDICT is not None:
        return _PREDICT("schp", img)
    if reason := unavailable_reason():
        raise RuntimeError(reason)
    sess = _session("schp")
    batch = _nchw(img, SCHP_SIZE, SCHP_MEAN, SCHP_STD, bgr=True)
    name = sess.get_inputs()[0].name
    logits = sess.run(None, {name: batch})[0]
    return _labels_from_logits(logits, img.size)


def predict_face(img: Image.Image) -> Image.Image:
    """Per-pixel CelebAMask-HQ class ids at the source size."""
    if _PREDICT is not None:
        return _PREDICT("bisenet", img)
    if reason := unavailable_reason():
        raise RuntimeError(reason)
    sess = _session("bisenet")
    batch = _nchw(img, BISENET_SIZE, BISENET_MEAN, BISENET_STD, bgr=False)
    name = sess.get_inputs()[0].name
    logits = sess.run(None, {name: batch})[0]
    return _labels_from_logits(logits, img.size)


def _binary(labels: Image.Image, ids: set[int]) -> Image.Image:
    return labels.point(lambda value: 255 if value in ids else 0)


def _dilate(mask: Image.Image, pixels: int) -> Image.Image:
    pixels = max(0, min(int(pixels), 128))
    if not pixels:
        return mask
    return mask.filter(ImageFilter.MaxFilter(2 * pixels + 1))


def region_at(img: Image.Image, x: int, y: int) -> str:
    """ATR class name under a click, mapped onto the public region vocabulary."""
    labels = predict_atr(img)
    x = max(0, min(int(x), labels.width - 1))
    y = max(0, min(int(y), labels.height - 1))
    idx = int(labels.getpixel((x, y)))  # type: ignore[arg-type]
    name = ATR_CLASSES[idx] if 0 <= idx < len(ATR_CLASSES) else "background"
    if name in {"skirt", "pants", "dress", "coat", "belt", "scarf", "bag"}:
        return "clothes"
    if name == "upper-clothes":
        return "upper-clothes"
    if name in {"hair", "hat"}:
        return "hair"
    if name == "face":
        return "face"
    return name


def mask_for(img: Image.Image, region: str, *, grow: int = 4) -> Image.Image:
    """White = change. `region` is one of REGIONS (or an ATR class name)."""
    wanted = str(region or "").strip().lower().replace(" ", "-")
    if wanted not in REGIONS and wanted not in ATR_INDEX and wanted not in {"eyes", "mouth"}:
        raise ValueError(f"unknown region {region!r}; expected one of {', '.join(REGIONS)}")
    rgb = ImageOps.exif_transpose(img).convert("RGB")
    if wanted in {"mouth", "eyes"}:
        face = predict_face(rgb)
        ids = ({FACE_INDEX["mouth"], FACE_INDEX["u_lip"], FACE_INDEX["l_lip"]} if wanted == "mouth"
               else {FACE_INDEX["l_eye"], FACE_INDEX["r_eye"]})
        mask = _binary(face, ids)
        return _dilate(mask, grow)
    atr = predict_atr(rgb)
    if wanted == "subject_minus_face":
        subject = atr.point(lambda value: 255 if value != ATR_INDEX["background"] else 0)
        face = _dilate(_binary(atr, {ATR_INDEX["face"]}), max(grow, 8))
        return ImageChops.subtract(subject, face)
    if wanted == "clothes":
        ids = {ATR_INDEX[name] for name in (
            "upper-clothes", "skirt", "pants", "dress", "belt", "scarf", "bag")}
        return _dilate(_binary(atr, ids), grow)
    if wanted == "hair":
        return _dilate(_binary(atr, {ATR_INDEX["hair"], ATR_INDEX["hat"]}), grow)
    idx = ATR_INDEX.get(wanted)
    if idx is None:
        raise ValueError(f"unknown region {region!r}")
    return _dilate(_binary(atr, {idx}), grow)


def protect_face_mask(img: Image.Image, *, grow: int = 8) -> Image.Image:
    """Inpaint mask that locks the face: white = subject except a dilated face."""
    return mask_for(img, "subject_minus_face", grow=grow)
