"""Post-processing tools: upscale, face restore, video frame interpolation.

These rely on optional deps (spandrel / facexlib / onnxruntime). Each entry
point imports lazily and raises `ToolUnavailable` with an actionable message so
a missing tool degrades to a clean error instead of crashing the app. Model
weights are fetched on first use into `models/weights/`.

numpy is imported inside each function (not at module level): this module is
imported by torch-free code paths (apply_image_post, the test suite), which must
stay importable without the imaging stack installed.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    import numpy as np

from .. import log
from ..config import settings

logger = log.get("postprocess")

WEIGHTS = settings.weights_path
REALESRGAN_X4 = ("https://github.com/xinntao/Real-ESRGAN/releases/download/"
                 "v0.1.0/RealESRGAN_x4plus.pth")
REALESRGAN_X4_SHA256 = "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1"
GFPGAN_V14 = ("https://github.com/TencentARC/GFPGAN/releases/download/"
              "v1.3.0/GFPGANv1.4.pth")
GFPGAN_V14_NAME = "GFPGANv1.4.pth"
GFPGAN_V14_SHA256 = "e2cd4703ab14f4d01fd1383a8a8b266f9a5833dacee8e6a79d3bf21a1b6be5ad"


class ToolUnavailable(RuntimeError):
    pass


# Leave the machine usable while a CPU-bound tool runs.
#
# torch defaults to one thread per core. That is the right answer for a batch
# job and the wrong one for a desktop app: a CPU-fallback upscale took every
# core, and the whole machine — compositor included — stalled until it finished.
# Capping at half the cores (at least one, never more than four) costs a little
# throughput on work that is already the slow path, and keeps the UI responsive.
# On CUDA this is nearly free: the cap only binds the handful of ops that stay
# on the host.
_CPU_TOOL_THREADS = max(1, min(4, (os.cpu_count() or 4) // 2))


@contextlib.contextmanager
def _cpu_thread_cap():
    """Run a block with torch's CPU thread pool capped, then restore it.

    Restoring matters: the same process runs the local diffusion pipeline, which
    does want the full machine, and torch's thread count is global.
    """
    import torch

    previous = torch.get_num_threads()
    try:
        torch.set_num_threads(_CPU_TOOL_THREADS)
    except Exception:  # noqa: BLE001 — an unsettable pool is not worth failing over
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            torch.set_num_threads(previous)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, name: str, expected_sha256: str) -> Path:
    WEIGHTS.mkdir(parents=True, exist_ok=True)
    dest = WEIGHTS / name
    if dest.exists() and _sha256(dest) != expected_sha256:
        raise ToolUnavailable(
            f"{name} failed its SHA-256 check; remove the cached file and try again"
        )
    if not dest.exists():
        logger.info("downloading %s …", name)
        with tempfile.NamedTemporaryFile(
            prefix=f".{name}.", suffix=".part", dir=WEIGHTS, delete=False
        ) as handle:
            partial = Path(handle.name)
        try:
            urllib.request.urlretrieve(url, partial)
            if _sha256(partial) != expected_sha256:
                raise ToolUnavailable(f"downloaded {name} failed its SHA-256 check")
            os.replace(partial, dest)
        finally:
            partial.unlink(missing_ok=True)
    return dest


# ---- upscaling (spandrel + Real-ESRGAN) -----------------------------------
_UPSCALER = {"model": None}


def _load_upscaler():
    if _UPSCALER["model"] is not None:
        return _UPSCALER["model"]
    try:
        from spandrel import ModelLoader
    except Exception as e:
        raise ToolUnavailable("Upscaling needs spandrel (a core dep). Re-run `make setup`.") from e
    weight = _download(REALESRGAN_X4, "RealESRGAN_x4plus.pth", REALESRGAN_X4_SHA256)
    model = ModelLoader().load_from_file(str(weight)).eval()
    # Stay on CPU here. Device placement happens under GPU_LOCK in
    # upscale_image; moving weights during this unlocked loader could overlap a
    # diffusion kernel running on the same card.
    _UPSCALER["model"] = model
    return model


# Side length of one upscale tile, and how much neighbouring tiles overlap.
#
# Real-ESRGAN is fully convolutional, so a tile is exactly as valid as the whole
# image provided it carries enough context for the receptive field. 512 keeps
# the peak working set at roughly a 2048x2048 output no matter how big the input
# is; 32px of padding is well past the network's receptive field, so the tile
# interiors already agree and the padding is simply cropped off rather than
# blended.
_TILE = 512
_TILE_OVERLAP = 32

# A finishing tool should not turn one ordinary render into an unbounded file.
# 4096² is already 16.8 MP / roughly 50 MB decoded RGB, large enough for print
# and display while keeping hashing, thumbnailing and browser decode tractable.
MAX_UPSCALE_SIDE = 4096
MAX_UPSCALE_TILES = 64


# A 512 tile at x4 produces 2048x2048 of 64-channel activations. Measured peak
# is a few hundred MB; 1.5 GB is that with room for fragmentation and the copy.
_TILE_VRAM_BYTES = 1_500_000_000


def _has_vram_for_tile() -> bool:
    """Whether the GPU has room for one upscale tile right now.

    `mem_get_info` reports what is genuinely free on the device, which is the
    question — `memory_reserved` would only describe this process's allocator and
    would happily report room that the resident pipeline is sitting on.
    """
    import torch

    try:
        free, _total = torch.cuda.mem_get_info()
        return int(free) >= _TILE_VRAM_BYTES
    except Exception:  # noqa: BLE001 — an unanswerable probe means "use the GPU"
        return True


def _tiles(width: int, height: int):
    """Yield (padded box, interior box) pairs covering the whole image."""
    for y in range(0, height, _TILE):
        for x in range(0, width, _TILE):
            interior = (x, y, min(width, x + _TILE), min(height, y + _TILE))
            padded = (
                max(0, x - _TILE_OVERLAP),
                max(0, y - _TILE_OVERLAP),
                min(width, interior[2] + _TILE_OVERLAP),
                min(height, interior[3] + _TILE_OVERLAP),
            )
            yield padded, interior


def upscale_target_size(width: int, height: int, scale: int) -> tuple[int, int]:
    """Requested output size, proportionally clamped to the documented ceiling."""
    width, height, scale = int(width), int(height), int(scale)
    if width <= 0 or height <= 0:
        raise ToolUnavailable("cannot upscale an image with empty dimensions")
    if max(width, height) >= MAX_UPSCALE_SIDE:
        raise ToolUnavailable(
            f"This image is already {max(width, height)} px on its long side; "
            f"the upscale ceiling is {MAX_UPSCALE_SIDE} px."
        )
    requested = (width * scale, height * scale)
    if max(requested) <= MAX_UPSCALE_SIDE:
        return requested
    ratio = MAX_UPSCALE_SIDE / max(requested)
    return max(1, round(requested[0] * ratio)), max(1, round(requested[1] * ratio))


def _blend_mask(size: tuple[int, int], left: int, top: int) -> Image.Image:
    """Opacity ramp for overlap with tiles already pasted to the left/above."""
    from PIL import ImageChops

    width, height = size
    mask = Image.new("L", size, 255)
    if left > 0:
        left = min(left, width)
        ramp = bytes(round(255 * i / max(1, left - 1)) for i in range(left))
        horizontal = Image.frombytes("L", (left, 1), ramp).resize((left, height))
        mask.paste(horizontal, (0, 0))
    if top > 0:
        top = min(top, height)
        ramp = bytes(round(255 * i / max(1, top - 1)) for i in range(top))
        vertical = Image.frombytes("L", (1, top), ramp).resize((width, top))
        top_mask = Image.new("L", size, 255)
        top_mask.paste(vertical, (0, 0))
        mask = ImageChops.multiply(mask, top_mask)
    return mask


def upscale_image(
    img: Image.Image, scale: int = 4,
    progress_cb: Callable[[float, str], None] | None = None,
) -> Image.Image:
    """Upscale ×4 with Real-ESRGAN, then resample to the requested scale.

    Tiled, rather than one forward pass over the whole image. The single-pass
    version allocated activations proportional to the *output* area: upscaling
    1024x1024 meant 64-channel feature maps at 4096x4096, gigabytes of them at
    once. On the GPU that fought the resident pipeline for VRAM; on the CPU
    fallback it saturated every core and enough RAM to stall the desktop for
    seconds. Tiling caps the working set at a constant, so upscaling gets slower
    with image size instead of falling off a cliff at it.
    """
    import numpy as np
    import torch

    from .base import GPU_LOCK

    target_size = upscale_target_size(img.width, img.height, scale)
    tiles = list(_tiles(img.width, img.height))
    if len(tiles) > MAX_UPSCALE_TILES:
        raise ToolUnavailable(
            f"This upscale needs {len(tiles)} tiles; the safe limit is {MAX_UPSCALE_TILES}."
        )
    if progress_cb:
        progress_cb(0.0, "loading Real-ESRGAN")
    model = _load_upscaler()
    src = img.convert("RGB")
    width, height = src.size
    natural_scale = max(1, int(getattr(model, "scale", 4) or 4))
    out = Image.new("RGB", (width * natural_scale, height * natural_scale))

    with GPU_LOCK, _cpu_thread_cap():  # share the GPU with local generation
        # Holding the lock is not the same as having room. With LOCAL_OFFLOAD=false
        # the diffusion pipeline stays resident for the whole session, so a card
        # can be 97% full at the moment a post-process step starts. Real-ESRGAN
        # then does not OOM cleanly — it trips "CUDA error: unknown error", which
        # can poison the context for the whole process. Check first, and fall back
        # to the (slower, thread-capped) CPU path rather than risk that.
        cuda = torch.cuda.is_available() and _has_vram_for_tile()
        if torch.cuda.is_available() and not cuda:
            logger.warning("upscaling on CPU: not enough free VRAM for a tile")
        # Move the CACHED instance and record where it went. `model.cpu()` moved
        # the shared model in place while _UPSCALER still believed it was on the
        # GPU, so the next call that DID have VRAM took the cuda branch, sent a
        # CUDA tensor into CPU weights, and raised a device mismatch. One
        # low-VRAM upscale poisoned every upscale after it.
        model = model.to("cuda" if cuda else "cpu")
        _UPSCALER["model"] = model
        for index, ((px0, py0, px1, py1), (x0, y0, _x1, _y1)) in enumerate(tiles):
            if progress_cb:
                progress_cb(index / len(tiles), f"upscaling tile {index + 1}/{len(tiles)}")
            arr = np.asarray(src.crop((px0, py0, px1, py1)), dtype=np.float32) / 255.0
            t = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)
            if cuda:
                t = t.cuda()
            with torch.inference_mode():
                up = model(t)
            up = up.squeeze(0).permute(1, 2, 0).clamp(0, 1).float().cpu().numpy()
            del t
            tile = Image.fromarray((up * 255.0).round().astype("uint8"))
            # Keep both tiles' context and ramp through their shared 64-source-
            # pixel band. Cropping the padding and butt-joining interiors made
            # a measured straight-line discontinuity through every 512 px.
            left_blend = min(tile.width, 2 * _TILE_OVERLAP * natural_scale) if x0 else 0
            top_blend = min(tile.height, 2 * _TILE_OVERLAP * natural_scale) if y0 else 0
            mask = _blend_mask(tile.size, left_blend, top_blend)
            out.paste(tile, (px0 * natural_scale, py0 * natural_scale), mask)
            if progress_cb:
                progress_cb((index + 1) / len(tiles),
                            f"upscaled tile {index + 1}/{len(tiles)}")
        if cuda:
            torch.cuda.empty_cache()

    if out.size != target_size:
        out = out.resize(target_size, Image.Resampling.LANCZOS)
    return out


# ---- face restoration (GFPGAN via spandrel + facexlib) --------------------
# spandrel's core registry supports GFPGAN directly (it also loads the
# Real-ESRGAN upscaler). CodeFormer is intentionally not used: it belongs to
# spandrel-extra-arches and therefore could never be loaded by our core-only
# dependency. facexlib supplies only detection, 5-point alignment and
# paste-back — the restoration network itself does not need the abandoned
# gfpgan/basicsr Python package tree or its torchvision compatibility shims.
_RESTORER: dict[str, object] = {"model": None, "helper": None}

# GFPGAN v1.4 is trained on 512x512 aligned crops normalised to [-1, 1].
_FACE_SIZE = 512


def _load_restorer():
    """Return the cached spandrel GFPGAN descriptor and facexlib helper."""
    if _RESTORER["model"] is not None:
        return _RESTORER["model"], _RESTORER["helper"]
    try:
        import torch
        from spandrel import ModelLoader
    except Exception as e:
        raise ToolUnavailable("Face restoration needs spandrel (a core dep). Re-run `make setup`.") from e
    try:
        from facexlib.utils.face_restoration_helper import FaceRestoreHelper
    except Exception as e:
        raise ToolUnavailable(
            "Face restoration needs the 'faces' extra (facexlib). "
            "Re-run `make setup` to install project-local optional processors."
        ) from e

    device = "cuda" if torch.cuda.is_available() else "cpu"
    weight = _download(GFPGAN_V14, GFPGAN_V14_NAME, GFPGAN_V14_SHA256)
    model = ModelLoader().load_from_file(str(weight)).eval()
    architecture = getattr(getattr(model, "architecture", None), "id", None)
    if str(architecture) != "GFPGAN":
        raise ToolUnavailable(
            f"{GFPGAN_V14_NAME} loaded as {architecture or 'an unknown architecture'}, "
            "not GFPGAN. Delete the weight and retry so it can be downloaded again."
        )
    if device == "cuda":
        model = model.cuda()
    helper = FaceRestoreHelper(
        upscale_factor=1, face_size=_FACE_SIZE, crop_ratio=(1, 1),
        det_model="retinaface_resnet50", save_ext="png", device=device,
        model_rootpath=str(settings.weights_path / "facexlib"),
    )
    _RESTORER["model"] = model
    _RESTORER["helper"] = helper
    return model, helper


def restore_faces(
    img: Image.Image,
    progress_cb: Callable[[float, str], None] | None = None,
) -> Image.Image:
    """Detect, align, restore and paste back every face. Returns the input
    unchanged when no face is found — a no-op beats a confusing error.

    Each crop is isolated: one unusual face that the model cannot process falls
    back to its aligned source crop rather than discarding every other restored
    face and the expensive generation that preceded this step.
    """
    import numpy as np
    import torch

    from .base import GPU_LOCK

    bgr = np.asarray(img.convert("RGB"))[:, :, ::-1].copy()
    if progress_cb:
        progress_cb(0.0, "loading GFPGAN")

    with GPU_LOCK, _cpu_thread_cap():  # share the GPU with local generation
        model, helper = _load_restorer()
        device = next(model.model.parameters()).device if hasattr(model, "model") else "cpu"
        helper.clean_all()
        try:
            if progress_cb:
                progress_cb(0.05, "detecting faces")
            helper.read_image(bgr)
            helper.get_face_landmarks_5(
                only_center_face=False, resize=640, eye_dist_threshold=5)
            if not helper.all_landmarks_5:
                return img
            helper.align_warp_face()

            count = len(helper.cropped_faces)
            for index, cropped in enumerate(helper.cropped_faces):
                if progress_cb:
                    progress_cb(0.1 + 0.75 * index / max(1, count),
                                f"restoring face {index + 1}/{count}")
                try:
                    # BGR uint8 crop -> RGB float tensor normalised to [-1, 1]
                    arr = np.asarray(cropped, dtype=np.float32)[:, :, ::-1] / 255.0
                    t = torch.from_numpy(arr.copy()).permute(2, 0, 1).unsqueeze(0)
                    t = ((t - 0.5) / 0.5).to(device)
                    with torch.no_grad():
                        result = model(t)
                    result = result.squeeze(0).float().cpu()
                    result = (result * 0.5 + 0.5).clamp(0, 1)
                    rgb = result.permute(1, 2, 0).numpy()
                    restored_crop = (rgb[:, :, ::-1] * 255.0).round().astype("uint8")
                except Exception as error:  # noqa: BLE001 — one face is not the whole asset
                    logger.warning("face %s/%s restore failed: %s", index + 1, count, error)
                    restored_crop = cropped
                helper.add_restored_face(restored_crop)

            if progress_cb:
                progress_cb(0.9, "blending restored faces")
            helper.get_inverse_affine(None)
            restored = helper.paste_faces_to_input_image()
        finally:
            # facexlib caches crops, landmarks and affine matrices on the helper.
            # A canceled/failed job must not leak them into the next asset.
            helper.clean_all()

    return Image.fromarray(restored[:, :, ::-1])


# ---- video frame interpolation (RIFE via ONNX, minterpolate fallback) -----
RIFE_ONNX = ("https://huggingface.co/yuvraj108c/rife-onnx/resolve/"
             "64de7265b6a06637c2f2c6a92ecd1972326499fe/"
             "rife49_ensemble_True_scale_1_sim.onnx")
RIFE_ONNX_SHA256 = "76e4cef9ab42fa7dd4e8f6e4aba47462051e3faa969e4bca6479784fbab0ac6f"

_RIFE = {"session": None}


def _load_rife():
    if _RIFE["session"] is not None:
        return _RIFE["session"]
    try:
        import onnxruntime as ort
    except Exception as e:
        raise ToolUnavailable(
            "RIFE interpolation needs the 'rife' extra (onnxruntime). "
            "Re-run `make setup` to install project-local optional processors "
            "(falling back to ffmpeg minterpolate)"
        ) from e
    weight = _download(RIFE_ONNX, "rife49_ensemble_sim.onnx", RIFE_ONNX_SHA256)
    providers = ["CPUExecutionProvider"]
    if "CUDAExecutionProvider" in ort.get_available_providers():
        providers.insert(0, "CUDAExecutionProvider")
    sess = ort.InferenceSession(str(weight), providers=providers)
    _RIFE["session"] = sess
    logger.info("RIFE 4.9 (onnx) loaded [%s]", providers[0])
    return sess


def _rife_middle(sess, f0: np.ndarray, f1: np.ndarray) -> np.ndarray:
    """Middle frame between two HxWx3 uint8 frames via the RIFE ONNX graph."""
    import numpy as np

    h, w = f0.shape[:2]
    ph, pw = -h % 32, -w % 32  # pad to /32 as the graph requires
    a = np.pad(f0, ((0, ph), (0, pw), (0, 0))).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    b = np.pad(f1, ((0, ph), (0, pw), (0, 0))).astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    names = [i.name for i in sess.get_inputs()]
    if len(names) >= 2:
        feed = {names[0]: a, names[1]: b}
    else:  # single concatenated input variant
        feed = {names[0]: np.concatenate([a, b], axis=1)}
    out = sess.run(None, feed)[0][0]
    out = (out.transpose(1, 2, 0)[:h, :w] * 255.0).clip(0, 255).astype("uint8")
    return out


def interpolate_video_rife(src: Path, dst: Path, *, factor: int = 2,
                           out_fps: int | None = None) -> Path:
    """×factor frame-rate boost with a bounded streaming working set."""
    import contextlib

    import imageio

    sess = _load_rife()
    reader = imageio.get_reader(str(src), "ffmpeg")  # type: ignore[arg-type]
    fps = float(reader.get_meta_data().get("fps") or 20)

    # On a GPU provider this is local-GPU work — it can run from the REMOTE
    # lane (post_interpolate on an i2v job), so it must honor the shared mutex
    # or it collides with a local generation on the 16 GB card. Check the whole
    # provider list: [0] may be TensorRT etc. with CUDA still in the chain.
    lock: AbstractContextManager = contextlib.nullcontext()
    if any("CPU" not in p for p in sess.get_providers() or []):
        from .base import GPU_LOCK
        lock = GPU_LOCK

    passes = 1 if factor <= 2 else 2
    target = out_fps or round(fps * (2 ** passes))
    writer = imageio.get_writer(str(dst), fps=target, codec="libx264",
                                pixelformat="yuv420p", macro_block_size=1)
    previous = None
    frame_count = 0
    try:
        with lock:
            for frame in reader.iter_data():
                current = frame[:, :, :3]
                frame_count += 1
                if previous is None:
                    previous = current
                    continue
                middle = _rife_middle(sess, previous, current)
                writer.append_data(previous)
                if passes == 1:
                    writer.append_data(middle)
                else:
                    writer.append_data(_rife_middle(sess, previous, middle))
                    writer.append_data(middle)
                    writer.append_data(_rife_middle(sess, middle, current))
                previous = current
            if previous is not None and frame_count >= 2:
                writer.append_data(previous)
    finally:
        reader.close()
        writer.close()  # even on an encode error — leaks an ffmpeg subprocess otherwise
    if frame_count < 2:
        dst.unlink(missing_ok=True)
        raise ToolUnavailable("clip too short to interpolate")
    return dst


# ---- video frame interpolation (ffmpeg minterpolate) ----------------------
def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — degrade quietly; the caller must not fail here
        return "ffmpeg"


def _run_ffmpeg(cmd: list[str], what: str) -> None:
    """Run an ffmpeg command, surfacing the stderr tail on failure — a bare
    CalledProcessError with captured (dropped) stderr is undebuggable from the UI."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or "").strip()[-500:]
        raise RuntimeError(f"ffmpeg {what} failed: {tail}")


def concat_videos(srcs: list[Path], dst: Path) -> Path:
    """Concatenate clips into one. Re-encodes so clips from separate generations
    join cleanly (the ffmpeg concat demuxer needs identical codecs/params)."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, dir=settings.temp_path
    ) as f:
        for s in srcs:
            f.write(f"file '{s.resolve()}'\n")
        listfile = f.name
    try:
        _run_ffmpeg([_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", listfile,
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dst)], "concat")
    finally:
        Path(listfile).unlink(missing_ok=True)
    return dst


def interpolate_video(src: Path, dst: Path, *, factor: int = 2, out_fps: int | None = None,
                      method: str = "rife") -> Path:
    """Frame interpolation that doubles (or ×factor) the frame rate.

    method="rife" (default) uses RIFE 4.9 via ONNX — sharper motion, needs the
    [rife] extra; anything else (or a RIFE failure) uses ffmpeg's weight-free
    `minterpolate`.
    """
    if method == "rife":
        try:
            return interpolate_video_rife(src, dst, factor=factor, out_fps=out_fps)
        except ToolUnavailable as e:
            logger.warning("RIFE unavailable (%s); using minterpolate", e)
    probe_fps = _probe_fps(src) or 20
    target = out_fps or int(probe_fps * factor)
    _run_ffmpeg([
        _ffmpeg(), "-y", "-i", str(src),
        "-vf", f"minterpolate=fps={target}:mi_mode=mci:mc_mode=aobmc:vsbmc=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dst),
    ], "minterpolate")
    return dst


def _probe_fps(path: Path) -> float | None:
    try:
        import imageio

        reader = imageio.get_reader(str(path), "ffmpeg")  # type: ignore[arg-type]
        meta = reader.get_meta_data()
        reader.close()
        return float(meta["fps"]) if meta.get("fps") else None
    except Exception:  # noqa: BLE001 — degrade quietly; the caller must not fail here
        return None
