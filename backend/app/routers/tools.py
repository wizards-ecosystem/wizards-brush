"""Standalone post-processing tools applied to existing gallery assets:
upscale, face restore, detail, background matte (images), and frame
interpolation and extension (videos).

Handlers live at module level and are registered at import — like images.py /
videos.py — so a finished tool job can be re-run after a server restart.

`TOOLS` is the catalogue both entry points read: the per-tool routes below and
the unified `POST /api/jobs`. Each entry names the asset kind it takes, its
settings as registry-style controls (so one validator serves generators and
tools alike) and the function that turns a source asset plus settings into the
job's params, so a tool job cannot differ by the door it came in through.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from PIL import Image, ImageOps
from pydantic import BaseModel

from .. import db
from ..models import Asset, JobKind
from ..queue import ProgressCb
from .common import (
    MAX_PROMPT,
    as_float,
    as_int,
    derivative_meta,
    persist_image,
    persist_video_file,
    register_handler,
    snap_scale,
    submit,
)

router = APIRouter(tags=["tools"])


class ToolReq(BaseModel):
    asset_id: int
    scale: int = 4          # upscale factor (2 or 4)
    factor: int = 2         # interpolation factor (2 or 4)
    method: str = "rife"    # interpolation engine: rife | minterpolate


class DetailReq(BaseModel):
    asset_id: int
    prompt: str = ""
    denoise: float = 0.35
    targets: list[str] = ["face"]


def require_asset(asset_id: int, kind: str) -> Asset:
    """The live (untrashed) asset `asset_id`, which must be of `kind`."""
    a = db.get_asset(asset_id)
    if not a:
        raise HTTPException(status_code=404, detail=f"asset {asset_id} not found")
    if a.kind != kind:
        raise HTTPException(status_code=400, detail=f"asset {asset_id} is not a {kind}")
    return a


_require_asset = require_asset  # the historical name


def _src_image(p: dict) -> Image.Image:
    if not Path(p["src_path"]).exists():
        raise RuntimeError("source image file no longer exists")
    return Image.open(p["src_path"]).convert("RGB")


def _src_video(p: dict) -> Path:
    src = Path(p["src_path"])
    if not src.exists():
        raise RuntimeError("source video file no longer exists")
    return src


# ---- handlers (module-level + registered so rerun survives a restart) ------
def _upscale_handler(job_id: int, p: dict, cb: ProgressCb) -> dict:
    from ..generators import postprocess as pp

    scale = snap_scale(p.get("scale"))  # snapped here too so old job reruns are covered
    cb(0.2, "upscaling …")
    src = _src_image(p)
    img = pp.upscale_image(
        src, scale=scale,
        progress_cb=lambda f, m: cb(0.2 + 0.75 * f, m),
    )
    requested_size = (src.width * scale, src.height * scale)
    aid = persist_image(img, job_id=job_id, generator="upscale",
                        meta=derivative_meta(
                            p.get("src_meta"), operations="upscale", input_size=src.size,
                            output_size=img.size,
                            details={
                                "scale": scale,
                                "requested_size": {"width": requested_size[0],
                                                   "height": requested_size[1]},
                                "clamped": img.size != requested_size,
                                "max_side": pp.MAX_UPSCALE_SIDE,
                                "source_asset_id": p.get("source_asset_id"),
                            },
                        ), tag="upscaled")
    return {"asset_ids": [aid]}


def _face_handler(job_id: int, p: dict, cb: ProgressCb) -> dict:
    from ..generators import postprocess as pp

    cb(0.2, "restoring faces …")
    src = _src_image(p)
    img = pp.restore_faces(src, progress_cb=lambda f, m: cb(0.2 + 0.75 * f, m))
    aid = persist_image(img, job_id=job_id, generator="face_restore",
                        meta=derivative_meta(
                            p.get("src_meta"), operations="face_restore", input_size=src.size,
                            output_size=img.size,
                            details={"source_asset_id": p.get("source_asset_id")},
                        ), tag="restored")
    return {"asset_ids": [aid]}


def _detail_handler(job_id: int, p: dict, cb: ProgressCb) -> dict:
    from ..generators import detailer, variants

    cb(0.1, "detecting regions …")
    src_meta = p.get("src_meta", {})
    # Refine with the model that made the image when it is still a configured
    # local variant — using the default instead would silently change both the
    # model and its CFG/step recipe.
    src_model = str(src_meta.get("model", ""))
    model = src_model if variants.by_repo(src_model) else None
    src = _src_image(p)
    denoise = as_float(p.get("denoise"), 0.35, 0.1, 0.9)
    targets = tuple(p.get("targets") or ["face"])
    img, n = detailer.refine(
        src,
        prompt=p.get("prompt", ""), negative=str(src_meta.get("negative_prompt", "")),
        denoise=denoise,
        targets=targets,
        seed=int(src_meta.get("seed", 0) or 0),
        model=model,
        progress_cb=cb,
    )
    if n == 0:
        raise RuntimeError("no faces/hands detected — nothing to refine")
    aid = persist_image(img, job_id=job_id, generator="detail",
                        meta=derivative_meta(
                            src_meta, operations="detail", input_size=src.size,
                            output_size=img.size,
                            details={
                                "regions": n, "denoise": denoise, "targets": list(targets),
                                "source_asset_id": p.get("source_asset_id"),
                            },
                        ), tag="detailed")
    return {"asset_ids": [aid]}


def _interpolate_handler(job_id: int, p: dict, cb: ProgressCb) -> dict:
    from ..config import settings
    from ..generators import postprocess as pp
    from ..utils.io import stamp_name

    cb(0.2, "interpolating frames …")
    src = _src_video(p)
    factor = snap_scale(p.get("factor"), default=2)  # RIFE passes are ×2/×4
    settings.ensure_dirs()
    # stamp_name, not a deterministic "<src>_x2.mp4": rerunning the tool on the
    # same source must not overwrite the previous result's file out from under
    # its asset row.
    dst = settings.videos_dir / stamp_name(f"{src.stem}_x{factor}", "mp4")
    pp.interpolate_video(src, dst, factor=factor, method=p.get("method", "rife"))
    size = (int(p.get("src_width") or 0), int(p.get("src_height") or 0))
    meta = derivative_meta(
        p.get("src_meta"), operations="interpolate", input_size=size, output_size=size,
        details={
            "factor": factor, "method": p.get("method", "rife"),
            "source_asset_id": p.get("source_asset_id"),
        },
    )
    aid = persist_video_file(
        dst, job_id=job_id, generator="interpolate", meta=meta, tag="interpolated",
        post_interpolate=False, cb=cb,
    )
    return {"asset_ids": [aid]}


def _extend_handler(job_id: int, p: dict, cb: ProgressCb) -> dict:
    from ..config import settings
    from ..generators import postprocess as pp
    from ..remote_gpu_client import run_remote, save_remote_video
    from ..utils.io import last_frame_png_b64, require_video_output_space
    src = _src_video(p)
    cb(0.05, "extracting last frame …")
    last_b64 = last_frame_png_b64(src)
    body = {
        "prompt": p["prompt"], "negative_prompt": p["negative_prompt"],
        "num_frames": p["num_frames"], "steps": p["steps"], "guidance": p["guidance"],
        "fps": p["fps"], "seed": p["seed"],
        # Match the source clip — a 720p extension of a 480p clip can't concat.
        "resolution": p.get("resolution", "720p"),
        "orientation": p.get("orientation", "portrait"),
        "image_b64": last_b64, "speed": bool(p.get("speed_mode", False)),
    }
    resp = run_remote("/i2v", body,
                      progress_cb=lambda f, m, preview=None: cb(
                          0.05 + 0.75 * f, m, preview=preview), job_id=job_id)
    cb(0.85, "stitching …")
    settings.ensure_dirs()
    seg = settings.videos_dir / f"_seg_{job_id}_ext.mp4"
    concat = settings.videos_dir / f"_concat_{job_id}.mp4"
    size = (int(p.get("src_width") or 0), int(p.get("src_height") or 0))
    meta = derivative_meta(
        {**p.get("src_meta", {}), "prompt": p["prompt"], "seed": p["seed"],
         "num_frames": p["num_frames"]},
        operations="extend_video", input_size=size, output_size=size,
        details={
            "source_asset_id": p.get("source_asset_id"),
            "added_frames": p["num_frames"], "fps": p["fps"],
        },
    )
    try:
        save_remote_video(resp["video_b64"], seg)
        require_video_output_space(settings.videos_dir, [src, seg])
        pp.concat_videos([src, seg], concat)
        # Persist by moving the file — the stitched clip never rides through RAM.
        aid = persist_video_file(concat, job_id=job_id, generator="extend_video", meta=meta,
                                 tag="extended", post_interpolate=False, cb=cb)
    finally:
        seg.unlink(missing_ok=True)
        concat.unlink(missing_ok=True)  # no-op after a successful move
    return {"asset_ids": [aid]}


# Background matte: BiRefNet-lite (MIT) via onnxruntime; see generators/matting.py.
# "cutout" keeps the subject on transparency; "mask" and "inverse_mask" save the
# matte itself as a reusable mask asset (white = change), selecting the subject
# or everything around it for inpainting and Variant Set stages.
MATTE_MODES = ("cutout", "mask", "inverse_mask")


def _matte_handler(job_id: int, p: dict, cb: ProgressCb) -> dict:
    from ..generators import matting

    if reason := matting.unavailable_reason():
        raise RuntimeError(reason)
    if not Path(p["src_path"]).exists():
        raise RuntimeError("source image file no longer exists")
    mode = p.get("mode") if p.get("mode") in MATTE_MODES else "cutout"
    with Image.open(p["src_path"]) as opened:
        opened.load()
        # Keep an existing alpha channel: a cutout of a cutout must stay cut.
        src = opened.convert("RGBA" if "A" in opened.getbands() else "RGB")
    cb(0.15, "finding the subject …")
    details: dict[str, Any] = {"mode": mode, "source_asset_id": p.get("source_asset_id"),
                               "model": matting.MODEL_ID, "license": matting.LICENSE}
    if mode == "cutout":
        img, record = matting.remove_background(src)
        details["transparent_fraction"] = record["transparent_fraction"]
        generator, tag = "matte:cutout", "cutout"
    else:
        img = matting.predict_matte(src)
        if mode == "inverse_mask":
            img = ImageOps.invert(img)
        generator, tag = "mask", "mask"
    cb(0.9, "saving …")
    meta = derivative_meta(p.get("src_meta"), operations="matte", input_size=src.size,
                           output_size=img.size, details=details)
    if generator == "mask":
        # The same record an uploaded mask carries, so pickers treat both alike.
        meta["mask"] = {"source": "matte", "mode": mode,
                        "source_asset_id": p.get("source_asset_id")}
    aid = persist_image(img, job_id=job_id, generator=generator, meta=meta, tag=tag)
    return {"asset_ids": [aid]}


# ---- the tool catalogue -------------------------------------------------------
def _source(a: Asset) -> dict[str, Any]:
    return {"src_path": a.path, "src_meta": a.meta, "source_asset_id": a.id,
            "src_width": a.width, "src_height": a.height}


def _upscale_params(a: Asset, raw: dict[str, Any]) -> dict[str, Any]:
    return {**_source(a), "scale": snap_scale(raw.get("scale", 4))}


def _face_params(a: Asset, raw: dict[str, Any]) -> dict[str, Any]:
    return _source(a)


def _detail_params(a: Asset, raw: dict[str, Any]) -> dict[str, Any]:
    targets = [t for t in (raw.get("targets") or ["face"]) if t in ("face", "hand")]
    return {**_source(a), "prompt": str(raw.get("prompt") or "")[:MAX_PROMPT],
            "denoise": as_float(raw.get("denoise"), 0.35, 0.1, 0.9),
            "targets": targets or ["face"]}


def _interpolate_params(a: Asset, raw: dict[str, Any]) -> dict[str, Any]:
    return {**_source(a), "factor": snap_scale(raw.get("factor", 2), default=2),
            "method": str(raw.get("method") or "rife")}


def _extend_params(a: Asset, raw: dict[str, Any]) -> dict[str, Any]:
    from ..prompt_engine import resolve_negative
    from ..utils.seeds import resolve_seed

    frames = as_int(raw.get("num_frames"), 49, 1, 129)
    meta = a.meta
    return {
        **_source(a), "prompt": str(raw.get("prompt") or "")[:MAX_PROMPT],
        "negative_prompt": resolve_negative("i2v", "", True),
        "num_frames": max(25, min(((frames - 1) // 4) * 4 + 1, 121)),  # Wan 4k+1
        "steps": as_int(raw.get("steps"), 40, 1, 100),
        "guidance": as_float(raw.get("guidance"), 5.0, 0.0, 30.0),
        "fps": as_int(raw.get("fps"), 20, 8, 30),
        "seed": resolve_seed(raw.get("seed", -1)),
        "speed_mode": bool(raw.get("speed_mode", False)),
        # Extension must be generated at the source clip's size or concat fails.
        "resolution": meta.get("resolution") or "720p",
        "orientation": meta.get("orientation") or "portrait",
    }


def _matte_params(a: Asset, raw: dict[str, Any]) -> dict[str, Any]:
    mode = raw.get("mode", "cutout")
    return {**_source(a), "mode": mode if mode in MATTE_MODES else "cutout"}


@dataclass(frozen=True)
class Tool:
    kind: str
    title: str
    description: str
    input: str                    # the asset kind it takes: "image" | "video"
    output: str                   # what it makes
    controls: tuple[dict[str, Any], ...]
    build: Callable[[Asset, dict[str, Any]], dict[str, Any]]
    handler: Callable
    unavailable: Callable[[], str | None] = lambda: None


def _matte_unavailable() -> str | None:
    from ..generators import matting

    return matting.unavailable_reason()


_SEED_CONTROL = {"name": "seed", "label": "Seed", "type": "number", "default": -1,
                 "min": -1, "max": 2**32 - 1}

TOOLS: dict[str, Tool] = {t.kind: t for t in (
    Tool("upscale", "Upscale", "Real-ESRGAN, 2x or 4x.", "image", "image",
         ({"name": "scale", "label": "Scale", "type": "select", "default": 4,
           "options": [2, 4]},),
         _upscale_params, _upscale_handler),
    Tool("face_restore", "Face restore", "GFPGAN over every detected face.", "image", "image",
         (), _face_params, _face_handler),
    Tool("detail", "Detail", "Re-render detected faces or hands with a masked low-denoise pass.",
         "image", "image",
         ({"name": "prompt", "label": "Prompt", "type": "textarea", "default": ""},
          {"name": "denoise", "label": "Denoise", "type": "slider", "default": 0.35,
           "min": 0.1, "max": 0.9, "step": 0.05},
          {"name": "targets", "label": "Targets", "type": "multiselect", "default": ["face"],
           "options": ["face", "hand"]}),
         _detail_params, _detail_handler),
    Tool("matte", "Background matte",
         "Cut the subject out onto transparency, or save it (or its surroundings) as a mask.",
         "image", "image",
         ({"name": "mode", "label": "Output", "type": "select", "default": "cutout",
           "options": list(MATTE_MODES)},),
         _matte_params, _matte_handler, _matte_unavailable),
    Tool("interpolate", "Interpolate", "Multiply a clip's frame rate.", "video", "video",
         ({"name": "factor", "label": "Factor", "type": "select", "default": 2,
           "options": [2, 4]},
          {"name": "method", "label": "Method", "type": "select", "default": "rife",
           "options": ["rife", "minterpolate"]}),
         _interpolate_params, _interpolate_handler),
    Tool("extend_video", "Extend video",
         "Continue a clip from its last frame on the Remote GPU and stitch the result.",
         "video", "video",
         ({"name": "prompt", "label": "Prompt", "type": "textarea", "default": ""},
          {"name": "num_frames", "label": "Frames", "type": "slider", "default": 49,
           "min": 25, "max": 121, "step": 4},
          {"name": "steps", "label": "Steps", "type": "slider", "default": 40,
           "min": 1, "max": 100},
          {"name": "guidance", "label": "Guidance", "type": "slider", "default": 5.0,
           "min": 0.0, "max": 30.0, "step": 0.5},
          {"name": "fps", "label": "FPS", "type": "slider", "default": 20, "min": 8, "max": 30},
          _SEED_CONTROL,
          {"name": "speed_mode", "label": "Speed mode", "type": "toggle", "default": False}),
         _extend_params, _extend_handler),
)}


async def submit_tool(kind: str, asset_id: int, raw: dict[str, Any],
                      request_id: str | None = None) -> dict:
    """Queue tool `kind` on gallery asset `asset_id`: the one way a tool job is made."""
    tool = TOOLS[kind]
    if reason := tool.unavailable():
        raise HTTPException(status_code=503, detail=reason)
    a = require_asset(asset_id, tool.input)
    return await submit(kind, tool.build(a, raw), tool.handler, request_id=request_id)


# ---- routes -----------------------------------------------------------------
@router.post("/tools/upscale")
async def tool_upscale(req: ToolReq) -> dict:
    return await submit_tool(JobKind.upscale.value, req.asset_id, {"scale": req.scale})


@router.post("/tools/face-restore")
async def tool_face(req: ToolReq) -> dict:
    return await submit_tool(JobKind.face_restore.value, req.asset_id, {})


class ExtendReq(BaseModel):
    asset_id: int
    prompt: str
    num_frames: int = 49
    steps: int = 40
    guidance: float = 5.0
    fps: int = 20
    seed: int = -1
    speed_mode: bool = False


@router.post("/tools/extend-video")
async def tool_extend_video(req: ExtendReq) -> dict:
    """Continue a video: last frame → i2v on the A100, then concat with the source."""
    return await submit_tool(JobKind.extend_video.value, req.asset_id,
                             req.model_dump(exclude={"asset_id"}))


class MatteReq(BaseModel):
    asset_id: int
    mode: str = "cutout"


@router.post("/tools/matte")
async def tool_matte(req: MatteReq) -> dict:
    """Cut an image's subject out, or save its subject/background as a mask asset."""
    if req.mode not in MATTE_MODES:
        raise HTTPException(status_code=400,
                            detail=f"mode must be one of {', '.join(MATTE_MODES)}")
    return await submit_tool(JobKind.matte.value, req.asset_id, {"mode": req.mode})


@router.post("/tools/enrich-all")
async def enrich_all() -> dict:
    """Backfill: queue captioning for every asset without a caption."""
    from .. import enrichment

    rows, _total = db.search_assets(limit=100000)
    n = 0
    for a in rows:
        if not a.caption:
            assert a.id is not None
            enrichment.enqueue(a.id)
            n += 1
    return {"ok": True, "queued": n}


@router.post("/tools/detail")
async def tool_detail(req: DetailReq) -> dict:
    return await submit_tool(JobKind.detail.value, req.asset_id,
                             {"prompt": req.prompt, "denoise": req.denoise,
                              "targets": req.targets})


@router.post("/tools/interpolate")
async def tool_interpolate(req: ToolReq) -> dict:
    return await submit_tool(JobKind.interpolate.value, req.asset_id,
                             {"factor": req.factor, "method": req.method})


# Register so finished tool jobs can be re-run by id even in a fresh process.
for _tool in TOOLS.values():
    register_handler(_tool.kind, _tool.handler)
