"""Image generation endpoints: local model (txt2img/img2img/inpaint) + Remote GPU.

Requests are multipart so img2img/inpaint can attach files; pure-text generators
send only the `payload` JSON field. Each endpoint enqueues a worker job.
"""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, UploadFile
from PIL import Image

from ..config import settings
from ..generators.base import batch_for, dims_for, dims_for_ratio
from ..generators.schedulers import OPTIONS as SAMPLER_OPTIONS
from ..loras import sanitize as sanitize_loras
from ..modelprobe import family_of_model
from ..models import JobKind
from ..outpaint import DEFAULT_PCT, DIRECTIONS, MAX_PCT, MIN_PCT, OUTPAINT_STRENGTH
from ..presets import QUALITY_STEPS, quality_steps
from ..prompt_engine import count_variants, expand_all, resolve_negative
from ..queue import ProgressCb
from ..utils.seeds import resolve_seed
from .common import (
    MAX_PROMPT,
    apply_image_post,
    as_float,
    as_int,
    image_meta,
    image_progress_window,
    item_seed_prompts,
    normalize_request_id,
    parse_payload,
    persist_image,
    register_handler,
    require_upload,
    resolve_finish,
    save_upload,
    submit,
    submit_fanout,
)

router = APIRouter(tags=["images"])

# kind -> (device for sizing, step group, default guidance, default steps).
_PROFILE = {
    "image_local": ("local", "local", 1.0, 9),
    "img2img": ("local", "local", 1.0, 9),
    "inpaint": ("local", "local", 1.0, 9),
    "outpaint": ("local", "local", 1.0, 9),
    "image_colab": ("a100", "a100", 4.0, 30),
    "image_edit": ("a100", "a100", 4.0, 30),
}


def _require_local_gpu() -> None:
    """Reject only a completed negative probe; unknown startup state may queue."""
    from ..backends.local import unavailable_reason

    if reason := unavailable_reason():
        raise HTTPException(status_code=503, detail=reason)


def _resolved_variant(p: dict, device: str) -> str:
    """The variant name this request actually resolved to, for persistence."""
    from ..generators import variants

    return variants.get(p.get("model_variant"),
                        lane="local" if device == "local" else "colab").name


def _require_model_access(params: dict) -> None:
    """Refuse a cold gated fetch before uploads are copied or a job is queued."""
    from ..generators import variants

    if reason := variants.access_reason(params.get("model_variant"), lane="local"):
        raise HTTPException(status_code=409, detail=reason)


def _common_params(p: dict, kind: str) -> dict:
    device, group, def_guidance, def_steps = _PROFILE[kind]
    # Each local model has its own step tier and CFG regime — a distilled 8-step
    # model and a real-CFG one cannot share either. The catalogue in
    # generators/variants.py holds both, so this is a lookup rather than a chain
    # of per-model branches that has to be extended for every new model.
    #
    # def_guidance only applies when the caller omits `guidance` entirely, which
    # the UI never does — it ships the variant-aware value via the control's
    # defaults_by map. This stays as the floor for API clients that send neither.
    # The a100 lane is one device serving whatever checkpoint is configured, and
    # SDXL needs different numbers from Qwen-Image on every axis: area, steps and
    # CFG. Resolved from the model's own family rather than assumed.
    family = ""
    if device == "a100":
        from ..generators import variants

        # image_edit has no picker — it is a separate model — so it resolves to
        # the lane default and behaves exactly as before.
        remote_variant = variants.get(p.get("model_variant"), lane="colab")
        family = family_of_model(variants.repo_of(remote_variant))
        if kind == "image_colab":
            group, def_guidance, def_steps = (remote_variant.steps_group,
                                              remote_variant.guidance,
                                              remote_variant.default_steps)
        # A family row still wins over the variant's declared tier: an SDXL
        # checkpoint configured into either slot needs SDXL's numbers.
        if f"a100_{family}" in QUALITY_STEPS:
            group, def_guidance, def_steps = f"a100_{family}", 3.5, 30
    if group == "local":
        from ..generators import variants

        variant = variants.get(p.get("model_variant"), lane="local")
        group, def_guidance, def_steps = (
            variant.steps_group, variant.guidance, variant.default_steps)
        # The local lane serves several architectures now, so it needs the same
        # family-aware sizing the Remote GPU lane does — an SDXL checkpoint must keep
        # its ~1.05 MP bucket rather than inherit the card's DiT-shaped budget.
        family = family_of_model(variants.repo_of(variant))
    # Lightning speed mode (Remote GPU): distilled 4-step regime, CFG 1, no negative.
    speed = bool(p.get("speed_mode", False)) and group == "a100"
    if speed:
        def_guidance, def_steps = 1.0, 4
        p = {**p, "quality": "Custom", "steps": min(int(p.get("steps", 4) or 4), 8),
             "guidance": 1.0, "auto_negative": False, "negative_prompt": ""}
    finish, post_detail, post_face, post_upscale = resolve_finish(p)
    quality = p.get("quality", "Standard")
    tier = quality if quality in ("Draft", "Standard", "High") else "Standard"
    custom_steps = as_int(p.get("steps"), def_steps, 1, 100)
    steps = custom_steps if quality == "Custom" else quality_steps(group, quality, def_steps)
    # Explicit width/height only apply when the aspect picker is on "Custom" — otherwise
    # a preset aspect always wins (a stale custom W/H left in the form must not leak through).
    # Inpaint always returns the source canvas: an aspect picker would imply it
    # can change framing while simultaneously promising to preserve unmasked
    # pixels, which is impossible. Img2img may still deliberately crop.
    aspect = "Match input" if kind == JobKind.inpaint.value else p.get("aspect", "1:1")
    if aspect == "Custom":
        cw, ch = as_int(p.get("width"), 0, 0, 8192), as_int(p.get("height"), 0, 0, 8192)
        if not cw and not ch:
            cw = ch = 1024
        cw, ch = cw or ch, ch or cw  # if only one given, square off the provided side
        width, height = dims_for(device=device, width=cw, height=ch, family=family)
    else:
        width, height = dims_for(aspect=aspect, tier=tier, device=device, family=family)
    result = {
        "request_id": normalize_request_id(p.get("request_id")),
        "prompt": (p.get("prompt", "") or "")[:MAX_PROMPT],
        "raw_prompt": (p.get("raw_prompt", p.get("prompt", "")) or "")[:MAX_PROMPT],
        "style_ids": [str(x) for x in (p.get("style_ids") or []) if isinstance(x, str)][:32],
        "negative_prompt": resolve_negative(kind, p.get("negative_prompt", ""),
                                            bool(p.get("auto_negative", False)))[:MAX_PROMPT],
        "quality": quality,
        # Validated here rather than in the handler: params are persisted and
        # replayed on rerun, so a path that escaped LORA_DIR would be replayed
        # too. sanitize() drops anything that no longer resolves.
        "loras": sanitize_loras(p.get("loras")) if device == "local" else [],
        # Unknown values are rejected in schedulers.apply(); normalise here so a
        # stale draft cannot smuggle one through to a rerun.
        "sampler": (str(p.get("sampler", "default")).lower()
                    if str(p.get("sampler", "default")).lower() in SAMPLER_OPTIONS else "default"),
        "steps": max(1, min(steps, 100)),
        "guidance": as_float(p.get("guidance"), def_guidance, 0.0, 30.0),
        "seed": resolve_seed(p.get("seed")),
        "seed_mode": p.get("seed_mode", "increment"),
        # Capped by a pixel budget, not a flat count: a High-tier 16:9 batch of
        # 8 is roughly four times the VRAM of a Draft 1:1 batch of 8, and
        # treating them as equal is how a batch that worked yesterday OOMs today
        # because the aspect changed.
        "batch": batch_for(width, height, device=device,
                           requested=as_int(p.get("batch"), 1, 1, 8)),
        "width": width, "height": height, "aspect": aspect,
        "strength": as_float(p.get("strength"), 0.6, 0.05, 1.0),
        # One "Finishing" choice in the UI, expanded to the flags the pipeline
        # reads. Both are persisted: the preset so the form round-trips on
        # rerun, the flags so the job still records precisely what ran.
        "finish": finish,
        "post_upscale": post_upscale,
        "post_face": post_face,
        "post_scale": as_int(p.get("post_scale"), 4, 2, 4),
        "post_detail": post_detail,
        "detail_prompt": (p.get("detail_prompt", "") or "")[:MAX_PROMPT],
        "combinatorial": bool(p.get("combinatorial", False)),
        "speed_mode": speed,
        # New forms state their syntax. Legacy rows lack this key and handlers
        # interpret absence as literal, so old parentheses keep their meaning.
        "prompt_syntax": (str(p.get("prompt_syntax"))
                          if p.get("prompt_syntax") in {"a1111", "literal"} else "literal"),
        # The resolved name, not the raw request: an unknown or unconfigured
        # variant falls back, and storing the unresolved string would let a
        # rerun pick a different model than the run it is reproducing.
        "model_variant": _resolved_variant(p, device),
    }
    if kind == JobKind.inpaint.value:
        area = str(p.get("inpaint_area", "masked area"))
        result.update({
            "inpaint_area": area if area in {"masked area", "whole image"} else "masked area",
            "mask_grow": as_int(p.get("mask_grow"), 4, 0, 128),
            "mask_padding": as_int(p.get("mask_padding"), 64, 0, 512),
            "mask_blur": as_int(p.get("mask_blur"), 4, 0, 64),
        })
    return result


def _match_input_dimensions(params: dict, image: Image.Image, model: str) -> tuple[int, int]:
    """Resolve a source ratio into this model's quality-aware pixel bucket."""
    if params.get("aspect") != "Match input":
        return int(params.get("width") or 0), int(params.get("height") or 0)
    quality = str(params.get("quality", "Standard"))
    tier = quality if quality in {"Draft", "Standard", "High"} else "Standard"
    width, height = dims_for_ratio(
        image.width, image.height, tier=tier, device="local", family=family_of_model(model),
    )
    params["width"], params["height"] = width, height
    return width, height


def _size_uploaded_input(params: dict, path: str) -> None:
    """Persist truthful match-input dimensions before the job is queued."""
    if params.get("aspect") != "Match input":
        return
    from ..generators import variants

    model = variants.repo_of(variants.get(params.get("model_variant"), lane="local"))
    with Image.open(path) as uploaded:
        _match_input_dimensions(params, uploaded, model)


# ---- local image ----------------------------------------------------------
def _local_handler(mode: str):
    def handler(job_id: int, params: dict, cb: ProgressCb) -> dict:
        from ..generators import local_image
        from ..queue import SkipItem, load_reporter

        image = Image.open(params["image_path"]).convert("RGB") if params.get("image_path") else None
        mask = Image.open(params["mask_path"]).convert("L") if params.get("mask_path") else None
        model = local_image.resolve_model(params.get("model_variant"))
        edit_plan = None
        # Missing inpaint_area means this is a legacy queued/rerun job. Preserve
        # its old cover-fit/output behavior exactly. Every new request records
        # the field and gets non-destructive source compositing.
        if mode == "inpaint" and "inpaint_area" in params and image is not None and mask is not None:
            from ..inpaint import plan_inpaint

            edit_plan = plan_inpaint(
                image, mask, regional=params.get("inpaint_area") == "masked area",
                padding=int(params.get("mask_padding", 64)),
                grow=int(params.get("mask_grow", 4)),
            )
            image, mask = edit_plan.input_image, edit_plan.input_mask
        asset_ids = []
        batch = max(1, int(params.get("batch", 1)))
        # width/height are 0 for legacy jobs → local_image falls back to res_for().
        w, h = int(params.get("width") or 0), int(params.get("height") or 0)
        if edit_plan is not None and edit_plan.regional:
            w, h = _match_input_dimensions(
                {**params, "aspect": "Match input"}, edit_plan.input_image, model)
        elif image is not None:
            w, h = _match_input_dimensions(params, image, model)
        # Legacy queued jobs and replayed params may still have `steps=None` or
        # `guidance=None` from older saved rows. Coerce them before casting so a
        # restart can resume the queue instead of immediately cratering every job.
        default_steps = 9
        default_guidance = 1.0
        safe_steps = params.get("steps")
        safe_guidance = params.get("guidance")
        if safe_steps is None:
            safe_steps = default_steps
        if safe_guidance is None:
            safe_guidance = default_guidance
        for i in range(batch):
            item_start, denoise_end, item_end = image_progress_window(params, i, batch)
            cb(item_start, f"image {i + 1}/{batch}")  # also the per-item cancellation point
            seed, prompt, negative = item_seed_prompts(params, i)
            # Per item, not per job: a batch can legitimately differ if an
            # adapter file disappears midway through it.
            warnings: list[str] = []
            metrics: dict[str, float] = {}
            try:
                img = local_image.generate(
                    mode=mode, prompt=prompt, negative_prompt=negative,
                    steps=int(safe_steps), guidance=float(safe_guidance),
                    seed=seed, width=w, height=h,
                    resolution=params.get("resolution", "720p"),
                    orientation=params.get("orientation", "portrait"),
                    image=image, mask=mask, strength=float(params.get("strength", 0.6)),
                    model=model, loras=params.get("loras") or [],
                    sampler=params.get("sampler", "default"),
                    report=load_reporter(job_id, mode),
                    warnings=warnings,
                    metrics=metrics,
                    progress_cb=lambda f, m, preview=None, _start=item_start, _end=denoise_end: cb(
                        _start + (_end - _start) * f, m, preview=preview),
                )
            except SkipItem:
                continue  # user skipped this batch item — move on to the next
            # Size BEFORE post-processing: that is what a rerun should request.
            # image_meta otherwise records the upscaled dimensions, and "reuse
            # all" feeds them back as generation dims.
            gen_w, gen_h = img.size
            if edit_plan is not None:
                img = edit_plan.composite(img, blur=int(params.get("mask_blur", 4)))
            img, post = apply_image_post(
                img, params, cb, start=denoise_end, end=item_end, metrics=metrics)
            effective_steps = local_image.effective_step_count(
                mode, int(safe_steps), float(params.get("strength", 0.6)), model)
            edit_meta = ({
                "inpaint_area": params.get("inpaint_area"),
                "mask_grow": params.get("mask_grow"),
                "mask_padding": params.get("mask_padding"),
                "mask_blur": params.get("mask_blur"),
                "canvas_width": edit_plan.source.width,
                "canvas_height": edit_plan.source.height,
            } if edit_plan is not None else {})
            meta = image_meta(params, img, prompt=prompt, seed=seed,
                              negative_prompt=negative,
                              width=gen_w, height=gen_h, post=post,
                              aspect=params.get("aspect", ""), mode=mode, model=model,
                              loras=params.get("loras") or [],
                              sampler=params.get("sampler", "default"),
                              effective_steps=effective_steps,
                              **metrics,
                              **edit_meta,
                              warnings=warnings)
            asset_ids.append(persist_image(img, job_id=job_id, generator=f"local_image:{mode}",
                                            meta=meta, tag=mode, params=params))
        return {"asset_ids": asset_ids}

    return handler


async def _maybe_fanout(kind: str, params: dict, handler) -> dict | None:
    """Combinatorial toggle: queue one job per {a|b|c} combination (batch 1 each)."""
    if not params.get("combinatorial"):
        return None
    if count_variants(params.get("prompt", "")) <= 1:
        return None
    variants = [{"prompt": v, "batch": 1, "combinatorial": False}
                for v in expand_all(params.get("prompt", ""), cap=32)]
    return await submit_fanout(kind, params, handler, variants)


@router.post("/generate/image/local")
async def gen_local(payload: str = Form(...)) -> dict:
    _require_local_gpu()
    params = _common_params(parse_payload(payload), JobKind.image_local.value)
    _require_model_access(params)
    fanned = await _maybe_fanout(JobKind.image_local.value, params, _local_handler("txt2img"))
    return fanned or await submit(JobKind.image_local.value, params, _local_handler("txt2img"))


@router.post("/generate/image/img2img")
async def gen_img2img(payload: str = Form(...), image: UploadFile | None = None) -> dict:
    _require_local_gpu()
    params = _common_params(parse_payload(payload), JobKind.img2img.value)
    _require_model_access(params)
    params["image_path"] = require_upload(await save_upload(image))
    _size_uploaded_input(params, params["image_path"])
    return await submit(JobKind.img2img.value, params, _local_handler("img2img"))


@router.post("/generate/image/inpaint")
async def gen_inpaint(payload: str = Form(...), image: UploadFile | None = None,
                      mask: UploadFile | None = None) -> dict:
    _require_local_gpu()
    params = _common_params(parse_payload(payload), JobKind.inpaint.value)
    _require_model_access(params)
    params["image_path"] = require_upload(await save_upload(image))
    params["mask_path"] = require_upload(await save_upload(mask), "mask image")
    _size_uploaded_input(params, params["image_path"])
    return await submit(JobKind.inpaint.value, params, _local_handler("inpaint"))


# ---- outpainting (local; inpaint on an extended canvas) -------------------
def _outpaint_handler(job_id: int, params: dict, cb: ProgressCb) -> dict:
    from ..generators import local_image
    from ..outpaint import build, composite, plan, refine_priming
    from ..queue import SkipItem, load_reporter

    src = Image.open(params["image_path"]).convert("RGB")
    # Old queued rows can contain explicit nulls. dict.get(default) does not use
    # its default when the key exists with None, so normalize once before any
    # casts and let restart/resume run the same safe values as a fresh request.
    expand_pct = as_int(params.get("expand_pct"), DEFAULT_PCT, MIN_PCT, MAX_PCT)
    steps = as_int(params.get("steps"), 9, 1, 100)
    guidance = as_float(params.get("guidance"), 1.0, 0.0, 30.0)
    strength = as_float(params.get("strength"), OUTPAINT_STRENGTH, 0.05, 1.0)
    p = plan(src.width, src.height, params.get("direction", "all"), expand_pct)
    canvas, mask = build(src, p)
    # Continue image structure into the new area rather than smearing the border.
    # Best effort: falls back to build()'s canvas if OpenCV is unavailable.
    canvas = refine_priming(canvas, p)
    model = local_image.resolve_model(params.get("model_variant"))
    # The final canvas follows the source, but diffusion still has to stay in
    # the selected model's safe pixel bucket. Generate a proportional working
    # canvas and scale only the invented surroundings back; composite() restores
    # the original source pixels at their native resolution afterwards.
    from ..generators.base import dims_for
    from ..modelprobe import family_of_model

    work_w, work_h = dims_for(
        device="local", tier=str(params.get("quality") or "Standard"),
        width=p.canvas_w, height=p.canvas_h, family=family_of_model(model),
    )
    asset_ids = []
    batch = max(1, int(params.get("batch", 1)))

    for i in range(batch):
        item_start, denoise_end, item_end = image_progress_window(params, i, batch)
        cb(item_start, f"extending {i + 1}/{batch}")
        seed, prompt, negative = item_seed_prompts(params, i)
        warnings: list[str] = []
        metrics: dict[str, float] = {}
        try:
            img = local_image.generate(
                mode="inpaint", prompt=prompt,
                negative_prompt=negative,
                steps=steps, guidance=guidance,
                seed=seed, width=work_w, height=work_h,
                image=canvas, mask=mask,
                # Measured, not assumed. The first implementation pinned this at
                # 0.95 on the theory that outpainting always paints new territory
                # so the primed canvas is worthless. That was wrong, and it was
                # the actual cause of the visible rectangle around the source: at
                # 0.95 the model ignores the edge-extended priming and invents its
                # own exposure. A sweep put the worst-channel seam at 3.2 levels
                # for 0.65, 4.8 for 0.80 and -15.6 for 0.95 — the cliff sits
                # between 0.80 and 0.95, and 0.65 produces a genuinely continuous
                # image. The priming smear is tonal guidance for the model, not
                # content to be preserved.
                strength=strength,
                model=model, loras=params.get("loras") or [],
                sampler=params.get("sampler", "default"),
                report=load_reporter(job_id, "outpaint"), warnings=warnings, metrics=metrics,
                progress_cb=lambda f, m, preview=None, _start=item_start, _end=denoise_end: cb(
                    _start + (_end - _start) * f, m, preview=preview),
            )
        except SkipItem:
            continue
        # Restore the true source pixels before any post-processing, so an
        # upscale or detail pass works on the composited image rather than on
        # the VAE's slightly-shifted copy of the original.
        if img.size != (p.canvas_w, p.canvas_h):
            img = img.resize((p.canvas_w, p.canvas_h), Image.Resampling.LANCZOS)
        img = composite(img, src, p)
        gen_w, gen_h = img.size
        img, post = apply_image_post(
            img, params, cb, start=denoise_end, end=item_end, metrics=metrics)
        effective_steps = local_image.effective_step_count(
            "inpaint", steps, strength, model)
        meta = image_meta(params, img, prompt=prompt, seed=seed, aspect="Custom",
                          negative_prompt=negative,
                          width=gen_w, height=gen_h, post=post,
                          mode="outpaint", model=model, loras=params.get("loras") or [],
                          sampler=params.get("sampler", "default"),
                          direction=params.get("direction", "all"),
                          expand_pct=expand_pct,
                          effective_steps=effective_steps,
                          **metrics,
                          warnings=warnings)
        asset_ids.append(persist_image(img, job_id=job_id, generator="local_image:outpaint",
                                       meta=meta, tag="outpaint", params=params))
    return {"asset_ids": asset_ids}


@router.post("/generate/image/outpaint")
async def gen_outpaint(payload: str = Form(...), image: UploadFile | None = None) -> dict:
    _require_local_gpu()
    raw = parse_payload(payload)
    params = _common_params(raw, JobKind.outpaint.value)
    _require_model_access(params)
    params["image_path"] = require_upload(await save_upload(image))
    params["direction"] = (raw.get("direction") if raw.get("direction") in DIRECTIONS else "all")
    params["expand_pct"] = max(MIN_PCT, min(as_int(raw.get("expand_pct"), DEFAULT_PCT, MIN_PCT, MAX_PCT),
                                            MAX_PCT))
    return await submit(JobKind.outpaint.value, params, _outpaint_handler)


# ---- Remote GPU image (batched in a single round-trip) ----------------------
def _remote_image_handler(job_id: int, params: dict, cb: ProgressCb) -> dict:
    from ..generators.base import res_for
    from ..remote_gpu_client import decode_remote_image, run_remote

    batch = max(1, int(params.get("batch", 1)))
    w, h = int(params.get("width") or 0), int(params.get("height") or 0)
    if not (w and h):  # legacy job: derive from stored resolution/orientation
        w, h = res_for(params.get("resolution", "720p"), params.get("orientation", "portrait"))
    pairs = [item_seed_prompts(params, i) for i in range(batch)]
    seeds = [seed for seed, _prompt, _negative in pairs]
    prompts = [prompt for _seed, prompt, _negative in pairs]
    negatives = [negative for _seed, _prompt, negative in pairs]
    cb(0.05, f"A100 generating {batch} image(s) …")
    from ..generators import variants

    # Which configured remote image model to run. Resolved here rather than
    # passed through raw so a stored job names a variant the worker knows,
    # and recorded in the metadata below so the asset says what made it.
    remote_variant = variants.get(params.get("model_variant"), lane="colab")
    required_feature = "image_alt" if remote_variant.name == "alt" else None
    if required_feature:
        # This handler executes in the queue's worker thread, so a synchronous
        # refresh is safe here.  Do not trust a possibly empty startup cache:
        # older workers fall back from an unknown variant to their primary
        # model, which would make a custom-slot test falsely look successful.
        import asyncio

        from ..remote_gpu_client import remote_gpu_health

        health = asyncio.run(remote_gpu_health())
        if not health.get("connected") or required_feature not in set(health.get("features") or []):
            raise RuntimeError(
                f"The connected A100 does not advertise its {remote_variant.name} image slot. "
                "Restart the Remote GPU worker with the regenerated runner before queuing this model."
            )
    resp = run_remote("/image", {
        "model_variant": remote_variant.name,
        "prompt": prompts[0], "prompts": prompts,
        "negative_prompt": negatives[0], "negative_prompts": negatives,
        "steps": int(params.get("steps", 30)), "guidance": float(params.get("guidance", 4.0)),
        "seed": seeds[0], "seeds": seeds, "width": w, "height": h,
        "speed": bool(params.get("speed_mode", False)),
    }, progress_cb=lambda f, m, preview=None: cb(0.05 + 0.8 * f, m, preview=preview),
        job_id=job_id)

    images_b64 = resp.get("images_b64") or [resp["image_b64"]]
    if not isinstance(images_b64, list) or not images_b64 or len(images_b64) > batch:
        raise RuntimeError("Remote GPU returned an invalid image batch")
    asset_ids = []
    for i, b64 in enumerate(images_b64):
        cb(0.85 + 0.1 * (i / max(1, len(images_b64))), f"saving {i + 1}/{len(images_b64)}")
        raw = decode_remote_image(b64, max_side=max(w, h), max_pixels=w * h)
        gen_w, gen_h = raw.size
        post_start = 0.85 + 0.15 * (i / max(1, len(images_b64)))
        post_end = 0.85 + 0.15 * ((i + 1) / max(1, len(images_b64)))
        img, post = apply_image_post(raw, params, cb, start=post_start, end=post_end)
        meta = image_meta(params, img, width=gen_w, height=gen_h, post=post,
                          prompt=prompts[i] if i < len(prompts) else prompts[0],
                          seed=seeds[i] if i < len(seeds) else seeds[0],
                          negative_prompt=negatives[i] if i < len(negatives) else negatives[0],
                          aspect=params.get("aspect", ""),
                          model=variants.repo_of(remote_variant), loras=[],
                          remote=True)
        asset_ids.append(persist_image(img, job_id=job_id, generator="colab_a100", meta=meta,
                                       tag="a100", params=params))
    return {"asset_ids": asset_ids}


@router.post("/generate/image/remote")
@router.post("/generate/image/colab", deprecated=True)
async def gen_remote(payload: str = Form(...)) -> dict:
    params = _common_params(parse_payload(payload), JobKind.image_colab.value)
    fanned = await _maybe_fanout(JobKind.image_colab.value, params, _remote_image_handler)
    return fanned or await submit(JobKind.image_colab.value, params, _remote_image_handler)


# ---- Remote GPU image editing (Qwen-Image-Edit, 1-3 input images) -----------
def _remote_edit_handler(job_id: int, params: dict, cb: ProgressCb) -> dict:
    import base64

    from ..remote_gpu_client import decode_remote_image, run_remote

    paths = params.get("image_paths") or []
    if not paths:
        raise RuntimeError("Image Edit requires at least one input image.")
    images_b64 = []
    for p in paths:
        with open(p, "rb") as f:
            images_b64.append(base64.b64encode(f.read()).decode())
    seed, prompt, negative = item_seed_prompts(params, 0)
    cb(0.05, "A100 editing …")
    resp = run_remote("/edit", {
        "prompt": prompt, "negative_prompt": negative,
        "steps": int(params.get("steps", 30)), "guidance": float(params.get("guidance", 4.0)),
        "seed": seed, "images_b64": images_b64,
        "speed": bool(params.get("speed_mode", False)),
    }, progress_cb=lambda f, m, preview=None: cb(0.05 + 0.85 * f, m, preview=preview),
        job_id=job_id)
    raw = decode_remote_image(resp["image_b64"])
    gen_w, gen_h = raw.size
    img, post = apply_image_post(raw, params, cb, start=0.9, end=1.0)
    meta = image_meta(params, img, prompt=prompt, seed=seed, inputs=len(paths),
                      negative_prompt=negative,
                      width=gen_w, height=gen_h, post=post,
                      model=settings.qwen_edit_model, remote=True)
    aid = persist_image(img, job_id=job_id, generator="colab_edit", meta=meta, tag="edit",
                         params=params)
    return {"asset_ids": [aid]}


@router.post("/generate/image/edit")
async def gen_edit(payload: str = Form(...), image: UploadFile | None = None,
                   image_2: UploadFile | None = None,
                   image_3: UploadFile | None = None) -> dict:
    params = _common_params(parse_payload(payload), JobKind.image_edit.value)
    paths = [await save_upload(f) for f in (image, image_2, image_3) if f is not None]
    params["image_paths"] = [p for p in paths if p]
    if not params["image_paths"]:
        raise HTTPException(status_code=400, detail="input image required")
    return await submit(JobKind.image_edit.value, params, _remote_edit_handler)


# ---- local ControlNet (experimental; registry entry gated on ENABLE_CONTROLNET) ----
def _control_handler(job_id: int, params: dict, cb: ProgressCb) -> dict:
    from ..generators import control_pre, local_image
    from ..queue import SkipItem, load_reporter

    if not params.get("image_path"):
        raise RuntimeError("ControlNet requires an input image.")
    src = Image.open(params["image_path"]).convert("RGB")
    cb(0.02, f"preprocessing ({params.get('control_mode', 'canny')}) …")
    control = control_pre.preprocess(src, params.get("control_mode", "canny"))
    asset_ids = []
    batch = max(1, int(params.get("batch", 1)))
    w, h = int(params.get("width") or 1024), int(params.get("height") or 1024)
    for i in range(batch):
        item_start, denoise_end, item_end = image_progress_window(params, i, batch)
        cb(item_start, f"image {i + 1}/{batch}")
        seed, prompt, negative = item_seed_prompts(params, i)
        metrics: dict[str, float] = {}
        try:
            img = local_image.generate_control(
                prompt=prompt, negative_prompt=negative,
                steps=int(params.get("steps", 8)), guidance=float(params.get("guidance", 1.0)),
                seed=seed, width=w, height=h, control_image=control,
                control_weight=float(params.get("control_weight", 0.7)),
                metrics=metrics,
                report=load_reporter(job_id, "control"),
                progress_cb=lambda f, m, preview=None, _start=item_start, _end=denoise_end: cb(
                    _start + (_end - _start) * f, m, preview=preview),
            )
        except SkipItem:
            continue
        gen_w, gen_h = img.size
        img, post = apply_image_post(
            img, params, cb, start=denoise_end, end=item_end, metrics=metrics)
        meta = image_meta(params, img, prompt=prompt, seed=seed,
                          negative_prompt=negative,
                          width=gen_w, height=gen_h, post=post,
                          control_mode=params.get("control_mode"),
                          control_weight=params.get("control_weight"),
                          model=settings.local_image_model, **metrics)
        asset_ids.append(persist_image(img, job_id=job_id, generator="local_image:control",
                                        meta=meta, tag="control", params=params))
    return {"asset_ids": asset_ids}


@router.post("/generate/image/control")
async def gen_control(payload: str = Form(...), image: UploadFile | None = None) -> dict:
    _require_local_gpu()
    p = parse_payload(payload)
    params = _common_params(p, JobKind.image_local.value)  # sizing/steps like local txt2img
    params["control_mode"] = p.get("control_mode", "canny")
    params["control_weight"] = as_float(p.get("control_weight"), 0.7, 0.0, 1.0)
    params["image_path"] = require_upload(await save_upload(image))
    return await submit(JobKind.control_local.value, params, _control_handler)


# Register handlers so jobs of these kinds can be re-run by id (retry / generate-more).
register_handler(JobKind.image_local.value, _local_handler("txt2img"))
register_handler(JobKind.img2img.value, _local_handler("img2img"))
register_handler(JobKind.inpaint.value, _local_handler("inpaint"))
register_handler(JobKind.outpaint.value, _outpaint_handler)
register_handler(JobKind.image_colab.value, _remote_image_handler)
register_handler(JobKind.image_edit.value, _remote_edit_handler)
register_handler(JobKind.control_local.value, _control_handler)
