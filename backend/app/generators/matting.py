"""Background removal: a real alpha channel from a segmentation model.

**Model.** BiRefNet-lite (Zheng et al., "Bilateral Reference for
High-Resolution Dichotomous Image Segmentation"), as the ONNX export published
at ``onnx-community/BiRefNet_lite-ONNX``. Licence verified at each layer on
2026-09-18 rather than taken from a wrapper: the upstream code repository
(github.com/ZhengPeng7/BiRefNet) carries an MIT LICENSE, the author's weights
(``ZhengPeng7/BiRefNet_lite``) are tagged MIT, and the ONNX conversion is tagged
MIT and names that repository as its base model. BRIA's RMBG models are
deliberately not used: their weights are non-commercial, which this Apache-2.0
project cannot depend on. See docs/model-licenses.md.

**Why ONNX.** An ONNX graph is data, not code: loading it executes nothing the
publisher wrote, unlike the model's ``trust_remote_code`` Python or a pickle.
onnxruntime is already the project's optional runtime for RIFE, so this adds a
weight, not a dependency.

**Integrity and containment.** The file is fetched once, on first use, from a
pinned commit into ``WEIGHTS_DIR`` and refused unless its SHA-256 matches — the
same path the upscaler and RIFE weights take.

**Output.** The source pixels are left exactly as they were and the predicted
matte becomes their alpha channel, at the source's own dimensions. Nothing is
composited onto a checkerboard or a colour: transparency is transparency. An
existing alpha channel is respected by multiplying it with the matte, so a
second pass can only remove more, never restore what an earlier step removed.

numpy and onnxruntime are imported inside the functions that need them, like
every other heavy import here.
"""
from __future__ import annotations

import contextlib
import importlib.util
from contextlib import AbstractContextManager
from typing import Any

from PIL import Image, ImageChops

from .. import log

logger = log.get("matting")

MODEL_REPO = "onnx-community/BiRefNet_lite-ONNX"
MODEL_REVISION = "de15b22ba131738a16dff04aab8bdf8dc32e3ac1"
MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/onnx/model.onnx"
MODEL_SHA256 = "5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333"
MODEL_FILE = "birefnet-lite-general.onnx"
MODEL_ID = f"{MODEL_REPO}@{MODEL_REVISION[:12]}"
LICENSE = "MIT"

# The export's preprocessor_config: bilinear resize to 1024x1024, scale to
# [0, 1], then ImageNet mean/std.
INPUT_SIZE = 1024
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)

_SESSION: dict[str, Any] = {"session": None}


def unavailable_reason() -> str | None:
    """None when background removal can run here, otherwise why not.

    Answered from the installed-package index without importing anything, so a
    capability listing never pulls numpy into a request.
    """
    for module in ("onnxruntime", "numpy"):
        if importlib.util.find_spec(module) is None:
            return (f"Background removal needs {module}, installed with the optional "
                    "processors by `make setup`.")
    return None


def _session():
    if _SESSION["session"] is not None:
        return _SESSION["session"]
    import onnxruntime as ort

    from .postprocess import cpu_tool_threads, download_weight

    weight = download_weight(MODEL_URL, MODEL_FILE, MODEL_SHA256)
    options = ort.SessionOptions()
    # The CPU path must not take the whole machine; see postprocess._cpu_thread_cap.
    options.intra_op_num_threads = cpu_tool_threads()
    options.inter_op_num_threads = 1
    providers = ["CPUExecutionProvider"]
    if "CUDAExecutionProvider" in ort.get_available_providers():
        providers.insert(0, "CUDAExecutionProvider")
    session = ort.InferenceSession(str(weight), sess_options=options, providers=providers)
    _SESSION["session"] = session
    logger.info("BiRefNet-lite (onnx) loaded [%s]", session.get_providers()[0])
    return session


def predict_matte(img: Image.Image) -> Image.Image:
    """The model's foreground matte for `img`, as an "L" image of the same size."""
    import numpy as np

    session = _session()
    rgb = img.convert("RGB")
    resized = rgb.resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BILINEAR)
    pixels = np.asarray(resized, dtype=np.float32) / np.float32(255.0)
    mean = np.asarray(MEAN, dtype=np.float32)
    std = np.asarray(STD, dtype=np.float32)
    batch = ((pixels - mean) / std).transpose(2, 0, 1)[None].astype(np.float32)

    lock: AbstractContextManager = contextlib.nullcontext()
    if any("CPU" not in provider for provider in session.get_providers()):
        from .base import GPU_LOCK  # a GPU provider shares the card with generation

        lock = GPU_LOCK
    with lock:
        logits = session.run(None, {session.get_inputs()[0].name: batch})[0]
    logits = np.asarray(logits, dtype=np.float32).reshape(INPUT_SIZE, INPUT_SIZE)
    matte = 1.0 / (1.0 + np.exp(-logits))
    small = Image.fromarray((matte * 255.0).round().clip(0, 255).astype("uint8"), "L")
    return small.resize(rgb.size, Image.Resampling.BILINEAR)


def apply_matte(img: Image.Image, matte: Image.Image) -> Image.Image:
    """`img` with `matte` as its alpha: same pixels, same size, real transparency."""
    if matte.size != img.size:
        raise ValueError(f"matte is {matte.size[0]}x{matte.size[1]}, image is "
                         f"{img.size[0]}x{img.size[1]}")
    rgba = img.convert("RGBA")
    existing = rgba.getchannel("A")
    rgba.putalpha(ImageChops.multiply(existing, matte.convert("L")))
    return rgba


def remove_background(img: Image.Image) -> tuple[Image.Image, dict[str, Any]]:
    """(RGBA image, provenance record) for one finished image."""
    reason = unavailable_reason()
    if reason:
        raise RuntimeError(reason)
    out = apply_matte(img, predict_matte(img))
    alpha = out.getchannel("A")
    transparent = sum(alpha.histogram()[:9]) / max(1, out.width * out.height)
    return out, {
        "model": MODEL_ID,
        "model_sha256": MODEL_SHA256,
        "license": LICENSE,
        "transparent_fraction": round(transparent, 4),
    }
