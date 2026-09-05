"""Standalone post-processing tools applied to existing gallery assets:
upscale, face restore (images), and frame interpolation (videos).

Handlers live at module level and are registered at import — like images.py /
videos.py — so a finished tool job can be re-run after a server restart.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from PIL import Image
from pydantic import BaseModel

from .. import db
from ..models import AssetKind, JobKind
from ..queue import ProgressCb
from .common import (
    MAX_PROMPT,
    as_float,
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


def _require_asset(asset_id: int, kind: str):
    a = db.get_asset(asset_id)
    if not a:
        raise HTTPException(status_code=404, detail=f"asset {asset_id} not found")
    if a.kind != kind:
        raise HTTPException(status_code=400, detail=f"asset {asset_id} is not a {kind}")
    return a


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


# ---- routes -----------------------------------------------------------------
@router.post("/tools/upscale")
async def tool_upscale(req: ToolReq) -> dict:
    a = _require_asset(req.asset_id, AssetKind.image.value)
    params = {"src_path": a.path, "scale": snap_scale(req.scale), "src_meta": a.meta,
              "source_asset_id": a.id, "src_width": a.width, "src_height": a.height}
    return await submit(JobKind.upscale.value, params, _upscale_handler)


@router.post("/tools/face-restore")
async def tool_face(req: ToolReq) -> dict:
    a = _require_asset(req.asset_id, AssetKind.image.value)
    params = {"src_path": a.path, "src_meta": a.meta, "source_asset_id": a.id,
              "src_width": a.width, "src_height": a.height}
    return await submit(JobKind.face_restore.value, params, _face_handler)


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
    from ..prompt_engine import resolve_negative
    from ..utils.seeds import resolve_seed

    a = _require_asset(req.asset_id, AssetKind.video.value)
    frames = max(25, min(((req.num_frames - 1) // 4) * 4 + 1, 121))  # Wan 4k+1
    meta = a.meta
    params = {
        "src_path": a.path, "src_meta": meta, "prompt": req.prompt[:MAX_PROMPT],
        "source_asset_id": a.id, "src_width": a.width, "src_height": a.height,
        "negative_prompt": resolve_negative("i2v", "", True),
        "num_frames": frames, "steps": max(1, min(req.steps, 100)),
        "guidance": as_float(req.guidance, 5.0, 0.0, 30.0), "fps": max(8, min(req.fps, 30)),
        "seed": resolve_seed(req.seed), "speed_mode": req.speed_mode,
        # Extension must be generated at the source clip's size or concat fails.
        "resolution": meta.get("resolution") or "720p",
        "orientation": meta.get("orientation") or "portrait",
    }
    return await submit(JobKind.extend_video.value, params, _extend_handler)


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
    a = _require_asset(req.asset_id, AssetKind.image.value)
    params = {"src_path": a.path, "prompt": req.prompt,
              "denoise": as_float(req.denoise, 0.35, 0.1, 0.9),
              "targets": req.targets, "src_meta": a.meta, "source_asset_id": a.id,
              "src_width": a.width, "src_height": a.height}
    return await submit(JobKind.detail.value, params, _detail_handler)


@router.post("/tools/interpolate")
async def tool_interpolate(req: ToolReq) -> dict:
    a = _require_asset(req.asset_id, AssetKind.video.value)
    params = {"src_path": a.path, "factor": snap_scale(req.factor, default=2),
              "method": req.method, "src_meta": a.meta, "source_asset_id": a.id,
              "src_width": a.width, "src_height": a.height}
    return await submit(JobKind.interpolate.value, params, _interpolate_handler)


# Register so finished tool jobs can be re-run by id even in a fresh process.
register_handler(JobKind.upscale.value, _upscale_handler)
register_handler(JobKind.face_restore.value, _face_handler)
register_handler(JobKind.detail.value, _detail_handler)
register_handler(JobKind.interpolate.value, _interpolate_handler)
register_handler(JobKind.extend_video.value, _extend_handler)
