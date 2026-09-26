"""Face identity: OpenCV SFace embeddings with YuNet detection.

Apache-2.0 weights from opencv_zoo, fetched on first use at pinned SHA-256.
InsightFace buffalo_l is not used (research-only weights). CLIP is the wrong
metric for a face. numpy/onnxruntime are imported inside the functions that
need them.

Detection prefers YuNet when OpenCV can construct FaceDetectorYN; otherwise the
SCHP face class supplies a box so the validator still has a face crop.
"""
from __future__ import annotations

import importlib.util
from collections.abc import Callable
from typing import Any

from PIL import Image, ImageOps

from .. import log

logger = log.get("identity")

SFACE_URL = ("https://huggingface.co/opencv/face_recognition_sface/resolve/"
             "3d7082438a6e4551e840c9b2bb60b71e8da4b524/face_recognition_sface_2021dec.onnx")
SFACE_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
SFACE_FILE = "face_recognition_sface_2021dec.onnx"
SFACE_ID = "opencv/face_recognition_sface@2021dec"

YUNET_URL = ("https://huggingface.co/opencv/face_detection_yunet/resolve/"
             "main/face_detection_yunet_2023mar.onnx")
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
YUNET_FILE = "face_detection_yunet_2023mar.onnx"
YUNET_ID = "opencv/face_detection_yunet@2023mar"

LICENSE = "Apache-2.0"
SFACE_SIZE = 112

_SESSIONS: dict[str, Any] = {"sface": None, "yunet": None}
# Tests inject (source, output) -> score without downloading weights.
_COMPARE: Callable[[Image.Image, Image.Image], float | None] | None = None


def unavailable_reason() -> str | None:
    if _COMPARE is not None:
        return None
    for module in ("onnxruntime", "numpy"):
        if importlib.util.find_spec(module) is None:
            return (f"Face identity needs {module}, installed with the optional "
                    "processors by `make setup`.")
    return None


def _sface():
    if _SESSIONS["sface"] is not None:
        return _SESSIONS["sface"]
    import onnxruntime as ort

    from .postprocess import cpu_tool_threads, download_weight

    path = download_weight(SFACE_URL, SFACE_FILE, SFACE_SHA256)
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = cpu_tool_threads()
    sess = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
    _SESSIONS["sface"] = sess
    return sess


def _yunet_path():
    from .postprocess import download_weight

    return str(download_weight(YUNET_URL, YUNET_FILE, YUNET_SHA256))


def _face_box(img: Image.Image) -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of the strongest face, or None."""
    rgb = ImageOps.exif_transpose(img).convert("RGB")
    try:
        import cv2
        import numpy as np

        path = _yunet_path()
        detector = cv2.FaceDetectorYN.create(path, "", (rgb.width, rgb.height))
        detector.setInputSize((rgb.width, rgb.height))
        _retval, faces = detector.detect(np.asarray(rgb)[:, :, ::-1])
        if faces is not None and len(faces):
            x, y, w, h = [float(v) for v in faces[0][:4]]
            left, top = max(0, int(x)), max(0, int(y))
            return (left, top,
                    min(rgb.width, left + max(1, int(w))),
                    min(rgb.height, top + max(1, int(h))))
    except Exception:
        logger.debug("YuNet unavailable; falling back to SCHP face box", exc_info=True)
    try:
        from . import parsing

        labels = parsing.predict_atr(rgb)
        face = labels.point(lambda value: 255 if value == parsing.ATR_INDEX["face"] else 0)
        return face.getbbox()
    except Exception:  # noqa: BLE001
        return None


def _embed(img: Image.Image, box: tuple[int, int, int, int]):
    import numpy as np

    crop = ImageOps.exif_transpose(img).convert("RGB").crop(box)
    if min(crop.size) < 8:
        return None
    face = crop.resize((SFACE_SIZE, SFACE_SIZE), Image.Resampling.BILINEAR)
    arr = np.asarray(face, dtype=np.float32)
    arr = (arr - 127.5) / 128.0
    batch = np.transpose(arr, (2, 0, 1))[None]
    sess = _sface()
    name = sess.get_inputs()[0].name
    feat = np.asarray(sess.run(None, {name: batch})[0], dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(feat))
    if norm <= 0:
        return None
    return feat / norm


def compare(source: Image.Image, output: Image.Image) -> float | None:
    """Cosine similarity in [0, 1] between the primary faces, or None if missing."""
    if _COMPARE is not None:
        return _COMPARE(source, output)
    if reason := unavailable_reason():
        raise RuntimeError(reason)
    src_box = _face_box(source)
    out_box = _face_box(output)
    if src_box is None or out_box is None:
        return None
    a, b = _embed(source, src_box), _embed(output, out_box)
    if a is None or b is None:
        return None
    import numpy as np

    return float(np.clip(np.dot(a, b), 0.0, 1.0))
