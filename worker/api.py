"""The HTTP surface and the job protocol behind it.

Generation takes minutes and every managed host puts a reverse proxy in front of
this service with a ~100s ceiling, so a POST enqueues and returns a token
immediately; the client polls /result/{token} and /acks once it holds the bytes.
"""
from __future__ import annotations

import base64
import hmac
import inspect
import io
import queue as _queue
import threading
import time
import uuid
from typing import Annotated

import torch
from fastapi import FastAPI, Header, HTTPException
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from . import build_id, config, pipelines, runtime, watchdog

# ---- API ------------------------------------------------------------------
app = FastAPI(title="The Wizard's Brush Remote GPU Server")

MAX_REMOTE_BODY_BYTES = 192 * 1024 * 1024


class _RemoteBodyTooLarge(Exception):
    pass


class _RemoteIngressMiddleware:
    """Authenticate and cap public-tunnel requests before parsing their body."""

    def __init__(self, app, *, secret: str, max_body_bytes: int = MAX_REMOTE_BODY_BYTES):
        self.app = app
        self.secret = secret
        self.max_body_bytes = max_body_bytes

    async def _respond(self, send, status: int, detail: str) -> None:
        import json

        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"x-gen-secret", b"").decode("latin-1")
        if not hmac.compare_digest(supplied, self.secret):
            await self._respond(send, 401, "bad or missing X-Gen-Secret")
            return
        # An authenticated request is the heartbeat the idle switch watches for.
        # Unauthenticated noise must not count: a public proxy URL attracts
        # scanners, and a scanner is not a reason to hold a GPU open.
        watchdog.touch()
        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > self.max_body_bytes:
                    await self._respond(send, 413, "request body too large")
                    return
            except ValueError:
                await self._respond(send, 400, "invalid Content-Length")
                return

        consumed = 0
        response_started = False

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_body_bytes:
                    raise _RemoteBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RemoteBodyTooLarge:
            if not response_started:
                await self._respond(send, 413, "request body too large")


app.add_middleware(_RemoteIngressMiddleware, secret=config.SHARED_SECRET)


def _auth(secret: str | None) -> None:
    if not hmac.compare_digest(secret or "", config.SHARED_SECRET):
        raise HTTPException(status_code=401, detail="bad or missing X-Gen-Secret")


# ---- async job infra -------------------------------------------------------
# Generation/first-load can take minutes; a single long HTTP request would hit
# Cloudflare's ~100s tunnel timeout (error 524) applies when the optional tunnel
# is used. POSTs therefore enqueue and return a
# token immediately; the client polls the short /result/{token} until done.

_JOBS: dict = {}
_REMOTE_QUEUE_LIMIT = 32
_TASKQ: _queue.Queue = _queue.Queue(maxsize=_REMOTE_QUEUE_LIMIT)
# client_job_id -> token: dedupes a client's submit retry (the POST may succeed
# server-side while the tunnel drops the response — never enqueue it twice).
_CLIENT_TOKENS: dict = {}


def _job_worker() -> None:
    """Drain the queue forever. EVERY per-job step is inside the try: this thread
    is the only thing that runs jobs, so if it dies the server keeps handing out
    tokens for work that will never happen while /health still says ok."""
    while True:
        token, fn = _TASKQ.get()
        try:
            j = _JOBS.get(token)
            if j is None:          # pruned before it ran — nothing to report to
                continue
            if j.get("cancel"):
                # Cancellation while queued must stop before `fn` resolves a
                # pipeline. Loading/evicting a model for already-canceled work
                # can waste most of a paid session before the first callback.
                j["status"] = "error"
                j["error"] = "canceled"
                j["finished_at"] = time.monotonic()
                continue
            j["status"] = "running"
            result = fn(token)
            # A cancelled job is NOT a successful one. diffusers' _interrupt
            # makes the pipeline return early rather than raise, so the call
            # above comes back normally holding a partially-denoised image.
            # Reporting that as "done" hands the client a garbage result that
            # looks successful — it would be persisted into the gallery as if
            # the user had asked for it.
            if j.get("cancel"):
                j["status"] = "error"
                j["error"] = "canceled"
                j["result"] = None
            else:
                j["result"] = result
                j["status"] = "done"
                j["progress"] = 1.0
            j["finished_at"] = time.monotonic()
        except Exception as e:  # noqa: BLE001
            import traceback
            j = _JOBS.get(token)
            if j is not None:
                j["status"] = "error"
                j["error"] = str(e)
                j["finished_at"] = time.monotonic()
            print("[remote-gpu] job error:\n", traceback.format_exc())
        finally:
            _TASKQ.task_done()


_WORKER: dict = {"thread": None}


def _ensure_worker() -> bool:
    """Start the worker, or restart it if it somehow died. Returns liveness."""
    t = _WORKER["thread"]
    if t is None or not t.is_alive():
        if t is not None:
            print("[remote-gpu] !! job worker died — restarting")
        t = threading.Thread(target=_job_worker, daemon=True, name="job-worker")
        _WORKER["thread"] = t
        t.start()
    return True


_ensure_worker()


# A delivered result is kept this long so a client whose response was dropped
# mid-flight can just poll again. An undelivered one is kept far longer — the
# client may still be working through a slow download.
_DELIVERED_TTL = 300.0      # 5 min after the client first received it
_UNDELIVERED_TTL = 3600.0   # 1 h for a result nobody has collected yet


def _prune_jobs() -> None:
    """Free finished jobs so never-collected results (multi-MB b64 videos) can't
    accumulate over a long session.

    Age-based, never count-based: a fixed `keep` window evicted results that a
    still-polling client had not fetched yet, and the client treats the
    resulting 404 as fatal rather than retrying."""
    now = time.monotonic()
    for t, j in list(_JOBS.items()):
        if j["status"] not in ("done", "error"):
            continue
        delivered = j.get("delivered_at")
        age = now - (delivered if delivered is not None
                     else j.get("finished_at") or now)
        if age > (_DELIVERED_TTL if delivered is not None else _UNDELIVERED_TTL):
            _JOBS.pop(t, None)
    for cid in [c for c, t in _CLIENT_TOKENS.items() if t not in _JOBS]:
        _CLIENT_TOKENS.pop(cid, None)


def _enqueue(fn, client_id: str = "") -> dict:
    _ensure_worker()
    _prune_jobs()
    if client_id and _CLIENT_TOKENS.get(client_id) in _JOBS:
        return {"token": _CLIENT_TOKENS[client_id]}  # duplicate submit retry
    if _TASKQ.full():
        raise HTTPException(status_code=429, detail="Remote GPU queue is full")
    token = uuid.uuid4().hex
    _JOBS[token] = {"status": "queued", "progress": 0.0, "error": "", "result": None,
                    "preview": None, "cancel": False,
                    "finished_at": None, "delivered_at": None}
    if client_id:
        _CLIENT_TOKENS[client_id] = token
    try:
        _TASKQ.put_nowait((token, fn))
    except _queue.Full:
        _JOBS.pop(token, None)
        if client_id:
            _CLIENT_TOKENS.pop(client_id, None)
        raise HTTPException(status_code=429, detail="Remote GPU queue is full") from None
    return {"token": token}


def _raise_if_canceled(token: str) -> None:
    """Close the dequeue-to-load race before any model or input work begins."""
    if _JOBS.get(token, {}).get("cancel"):
        raise RuntimeError("canceled")


def _latent_preview(pipe, kw: dict, width: int, height: int) -> str | None:
    """Rough latent → tiny JPEG (structure only). Never fails a job."""
    try:
        lat = kw.get("latents")
        if lat is None:
            return None
        if lat.dim() == 3 and hasattr(pipe, "_unpack_latents"):
            lat = pipe._unpack_latents(lat, height, width, getattr(pipe, "vae_scale_factor", 8))
        if lat.dim() == 5:
            lat = lat[:, :, lat.shape[2] // 2]
        if lat.dim() != 4:
            return None
        x = lat[0].detach().float().cpu()
        c = x.shape[0]
        third = max(1, c // 3)
        t = torch.stack([x[0:third].mean(0), x[third:2 * third].mean(0),
                         x[2 * third:3 * third].mean(0)], dim=-1)
        lo, hi = t.min(), t.max()
        t = (t - lo) / (hi - lo) if float(hi - lo) > 1e-6 else torch.zeros_like(t)
        import numpy as np

        img = Image.fromarray((t.numpy() * 255.0).astype(np.uint8))
        img.thumbnail((160, 160))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=60)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return None


def _progress_cb(token: str, total: int, width: int = 0, height: int = 0):
    def cb(pipe, step, t, kw):
        if _JOBS[token].get("cancel"):
            pipe._interrupt = True  # honored per-step by diffusers pipelines
        _JOBS[token]["progress"] = (step + 1) / max(1, total)
        if config.PREVIEW_EVERY and (step + 1) % config.PREVIEW_EVERY == 0 and (step + 1) < total:
            _JOBS[token]["preview"] = _latent_preview(pipe, kw, width, height)
        return kw
    return cb


@app.get("/result/{token}")
def result(token: str, x_gen_secret: str | None = Header(default=None)):
    _auth(x_gen_secret)
    j = _JOBS.get(token)
    if not j:
        raise HTTPException(status_code=404, detail="unknown token")
    out = {"status": j["status"], "progress": j["progress"], "error": j["error"]}
    if j["status"] == "running" and j.get("preview"):
        out["preview"] = j["preview"]
    if j["status"] == "done":
        # Return the result WITHOUT dropping it. This used to be j.pop(), so if
        # the tunnel lost the response — the exact failure the client's retry
        # loop exists for — the next poll raised KeyError, surfaced as a 500,
        # and minutes of A100 time were unrecoverable while the finished image
        # sat in RAM. The client calls /ack when it has persisted the result;
        # otherwise _prune_jobs reclaims it on the TTL above.
        out["result"] = j["result"]
        if j.get("delivered_at") is None:
            j["delivered_at"] = time.monotonic()
    return out


@app.post("/ack/{token}")
def ack(token: str, x_gen_secret: str | None = Header(default=None)):
    """Client has persisted the result — free it now instead of waiting for TTL."""
    _auth(x_gen_secret)
    j = _JOBS.get(token)
    if j is not None:
        j["result"] = None
        j["delivered_at"] = time.monotonic() - _DELIVERED_TTL  # eligible next prune
    return {"ok": bool(j)}


@app.post("/cancel/{token}")
def cancel(token: str, x_gen_secret: str | None = Header(default=None)):
    _auth(x_gen_secret)
    j = _JOBS.get(token)
    if j:
        j["cancel"] = True
    return {"ok": bool(j)}


@app.post("/cancel/client/{client_id}")
def cancel_client(client_id: str, x_gen_secret: str | None = Header(default=None)):
    """Cancel by durable submit id when the token response was lost to a restart."""
    _auth(x_gen_secret)
    token = _CLIENT_TOKENS.get(client_id)
    j = _JOBS.get(token) if token else None
    if j:
        j["cancel"] = True
    return {"ok": bool(j)}


Prompt = Annotated[str, Field(max_length=2000)]
EncodedImage = Annotated[str, Field(max_length=56 * 1024 * 1024)]


class ImageReq(BaseModel):
    # Which configured image model to run. Unknown values fall back
    # to the quality slot rather than failing — see pipelines.image_model_for().
    model_variant: str = Field(default="quality", max_length=32)
    prompt: Prompt
    negative_prompt: Prompt = ""
    steps: int = Field(default=30, ge=1, le=200)
    guidance: float = Field(default=3.5, ge=0, le=50)
    seed: int = Field(default=0, ge=0, le=2**32 - 1)
    width: int = Field(default=1024, ge=16, le=4096)
    height: int = Field(default=1024, ge=16, le=4096)
    speed: bool = False  # lightning LoRA: 4-8 steps, CFG 1
    # Optional batch: one round-trip generates several images on the A100.
    prompts: list[Prompt] | None = Field(default=None, max_length=8)
    negative_prompts: list[Prompt] | None = Field(default=None, max_length=8)
    seeds: list[int] | None = Field(default=None, max_length=8)
    client_job_id: str = Field(default="", max_length=128)


class EditReq(BaseModel):
    prompt: Prompt
    negative_prompt: Prompt = ""
    steps: int = Field(default=30, ge=1, le=200)
    guidance: float = Field(default=4.0, ge=0, le=50)
    seed: int = Field(default=0, ge=0, le=2**32 - 1)
    speed: bool = False
    images_b64: list[EncodedImage] = Field(min_length=1, max_length=3)
    client_job_id: str = Field(default="", max_length=128)


class InpaintReq(BaseModel):
    prompt: Prompt
    negative_prompt: Prompt = ""
    steps: int = Field(default=30, ge=1, le=200)
    guidance: float = Field(default=4.0, ge=0, le=50)
    seed: int = Field(default=0, ge=0, le=2**32 - 1)
    speed: bool = False
    image_b64: EncodedImage
    mask_b64: EncodedImage
    strength: float = Field(default=0.75, ge=0.05, le=1.0)
    padding_mask_crop: int | None = Field(default=None, ge=0, le=512)
    client_job_id: str = Field(default="", max_length=128)


class VideoReq(BaseModel):
    prompt: Prompt
    negative_prompt: Prompt = ""
    num_frames: int = Field(default=49, ge=1, le=1001)
    steps: int = Field(default=40, ge=1, le=200)
    guidance: float = Field(default=5.0, ge=0, le=50)
    fps: int = Field(default=20, ge=1, le=120)
    seed: int = Field(default=0, ge=0, le=2**32 - 1)
    resolution: str = Field(default="720p", max_length=16)
    orientation: str = Field(default="portrait", max_length=16)
    image_b64: EncodedImage | None = None
    last_image_b64: EncodedImage | None = None  # Wan FLF2V, when supported
    engine: str = Field(default="wan", max_length=16)  # wan | hunyuan | ltx
    speed: bool = False
    client_job_id: str = Field(default="", max_length=128)






@app.get("/health")
def health(x_gen_secret: str | None = Header(default=None)):
    _auth(x_gen_secret)
    name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
    worker_alive = bool(_WORKER["thread"] and _WORKER["thread"].is_alive())
    # `features` is the contract the backend gates its UI on, so it must describe
    # what this session can ACTUALLY do. Speed mode is listed only when a
    # Lightning LoRA is configured; whether it truly loaded is per-pipe and only
    # known after that pipe exists, so `speed_loaded` reports the live state and
    # the generate path refuses a speed request the pipe cannot honour.
    features = ["preview", "cancel", "cancel_client", "edit", "ack"]
    if pipelines._supports_flf2v(config.VIDEO_MODEL):
        features.append("flf2v")
    if config.IMAGE_LIGHTNING_LORA:
        features.append("speed_image")
    if config.EDIT_LIGHTNING_LORA:
        features.append("speed_edit")
    if pipelines._edit_inpaint_cls() is not None:
        features.append("edit_inpaint")
    if config.VIDEO_LIGHTNING_LORA:
        features.append("speed_video")
    if config.HUNYUAN_VIDEO_MODEL:
        features.append("hunyuan")
    if config.LTX_VIDEO_MODEL:
        features.append("ltx")
    if config.IMAGE_MODEL_ALT:
        features.append("image_alt")
    if config.IMAGE_MODEL_HIDREAM:
        features.append("image_hidream")
    loaded_models = []
    if pipelines._PIPES.get("image") is not None:
        _variant = str(pipelines._PIPES.get("image_variant") or "quality")
        loaded_models.append(pipelines.image_model_for(_variant))
    if pipelines._PIPES.get("edit") is not None:
        loaded_models.append(config.EDIT_MODEL)
    if pipelines._PIPES.get("video") is not None:
        video_engine = str(pipelines._PIPES.get("video_engine") or "wan")
        loaded_models.append({"hunyuan": config.HUNYUAN_VIDEO_MODEL,
                              "ltx": config.LTX_VIDEO_MODEL}.get(video_engine, config.VIDEO_MODEL))
    configured_models = [
        (config.IMAGE_MODEL, "Image", "image"),
        (config.IMAGE_MODEL_HIDREAM, "HiDream image", "image"),
        (config.IMAGE_MODEL_ALT, "Alternate image", "image"),
        (config.EDIT_MODEL, "Image edit", "image-edit"),
        (config.VIDEO_MODEL, "Wan video", "video"),
        (config.HUNYUAN_VIDEO_MODEL, "Hunyuan video", "video"),
        (config.LTX_VIDEO_MODEL, "LTX video", "video"),
    ]
    return {
        # ok reflects the thing that actually runs jobs, not merely "HTTP works".
        "ok": worker_alive,
        "worker_alive": worker_alive,
        "queue_depth": _TASKQ.qsize(),
        # Surfaced so the app can warn before a job triggers a download that
        # cannot fit. An 80 GB-class runtime holds far less than the largest video
        # checkpoints, and the failure without this is a long download that dies
        # partway and takes the resident pipeline with it.
        "disk_free_gb": round(runtime.free_disk_gb(), 1),
        "gpu": name,
        "image_model": config.IMAGE_MODEL,
        "image_model_hidream": config.IMAGE_MODEL_HIDREAM,
        "image_model_alt": config.IMAGE_MODEL_ALT,
        # Which one is resident right now, so the app can warn that a switch
        # costs a reload before the user queues one.
        "image_variant": pipelines._PIPES.get("image_variant"),
        "video_model": config.VIDEO_MODEL,
        "edit_model": config.EDIT_MODEL,
        "models_loaded": loaded_models,
        "models": [
            {"id": model, "label": label, "kind": kind,
             "ready": model in loaded_models,
             "status": "ready" if model in loaded_models else "unknown"}
            for model, label, kind in configured_models if model
        ],
        "image_loaded": pipelines._PIPES["image"] is not None,
        "video_mode": pipelines._PIPES["video_mode"],
        "features": features,
        "build": build_id(),
        "speed_loaded": {
            "image": bool(pipelines._PIPES.get("image_speed")),
            "edit": bool(pipelines._PIPES.get("edit_speed")),
            "video": bool(pipelines._PIPES.get("video_speed")),
        },
    }


def _cfg_call(sig, call: dict, guidance: float, negative: str) -> None:
    """Wire negative + CFG into the call the way this pipeline expects.
    Qwen-Image uses true_cfg_scale for real CFG (guidance_scale stays at 1.0)."""
    if negative and "negative_prompt" in sig:
        call["negative_prompt"] = negative
    if "true_cfg_scale" in sig:
        call["true_cfg_scale"] = guidance
        call["guidance_scale"] = 1.0
    else:
        call["guidance_scale"] = guidance


@app.post("/image")
def image(req: ImageReq, x_gen_secret: str | None = Header(default=None)):
    _auth(x_gen_secret)

    def run(token):

        _raise_if_canceled(token)
        pipe = pipelines.get_image_pipe(req.model_variant)
        if req.speed:
            pipelines._require_speed("image")
        pipelines._set_speed(pipe, req.speed)
        sig = inspect.signature(pipe.__call__).parameters
        prompts = req.prompts or [req.prompt]
        negatives = req.negative_prompts or [req.negative_prompt]
        seeds = req.seeds or [req.seed]
        n = max(len(prompts), len(negatives), len(seeds))
        total = max(1, req.steps * n)
        images: list[str] = []
        for i in range(n):
            if _JOBS[token].get("cancel"):
                break
            p = prompts[i] if i < len(prompts) else prompts[-1]
            negative = negatives[i] if i < len(negatives) else negatives[-1]
            sd = seeds[i] if i < len(seeds) else seeds[-1]
            gen = torch.Generator(device="cpu").manual_seed(int(sd))

            def cb(_pipe, step, _t, kw, _i=i):
                if _JOBS[token].get("cancel"):
                    _pipe._interrupt = True
                _JOBS[token]["progress"] = (_i * req.steps + step + 1) / total
                if config.PREVIEW_EVERY and (step + 1) % config.PREVIEW_EVERY == 0:
                    _JOBS[token]["preview"] = _latent_preview(_pipe, kw, req.width, req.height)
                return kw

            call = {"prompt": p, "num_inference_steps": req.steps, "width": req.width, "height": req.height,
                        "generator": gen, "callback_on_step_end": cb}
            _cfg_call(sig, call, req.guidance, negative)
            out = pipe(**call)
            buf = io.BytesIO()
            out.images[0].save(buf, format="PNG")
            images.append(base64.b64encode(buf.getvalue()).decode())
            del out
            pipelines.free_memory()  # free per image so a large batch can't accumulate to an OOM
        if not images:
            raise RuntimeError("canceled before any image finished")
        return {"images_b64": images, "image_b64": images[0]}

    return _enqueue(run, req.client_job_id)


@app.post("/edit")
def edit(req: EditReq, x_gen_secret: str | None = Header(default=None)):
    _auth(x_gen_secret)
    if not req.images_b64:
        raise HTTPException(status_code=400, detail="images_b64 required")

    def run(token):

        _raise_if_canceled(token)
        pipe = pipelines.get_edit_pipe()
        if req.speed:
            pipelines._require_speed("edit")
        pipelines._set_speed(pipe, req.speed)
        sig = inspect.signature(pipe.__call__).parameters
        imgs = [ImageOps.exif_transpose(pipelines._decode_image(b)).convert("RGB")
                for b in req.images_b64[:3]]
        gen = torch.Generator(device="cpu").manual_seed(int(req.seed))
        # Edit-Plus pipelines take a list; older single-image edit pipelines take one.
        image_arg = imgs if len(imgs) > 1 else imgs[0]
        call = {"image": image_arg, "prompt": req.prompt, "num_inference_steps": req.steps,
                    "generator": gen,
                    "callback_on_step_end": _progress_cb(token, req.steps, imgs[0].width, imgs[0].height)}
        _cfg_call(sig, call, req.guidance, req.negative_prompt)
        out = pipe(**call)
        buf = io.BytesIO()
        out.images[0].save(buf, format="PNG")
        del out
        pipelines.free_memory()
        return {"image_b64": base64.b64encode(buf.getvalue()).decode()}

    return _enqueue(run, req.client_job_id)


@app.post("/inpaint")
def inpaint(req: InpaintReq, x_gen_secret: str | None = Header(default=None)):
    _auth(x_gen_secret)

    def run(token):

        _raise_if_canceled(token)
        plus = pipelines.get_edit_pipe()
        pipe = pipelines.get_edit_inpaint_pipe()
        if req.speed:
            pipelines._require_speed("edit")
        pipelines._set_speed(plus, req.speed)
        pipelines._set_speed(pipe, req.speed)
        sig = inspect.signature(pipe.__call__).parameters
        image = ImageOps.exif_transpose(pipelines._decode_image(req.image_b64)).convert("RGB")
        mask = ImageOps.exif_transpose(pipelines._decode_image(req.mask_b64)).convert("L")
        if mask.size != image.size:
            mask = mask.resize(image.size, Image.Resampling.NEAREST)
        gen = torch.Generator(device="cpu").manual_seed(int(req.seed))
        call = {
            "image": image, "mask_image": mask, "prompt": req.prompt,
            "num_inference_steps": req.steps, "generator": gen,
            "callback_on_step_end": _progress_cb(token, req.steps, image.width, image.height),
        }
        if "strength" in sig:
            call["strength"] = float(req.strength)
        if req.padding_mask_crop is not None and "padding_mask_crop" in sig:
            call["padding_mask_crop"] = int(req.padding_mask_crop)
        _cfg_call(sig, call, req.guidance, req.negative_prompt)
        out = pipe(**call)
        buf = io.BytesIO()
        out.images[0].save(buf, format="PNG")
        del out
        pipelines.free_memory()
        return {"image_b64": base64.b64encode(buf.getvalue()).decode()}

    return _enqueue(run, req.client_job_id)


def _video_run(req: VideoReq, mode: str):
    def run(token):

        _raise_if_canceled(token)
        pipe = pipelines.get_video_pipe(mode, req.engine)
        if req.engine != "hunyuan":
            if req.speed:
                pipelines._require_speed("video")
            pipelines._set_speed(pipe, req.speed)
        sig = inspect.signature(pipe.__call__).parameters

        if mode == "i2v":
            # The /i2v route checks this too, but the invariant belongs where the
            # value is used: this closure runs on the worker thread, long after
            # the request handler returned, and nothing else re-establishes it.
            if not req.image_b64:
                raise HTTPException(status_code=400, detail="image_b64 required for i2v")
            img = ImageOps.exif_transpose(pipelines._decode_image(req.image_b64))
            orient = ("portrait" if img.height > img.width
                      else ("square" if img.height == img.width else "landscape"))
            w, h = pipelines.res_for(req.resolution, orient)
            first = pipelines.fit_image(img, w, h)
        else:
            w, h = pipelines.res_for(req.resolution, req.orientation)
            first = None

        neg = req.negative_prompt or (pipelines._wan_negative() if req.engine != "hunyuan" else "")
        gen = torch.Generator(device="cpu").manual_seed(int(req.seed))
        call = {"prompt": req.prompt, "height": h, "width": w, "num_frames": req.num_frames,
                    "guidance_scale": req.guidance, "num_inference_steps": req.steps, "generator": gen,
                    "callback_on_step_end": _progress_cb(token, req.steps, w, h)}
        if neg and "negative_prompt" in sig:
            call["negative_prompt"] = neg
        if first is not None:
            call["image"] = first
        # FLF2V: pass the last frame only when this pipeline actually supports it.
        if req.last_image_b64:
            # Refuse rather than drop. The frame used to disappear here whenever
            # the model could not use it, and the user got an ordinary i2v with
            # no hint that half their input was discarded.
            if not (pipelines._supports_flf2v(config.VIDEO_MODEL) and "last_image" in sig):
                raise HTTPException(
                    status_code=409,
                    detail=(f"{config.VIDEO_MODEL} does not support first+last-frame "
                            "conditioning: it has no image_encoder, so a last frame "
                            "has nothing to condition. Use a Wan FLF2V checkpoint, "
                            "or drop the last frame."),
                )
            call["last_image"] = pipelines.fit_image(pipelines._decode_image(req.last_image_b64), w, h)
        frames = pipe(**call).frames[0]
        out = pipelines._encode_video(frames, req.fps)
        pipelines.free_memory()
        return {"video_b64": out}

    return run


@app.post("/t2v")
def t2v(req: VideoReq, x_gen_secret: str | None = Header(default=None)):
    _auth(x_gen_secret)
    return _enqueue(_video_run(req, "t2v"), req.client_job_id)


@app.post("/i2v")
def i2v(req: VideoReq, x_gen_secret: str | None = Header(default=None)):
    _auth(x_gen_secret)
    if not req.image_b64:
        raise HTTPException(status_code=400, detail="image_b64 required for i2v")
    return _enqueue(_video_run(req, "i2v"), req.client_job_id)


def busy() -> bool:
    """Whether work is in flight, for the idle switch.

    A queued or running job counts even when its client has gone quiet, so a
    long render is never interrupted by the thing meant to stop idle billing.
    """
    if _TASKQ.qsize():
        return True
    return any(job.get("status") in ("queued", "running") for job in _JOBS.values())
