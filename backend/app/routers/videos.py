"""Video generation: Text→Video and Image→Video on the Remote GPU (Wan 2.2)."""
from __future__ import annotations

import base64

from fastapi import APIRouter, Form, UploadFile

from ..config import settings
from ..models import JobKind
from ..presets import CAMERA_OPTIONS, apply_camera, quality_steps
from ..prompt_engine import clamp_seed, prepare_prompt, resolve_negative
from ..queue import ProgressCb
from ..utils.seeds import resolve_seed
from .common import (
    MAX_PROMPT,
    as_float,
    as_int,
    normalize_request_id,
    parse_payload,
    persist_video_file,
    register_handler,
    require_upload,
    save_upload,
    submit,
    video_meta_base,
)

router = APIRouter(tags=["videos"])


def _video_params(p: dict, kind: str) -> dict:
    # Normalize FIRST: an unknown engine string is coerced to "wan", so it must
    # also get Wan's frame clamp — branching on the raw value would let a bogus
    # engine smuggle a non-4k+1 frame count into a Wan job.
    camera = p.get("camera", "none")
    camera = camera if camera in CAMERA_OPTIONS else "none"
    engine = p.get("engine", "wan")
    engine = engine if engine in ("wan", "hunyuan", "ltx") else "wan"
    frames = as_int(p.get("num_frames"), 49, 1, 129)
    if engine == "wan":
        # Enforce Wan's 4k+1 frame rule (from the model tuning guide).
        frames = max(25, min(((frames - 1) // 4) * 4 + 1, 121))
    else:
        frames = max(9, min(frames, 129))
    speed = bool(p.get("speed_mode", False)) and engine == "wan"
    quality = p.get("quality", "Standard")
    steps = as_int(p.get("steps"), 40, 1, 100) if quality == "Custom" else quality_steps("video", quality, 40)
    guidance = as_float(p.get("guidance"), 5.0, 0.0, 30.0)
    negative = resolve_negative(kind, p.get("negative_prompt", ""),
                                bool(p.get("auto_negative", True)) and engine == "wan")
    if speed:
        # Lightning distill regime: few steps, CFG 1, negative ignored.
        steps, guidance, negative = min(as_int(p.get("steps"), 4, 1, 100), 8), 1.0, ""
    return {
        "request_id": normalize_request_id(p.get("request_id")),
        # Baked into the prompt here rather than at send time, so what is stored
        # in the job is exactly what the model saw — a rerun cannot drift, and
        # the gallery's "reuse prompt" carries the move with it.
        "prompt": apply_camera((p.get("prompt", "") or ""), camera)[:MAX_PROMPT],
        "camera": camera,
        "negative_prompt": negative[:MAX_PROMPT],
        "quality": quality,
        "num_frames": frames,
        "steps": max(1, min(steps, 100)),
        "guidance": guidance,
        "fps": as_int(p.get("fps"), 20, 8, 30),
        "seed": resolve_seed(p.get("seed")),
        "orientation": p.get("orientation", "portrait"),
        "resolution": p.get("resolution", "720p"),
        "post_interpolate": bool(p.get("post_interpolate", False)),
        "engine": engine,
        "speed_mode": speed,
        "prompt_syntax": (str(p.get("prompt_syntax"))
                          if p.get("prompt_syntax") in {"a1111", "literal"} else "literal"),
    }


def _video_handler(kind: str):
    def handler(job_id: int, params: dict, cb: ProgressCb) -> dict:
        from ..remote_gpu_client import run_remote, save_remote_video
        from ..utils.io import stamp_name

        cb(0.05, f"sending {kind.upper()} request to A100 …")
        seed = int(params.get("seed", 0))
        syntax = str(params.get("prompt_syntax", "literal"))
        prompt = prepare_prompt(params["prompt"], 0, seed=seed, syntax=syntax)
        negative = prepare_prompt(params["negative_prompt"], 0, seed=seed, syntax=syntax)
        body = {
            "prompt": prompt,
            "negative_prompt": negative,
            "num_frames": params["num_frames"], "steps": params["steps"],
            "guidance": params["guidance"], "fps": params["fps"], "seed": params["seed"],
            "resolution": params["resolution"], "orientation": params["orientation"],
            "engine": params.get("engine", "wan"), "speed": bool(params.get("speed_mode", False)),
        }
        if kind == "i2v":
            if not params.get("image_path"):
                raise RuntimeError("Image → Video requires an input image.")
            with open(params["image_path"], "rb") as f:
                body["image_b64"] = base64.b64encode(f.read()).decode()
            if params.get("last_image_path"):  # FLF2V: optional last-frame conditioning
                with open(params["last_image_path"], "rb") as f:
                    body["last_image_b64"] = base64.b64encode(f.read()).decode()

        # run_remote drives progress (0..1) while the A100 works; map it to 0.1–0.85.
        resp = run_remote(f"/{kind}", body,
                          progress_cb=lambda f, m, preview=None: cb(
                              0.1 + 0.75 * f, m, preview=preview), job_id=job_id)
        cb(0.85, "downloading clip …")
        engine = params.get("engine", "wan")
        model = settings.hunyuan_video_model if engine == "hunyuan" else settings.video_model
        meta = {"prompt": prompt, "negative_prompt": negative,
                "seed": params["seed"], **video_meta_base(params),
                "model": model, "engine": engine, "kind": kind}
        settings.ensure_dirs()
        incoming = settings.videos_dir / f"_remote_{stamp_name(kind, 'mp4')}"
        try:
            save_remote_video(resp["video_b64"], incoming)
            asset_id = persist_video_file(
                incoming, job_id=job_id, generator=f"colab_wan:{kind}", meta=meta,
                tag=kind, post_interpolate=params["post_interpolate"], cb=cb,
            )
        finally:
            incoming.unlink(missing_ok=True)  # no-op after persist moves it
        return {"asset_ids": [asset_id]}

    return handler


@router.post("/generate/video/t2v")
async def gen_t2v(payload: str = Form(...)) -> dict:
    params = _video_params(parse_payload(payload), JobKind.t2v.value)
    return await submit(JobKind.t2v.value, params, _video_handler("t2v"))


@router.post("/generate/video/i2v")
async def gen_i2v(payload: str = Form(...), image: UploadFile | None = None,
                  last_frame: UploadFile | None = None) -> dict:
    params = _video_params(parse_payload(payload), JobKind.i2v.value)
    params["image_path"] = require_upload(await save_upload(image))
    if last_frame is not None:  # FLF2V (first+last frame) — optional
        params["last_image_path"] = await save_upload(last_frame)
    return await submit(JobKind.i2v.value, params, _video_handler("i2v"))


register_handler(JobKind.t2v.value, _video_handler("t2v"))
register_handler(JobKind.i2v.value, _video_handler("i2v"))


# ---- Long video: chained shots on the A100, stitched locally ---------------
def _long_params(p: dict) -> dict:
    params = _video_params(p, JobKind.long_video.value)
    params["shots"] = max(2, min(6, int(p.get("shots", 3))))
    return params


def _long_video_handler(job_id: int, params: dict, cb: ProgressCb) -> dict:
    from pathlib import Path

    from ..config import settings
    from ..generators import postprocess as pp
    from ..remote_gpu_client import run_remote, save_remote_video
    from ..utils.io import last_frame_png_b64, require_video_output_space

    shots = params["shots"]
    common = {
        "num_frames": params["num_frames"],
        "steps": params["steps"], "guidance": params["guidance"], "fps": params["fps"],
        "resolution": params["resolution"], "orientation": params["orientation"],
    }
    shot_prompts: list[str] = []
    shot_negatives: list[str] = []
    last_b64: str | None = None
    settings.ensure_dirs()
    seg_paths: list[Path] = []
    concat = settings.videos_dir / f"_concat_{job_id}.mp4"
    try:
        for i in range(shots):
            span0 = i / shots
            # Seed the wildcard picks so an expensive rerun stays reproducible.
            shot_seed = clamp_seed(params["seed"] + i)
            syntax = str(params.get("prompt_syntax", "literal"))
            shot_prompt = prepare_prompt(params["prompt"], i, seed=shot_seed, syntax=syntax)
            shot_negative = prepare_prompt(
                params["negative_prompt"], i, seed=shot_seed, syntax=syntax)
            shot_prompts.append(shot_prompt)
            shot_negatives.append(shot_negative)
            if i == 0:
                endpoint = "/t2v"
                body = {"prompt": shot_prompt, "negative_prompt": shot_negative,
                        "seed": shot_seed, **common}
            else:
                endpoint = "/i2v"
                body = {"prompt": shot_prompt, "negative_prompt": shot_negative,
                        "seed": shot_seed, "image_b64": last_b64, **common}
            resp = run_remote(
                endpoint, body,
                progress_cb=lambda f, m, preview=None, _s=span0, _i=i: cb(
                    _s + 0.85 * f / shots, f"shot {_i + 1}/{shots}: {m}", preview=preview),
                job_id=job_id,
            )
            sp = settings.videos_dir / f"_seg_{job_id}_{i}.mp4"
            save_remote_video(resp["video_b64"], sp)
            seg_paths.append(sp)
            if i < shots - 1:
                cb((i + 1) / shots * 0.85, f"linking shot {i + 2}/{shots}")
                last_b64 = last_frame_png_b64(sp)

        # Stitch from disk: six maximum-size clips never accumulate in RAM.
        cb(0.88, f"stitching {shots} shots")
        meta = {"prompt": shot_prompts[0], "prompts": shot_prompts,
                "negative_prompt": shot_negatives[0], "negative_prompts": shot_negatives,
                "shots": shots, **video_meta_base(params),
                "seconds_est": round(shots * params["num_frames"] / max(1, params["fps"]), 1),
                "model": settings.video_model, "kind": "long_video"}
        require_video_output_space(settings.videos_dir, seg_paths)
        pp.concat_videos(seg_paths, concat)
        # Persist by moving the file — the stitched clip never rides through RAM.
        asset_id = persist_video_file(concat, job_id=job_id, generator="long_video", meta=meta,
                                      tag="long",
                                      post_interpolate=params.get("post_interpolate", False), cb=cb)
    finally:
        for p in [*seg_paths, concat]:
            Path(p).unlink(missing_ok=True)  # concat unlink is a no-op after a successful move

    return {"asset_ids": [asset_id]}


@router.post("/generate/video/long")
async def gen_long(payload: str = Form(...)) -> dict:
    params = _long_params(parse_payload(payload))
    return await submit(JobKind.long_video.value, params, _long_video_handler)


register_handler(JobKind.long_video.value, _long_video_handler)
