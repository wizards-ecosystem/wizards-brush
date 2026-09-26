"""Model loading and the image/video pipelines.

One heavy pipeline is resident at a time: `_get()` clears `_STATE` and reports a
"swapping" stage before loading a different model, because these checkpoints are
35-58 GB and two will not co-exist on one card.
"""
from __future__ import annotations

import base64
import gc
import io
import subprocess
import sys
import tempfile
from pathlib import Path

import torch
from fastapi import HTTPException
from PIL import Image, ImageOps

from . import config, runtime

config.HIDREAM_CODE = runtime.TOOLS / "hidream-o1"
config.HIDREAM_CODE_REV = "2c2d29ff729e48f33e41f49edfdbd81d5ac103b4"


def _prepare_hidream() -> None:
    """Fetch the official HiDream runner at a reviewed, reproducible commit.

    The model repository contains weights but not the Pixel-DiT sampling code.
    Its official source defaults to FlashAttention and documents changing that
    flag when the optional kernel is unavailable. Remote runtime images vary, so
    this copy uses the documented portable path instead of compiling a CUDA
    extension at startup.
    """
    if not config.IMAGE_MODEL_HIDREAM:
        return
    git_dir = config.HIDREAM_CODE / ".git"
    if not git_dir.is_dir():
        config.HIDREAM_CODE.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(config.HIDREAM_CODE), "init", "-q"], check=True)
        subprocess.run([
            "git", "-C", str(config.HIDREAM_CODE), "remote", "add", "origin",
            "https://github.com/HiDream-ai/HiDream-O1-Image.git",
        ], check=True)
    current = subprocess.run(
        ["git", "-C", str(config.HIDREAM_CODE), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    if current != config.HIDREAM_CODE_REV:
        subprocess.run([
            "git", "-C", str(config.HIDREAM_CODE), "fetch", "-q", "--depth", "1",
            "origin", config.HIDREAM_CODE_REV,
        ], check=True)
        subprocess.run([
            "git", "-C", str(config.HIDREAM_CODE), "checkout", "-q", "--detach", "FETCH_HEAD",
        ], check=True)
    pipeline_file = config.HIDREAM_CODE / "models" / "pipeline.py"
    source = pipeline_file.read_text()
    portable = source.replace('"use_flash_attn": True', '"use_flash_attn": False')
    if portable == source and '"use_flash_attn": False' not in source:
        raise RuntimeError("the pinned HiDream runner no longer exposes its documented attention flag")
    if portable != source:
        pipeline_file.write_text(portable)


# ══════════════════ CELL 2 — server code (paste everything below into a new cell) ══════════════════

# (resolution, orientation) -> (w, h). Shared with the video request helpers.
_RES = {
    ("720p", "landscape"): (1280, 704), ("720p", "portrait"): (704, 1280), ("720p", "square"): (704, 704),
    ("480p", "landscape"): (832, 480), ("480p", "portrait"): (480, 832), ("480p", "square"): (512, 512),
}


def res_for(resolution: str, orientation: str) -> tuple[int, int]:
    return _RES.get((resolution, orientation), (1024, 1024))


def fit_image(img: Image.Image, w: int, h: int) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("RGB")
    sw, sh = img.size
    scale = max(w / sw, h / sh)
    nw, nh = round(sw * scale), round(sh * scale)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _decode_image(b64: str) -> Image.Image:
    try:
        data = base64.b64decode(b64, validate=True)
        with Image.open(io.BytesIO(data)) as opened:
            width, height = opened.size
            if (width <= 0 or height <= 0 or width > 16_384 or height > 16_384
                    or width * height > 64_000_000):
                raise ValueError("image dimensions exceed the remote input limit")
            opened.load()
            return opened.copy()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("invalid encoded image") from exc


def free_memory() -> None:
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()


# ---- optional speedups (never fatal) ---------------------------------------
def _try_fbcache(pipe) -> None:
    if config.FBCACHE_THRESHOLD <= 0:
        return
    try:
        from diffusers.hooks import FirstBlockCacheConfig

        pipe.transformer.enable_cache(FirstBlockCacheConfig(threshold=config.FBCACHE_THRESHOLD))
        print(f"[remote-gpu] first-block cache on (threshold={config.FBCACHE_THRESHOLD})")
    except Exception as e:  # noqa: BLE001
        print(f"[remote-gpu] fbcache unavailable: {e}")


def _try_sage(pipe) -> None:
    if not config.ENABLE_SAGE_ATTENTION:
        return
    try:
        pipe.transformer.set_attention_backend("sage")
        print("[remote-gpu] SageAttention backend on")
    except Exception as e:  # noqa: BLE001
        print(f"[remote-gpu] sage unavailable: {e}")


_FLF2V_SUPPORT: dict = {}


def _supports_flf2v(model: str) -> bool:
    """Does this video model actually use a last frame?

    Signature presence is not enough, and that is exactly how this went wrong:
    WanImageToVideoPipeline.__call__ accepts `last_image` on every Wan model, so
    the old `"last_image" in sig` check passed for Wan 2.2 TI2V-5B and the frame
    was then dropped internally. Measured: the same seed with and without a last
    frame produced pixel-identical video.

    The real requirement is an `image_encoder` component — that is what turns
    both the first and last frames into conditioning embeddings. TI2V-5B has
    none (it conditions through the VAE latent, which is why plain i2v still
    works); the Wan checkpoints that genuinely do FLF2V all declare one.

    Read from model_index.json, so it is known before any weights load.
    """
    if model in _FLF2V_SUPPORT:
        return _FLF2V_SUPPORT[model]
    ok = False
    try:
        import json as _json

        from huggingface_hub import hf_hub_download

        path = hf_hub_download(
            model,
            "model_index.json",
            token=config.HF_TOKEN or None,
            cache_dir=str(runtime.HF_CACHE),
            revision=config.model_revision(model),
        )
        with open(path) as fh:
            index = _json.load(fh)
        ok = "image_encoder" in index
    except Exception as e:  # noqa: BLE001 — unknown means "do not advertise it"
        print(f"[remote-gpu] could not determine FLF2V support for {model}: {e}")
    _FLF2V_SUPPORT[model] = ok
    return ok


def _pick_lightning_weight(repo: str, kind: str) -> str | None:
    """Choose which file in a Lightning repo belongs to this pipeline.

    The lightx2v repos hold 4- and 8-step variants, bf16 and fp32 weights, and
    sometimes full fp8 checkpoints that are not LoRAs at all. Calling
    load_lora_weights() without a weight_name lets diffusers pick an arbitrary
    file, so selection has to be explicit even though generation and editing now
    use separate, model-matched repositories.

    Selection is by rule rather than a hardcoded filename so a different repo
    still works: exclude full checkpoints, match the Edit/base split against the
    pipeline, then prefer bf16 (half the download, same result here), the
    4-step variant (the backend clamps speed requests to 4 steps), and the
    highest version available. Returns None if nothing matches, in which case
    the caller lets diffusers decide and says so.
    """
    from huggingface_hub import HfApi

    want_edit = kind == "edit"
    files = [
        f for f in HfApi().list_repo_files(repo, revision=config.model_revision(repo))
        if f.endswith(".safetensors")
        and "lightning" in f.lower()          # drops the fp8 full checkpoints
        and ("edit" in f.lower()) == want_edit
    ]
    if not files:
        return None

    def rank(name: str) -> tuple:
        low = name.lower()
        version = 0.0
        for token in low.replace("-", " ").replace("/", " ").split():
            if token.startswith("v") and token[1:].replace(".", "", 1).isdigit():
                version = max(version, float(token[1:]))
        return (
            "bf16" in low,        # smaller download, same output at this precision
            "4steps" in low,      # speed requests are clamped to 4 steps
            version,
        )

    return max(files, key=rank)


def _try_lightning(pipe, repo: str, kind: str = "image") -> bool:
    """Load a Lightning distill LoRA once per pipe (disabled until a request asks)."""
    if not repo:
        return False
    try:
        weight = _pick_lightning_weight(repo, kind)
    except Exception as e:  # noqa: BLE001 — listing is a convenience, never fatal
        print(f"[remote-gpu] could not list {repo} ({e}); letting diffusers choose")
        weight = None
    try:
        if weight:
            pipe.load_lora_weights(
                repo,
                weight_name=weight,
                adapter_name="lightning",
                cache_dir=str(runtime.HF_CACHE),
                revision=config.model_revision(repo),
            )
        else:
            print(f"[remote-gpu] !! no {kind} Lightning weight matched in {repo} — "
                  "diffusers will pick one, which may be the wrong adapter")
            pipe.load_lora_weights(
                repo, adapter_name="lightning", cache_dir=str(runtime.HF_CACHE),
                revision=config.model_revision(repo),
            )
        pipe.disable_lora()
        print(f"[remote-gpu] lightning LoRA loaded for {kind}: {repo}/{weight or '<auto>'}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[remote-gpu] lightning LoRA unavailable ({repo}): {e}")
        return False


def _require_speed(kind: str) -> None:
    """Fail loudly when a speed request cannot be honoured.

    The backend clamps steps to 4 and CFG to 1.0 BEFORE sending a speed request,
    because those settings only make sense with a Lightning distill LoRA active.
    If the LoRA never loaded, silently proceeding runs a non-distilled model for
    4 steps and returns noise with no error anywhere. Refusing is the only
    honest outcome — the backend gates the control on /health features, so this
    should only fire when the two have genuinely drifted apart."""
    if not _PIPES.get(f"{kind}_speed"):
        raise HTTPException(
            status_code=409,
            detail=(f"speed mode requested but no Lightning LoRA is loaded for the "
                    f"{kind} pipeline. Set {'VIDEO' if kind == 'video' else 'IMAGE'}"
                    f"_LIGHTNING_LORA and restart the Remote GPU worker, or turn speed mode off."),
        )


def _set_speed(pipe, on: bool) -> None:
    """Toggle the lightning adapter per request (the pipe is shared)."""
    try:
        if on:
            pipe.set_adapters(["lightning"])
            pipe.enable_lora()
        else:
            pipe.disable_lora()
    except Exception:  # noqa: BLE001 — no adapter loaded
        pass


# ---- lazy pipeline registry (one heavy pipe resident at a time) -----------
_PIPES: dict = {"image": None, "edit": None, "edit_inpaint": None, "video_mode": None,
                "video": None, "video_engine": None, "image_speed": False,
                "edit_speed": False, "video_speed": False}


def _evict_all() -> None:
    _PIPES.update(image=None, image_variant=None, edit=None, edit_inpaint=None,
                  video=None, video_mode=None, video_engine=None)
    free_memory()


def _tune_scheduler(pipe):
    """Give an SDXL checkpoint the sampler it was actually tuned for.

    SDXL repos ship EulerDiscreteScheduler in scheduler_config.json, but the
    community checkpoints people run here are tuned and previewed on DPM++ 2M SDE
    with Karras sigmas — community SDXL checkpoints are routinely published with
    exactly that, at 30 steps and CFG 2.5-4.5. Loading with the shipped default
    is not a crash, it is just
    quietly worse: softer skin, muddier detail, and nothing anywhere saying the
    sampler is the reason.

    Only SDXL. Qwen-Image, Flux and Wan ship flow-matching schedulers that are
    part of how the model was trained, and swapping those breaks them.
    """
    if "StableDiffusionXL" not in type(pipe).__name__:
        return
    try:
        from diffusers import DPMSolverMultistepScheduler

        pipe.scheduler = DPMSolverMultistepScheduler.from_config(
            pipe.scheduler.config,
            algorithm_type="sde-dpmsolver++",
            use_karras_sigmas=True,
        )
        print("[remote-gpu] scheduler -> DPM++ 2M SDE Karras (SDXL checkpoint)")
    except Exception as e:  # noqa: BLE001 — the shipped scheduler still generates
        print(f"[remote-gpu] keeping {type(pipe.scheduler).__name__} ({e})")


IMAGE_MODELS = {"quality": config.IMAGE_MODEL, "hidream": config.IMAGE_MODEL_HIDREAM,
                "alt": config.IMAGE_MODEL_ALT}


class _HiDreamResult:
    def __init__(self, image):
        self.images = [image]


class _HiDreamPipe:
    """Small adapter from the official Pixel-DiT runner to our image endpoint."""

    def __init__(self, model, processor, generate_image):
        self.model = model
        self.processor = processor
        self.generate_image = generate_image

    def disable_lora(self) -> None:
        return None

    def __call__(
        self, prompt: str, num_inference_steps: int = 50, width: int = 1024,
        height: int = 1024, guidance_scale: float = 5.0, generator=None,
        callback_on_step_end=None, **_kwargs,
    ):
        seed = int(generator.initial_seed()) if generator is not None else 0

        def progress(step: int, _total: int, _preview) -> None:
            if callback_on_step_end is not None:
                callback_on_step_end(self, step, None, {})

        image = self.generate_image(
            model=self.model,
            processor=self.processor,
            prompt=prompt,
            ref_image_paths=[],
            height=height,
            width=width,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            shift=3.0,
            timesteps_list=None,
            scheduler_name="default",
            seed=seed,
            callback=progress,
        )
        return _HiDreamResult(image)


def _load_hidream(model_id: str):
    """Load HiDream's official custom model and sampler on the remote GPU."""
    # Fetch the pinned runner here rather than at startup: it is a git clone of
    # a third-party repository, and a worker whose HiDream slot is empty (or
    # which never serves that variant) has no business doing it at all.
    _prepare_hidream()
    version = tuple(int(part) for part in torch.__version__.split("+", 1)[0].split(".")[:2])
    if version < (2, 10):
        raise RuntimeError(
            f"HiDream-O1 requires torch >=2.10; this runtime has {torch.__version__}")
    if str(config.HIDREAM_CODE) not in sys.path:
        sys.path.insert(0, str(config.HIDREAM_CODE))
    from models.pipeline import generate_image  # type: ignore[import-not-found]
    from models.qwen3_vl_transformers import (  # type: ignore[import-not-found]
        Qwen3VLForConditionalGeneration,
    )
    from transformers import AutoProcessor

    processor = AutoProcessor.from_pretrained(
        model_id, token=config.HF_TOKEN or None, cache_dir=str(runtime.HF_CACHE),
        revision=config.model_revision(model_id))
    custom_model = Qwen3VLForConditionalGeneration.from_pretrained(
        model_id,
        torch_dtype=torch.bfloat16,
        device_map="cuda",
        token=config.HF_TOKEN or None,
        cache_dir=str(runtime.HF_CACHE),
        revision=config.model_revision(model_id),
    ).eval()
    tokenizer = processor.tokenizer if hasattr(processor, "tokenizer") else processor
    tokenizer.boi_token = "<|boi_token|>"
    tokenizer.bor_token = "<|bor_token|>"
    tokenizer.eor_token = "<|eor_token|>"
    tokenizer.bot_token = "<|bot_token|>"
    tokenizer.tms_token = "<|tms_token|>"
    return _HiDreamPipe(custom_model, processor, generate_image)


def image_model_for(variant: str) -> str:
    """Repo id for a requested image variant, falling back to the quality slot.

    Total by construction: an unknown or unconfigured variant runs the default
    model rather than failing the job. The app only offers what /health lists, so
    reaching the fallback means the two sides disagree — which is survivable.
    """
    return IMAGE_MODELS.get(variant) or config.IMAGE_MODEL


def get_image_pipe(variant: str = "quality"):
    """The image pipeline for `variant`, loading (and evicting) as needed.

    Only ONE image model is resident: an 80 GB GPU holds these 35-58 GB models,
    so two would not co-exist and the second load would OOM mid-job. Switching
    therefore costs a full reload — and, on the first use of each model in a
    session, a download. `_PIPES["image_variant"]` records which one is up so a
    run of jobs on the same model pays nothing.
    """
    model = image_model_for(variant)
    if _PIPES["image"] is None or _PIPES.get("image_variant") != variant:
        _evict_all()
        print(f"[remote-gpu] loading image model {model} ({variant}) …")
        if variant == "hidream":
            pipe = _load_hidream(model)
        else:
            from diffusers import DiffusionPipeline

            # DiffusionPipeline picks the right class from model_index.json
            # (QwenImagePipeline, Flux2KleinPipeline, …) — model-agnostic.
            pipe = DiffusionPipeline.from_pretrained(
                model,
                torch_dtype=torch.bfloat16,
                cache_dir=str(runtime.HF_CACHE),
                add_watermarker=False,
                revision=config.model_revision(model),
            )
            _tune_scheduler(pipe)
            pipe.to("cuda") if not config.OFFLOAD else pipe.enable_model_cpu_offload()
        # This is specifically the Qwen-Image-2512 adapter. Never attempt to
        # merge it into an alternate family or a custom pipe.
        _PIPES["image_speed"] = (
            _try_lightning(pipe, config.IMAGE_LIGHTNING_LORA, "image")
            if variant == "quality" else False
        )
        if variant != "hidream":
            _try_fbcache(pipe)
            _try_sage(pipe)
        _PIPES["image"] = pipe
        _PIPES["image_variant"] = variant
        print(f"[remote-gpu] image pipeline ready ({variant}).")
    return _PIPES["image"]


def get_edit_pipe():
    if _PIPES["edit"] is None:
        import diffusers

        _evict_all()
        print(f"[remote-gpu] loading edit model {config.EDIT_MODEL} …")
        cls = (getattr(diffusers, "QwenImageEditPlusPipeline", None)
               or getattr(diffusers, "QwenImageEditPipeline", None)
               or diffusers.DiffusionPipeline)
        pipe = cls.from_pretrained(
            config.EDIT_MODEL,
            torch_dtype=torch.bfloat16,
            cache_dir=str(runtime.HF_CACHE),
            add_watermarker=False,
            revision=config.model_revision(config.EDIT_MODEL),
        )
        pipe.to("cuda") if not config.OFFLOAD else pipe.enable_model_cpu_offload()
        _PIPES["edit_speed"] = _try_lightning(pipe, config.EDIT_LIGHTNING_LORA, "edit")
        _try_fbcache(pipe)
        _try_sage(pipe)
        _PIPES["edit"] = pipe
        print("[remote-gpu] edit pipeline ready.")
    if _PIPES.get("edit_inpaint") is None:
        _ensure_edit_inpaint(_PIPES["edit"])
    return _PIPES["edit"]


def _edit_inpaint_cls():
    import diffusers

    return getattr(diffusers, "QwenImageEditInpaintPipeline", None)


def _ensure_edit_inpaint(plus_pipe) -> None:
    """Share the loaded 2511 modules with the masked inpaint pipeline.

    Plus restyle and masked inpaint are the same checkpoint. Building a second
    resident copy would evict the first. `from_pipe` (or a components copy) keeps
    one transformer. False means this Diffusers build cannot inpaint; /inpaint
    then 409s rather than silently calling Plus.
    """
    if _PIPES.get("edit_inpaint") is not None:
        return
    cls = _edit_inpaint_cls()
    if cls is None:
        _PIPES["edit_inpaint"] = False
        print("[remote-gpu] QwenImageEditInpaintPipeline is not in this Diffusers build.")
        return
    try:
        if hasattr(cls, "from_pipe"):
            inpaint = cls.from_pipe(plus_pipe)
        else:
            inpaint = cls(**plus_pipe.components)
        _PIPES["edit_inpaint"] = inpaint
        print("[remote-gpu] edit inpaint pipeline ready (shared 2511 weights).")
    except Exception as exc:  # noqa: BLE001 — feature flag must not kill /edit
        _PIPES["edit_inpaint"] = False
        print(f"[remote-gpu] edit inpaint unavailable: {exc}")


def get_edit_inpaint_pipe():
    get_edit_pipe()
    pipe = _PIPES.get("edit_inpaint")
    if pipe in (None, False):
        raise HTTPException(
            status_code=409,
            detail=("this worker cannot inpaint: QwenImageEditInpaintPipeline is "
                    "not available in this Diffusers build. Restart with a current "
                    "the worker, or use /edit for a full-frame restyle."),
        )
    return pipe


def _load_wan(mode: str):
    from diffusers import AutoencoderKLWan, WanImageToVideoPipeline, WanPipeline

    print(f"[remote-gpu] loading Wan {mode.upper()} pipeline {config.VIDEO_MODEL} "
          f"(first load downloads ~20-30GB) …")
    vae = AutoencoderKLWan.from_pretrained(
        config.VIDEO_MODEL,
        subfolder="vae",
        torch_dtype=torch.float32,
        cache_dir=str(runtime.HF_CACHE),
        revision=config.model_revision(config.VIDEO_MODEL),
    )
    cls = WanPipeline if mode == "t2v" else WanImageToVideoPipeline
    return cls.from_pretrained(
        config.VIDEO_MODEL,
        vae=vae,
        torch_dtype=torch.bfloat16,
        cache_dir=str(runtime.HF_CACHE),
        add_watermarker=False,
        revision=config.model_revision(config.VIDEO_MODEL),
    )


def _load_hunyuan(mode: str):
    import diffusers

    if not config.HUNYUAN_VIDEO_MODEL:
        raise RuntimeError("config.HUNYUAN_VIDEO_MODEL is not configured")
    print(f"[remote-gpu] loading Hunyuan {mode.upper()} pipeline {config.HUNYUAN_VIDEO_MODEL} …")
    names = (["HunyuanVideo15ImageToVideoPipeline", "HunyuanVideoImageToVideoPipeline"]
             if mode == "i2v" else ["HunyuanVideo15Pipeline", "HunyuanVideoPipeline"])
    for name in names:
        cls = getattr(diffusers, name, None)
        if cls is not None:
            return cls.from_pretrained(
                config.HUNYUAN_VIDEO_MODEL,
                torch_dtype=torch.bfloat16,
                cache_dir=str(runtime.HF_CACHE),
                add_watermarker=False,
                revision=config.model_revision(config.HUNYUAN_VIDEO_MODEL),
            )
    from diffusers import DiffusionPipeline

    return DiffusionPipeline.from_pretrained(
        config.HUNYUAN_VIDEO_MODEL,
        torch_dtype=torch.bfloat16,
        cache_dir=str(runtime.HF_CACHE),
        add_watermarker=False,
        revision=config.model_revision(config.HUNYUAN_VIDEO_MODEL),
    )


def _load_ltx(mode: str):
    """LTX-2: the only open-weights family that generates synced audio.

    Checks free disk first. The 2.x checkpoints are 150-200 GB; a standard 80 GB
    GPU runtime has well under that once Qwen and Wan are cached, and the failure
    without this check is a download that runs for many minutes and then dies
    with no space left — after evicting whatever pipeline was resident.
    """
    import diffusers

    if not config.LTX_VIDEO_MODEL:
        raise RuntimeError("config.LTX_VIDEO_MODEL is not configured")

    free = runtime.free_disk_gb()
    if free < 60:
        raise HTTPException(
            status_code=507,
            detail=(f"only {free:.0f} GB free — LTX-2 checkpoints are 150-200 GB. "
                    "Free space or use a runtime with a larger disk; the download "
                    "would fail partway and take the resident pipeline with it."),
        )

    print(f"[remote-gpu] loading LTX {mode.upper()} pipeline {config.LTX_VIDEO_MODEL} "
          f"({free:.0f} GB free) …")
    names = (["LTX2ImageToVideoPipeline", "LTXImageToVideoPipeline"]
             if mode == "i2v" else ["LTX2Pipeline", "LTXPipeline"])
    for name in names:
        cls = getattr(diffusers, name, None)
        if cls is not None:
            return cls.from_pretrained(
                config.LTX_VIDEO_MODEL,
                torch_dtype=torch.bfloat16,
                cache_dir=str(runtime.HF_CACHE),
                add_watermarker=False,
                revision=config.model_revision(config.LTX_VIDEO_MODEL),
            )
    from diffusers import DiffusionPipeline

    return DiffusionPipeline.from_pretrained(
        config.LTX_VIDEO_MODEL,
        torch_dtype=torch.bfloat16,
        cache_dir=str(runtime.HF_CACHE),
        add_watermarker=False,
        revision=config.model_revision(config.LTX_VIDEO_MODEL),
    )


_VIDEO_LOADERS = {"wan": _load_wan, "hunyuan": _load_hunyuan, "ltx": _load_ltx}


def get_video_pipe(mode: str, engine: str = "wan"):
    """mode: 't2v' or 'i2v'; engine: 'wan' | 'hunyuan' | 'ltx'. One resident pipe."""
    if (_PIPES["video_mode"] == mode and _PIPES["video_engine"] == engine
            and _PIPES["video"] is not None):
        return _PIPES["video"]
    _evict_all()
    loader = _VIDEO_LOADERS.get(engine, _load_wan)
    pipe = loader(mode)
    if config.OFFLOAD:
        pipe.enable_model_cpu_offload()
    else:
        pipe.to("cuda")
    if hasattr(pipe, "vae") and hasattr(pipe.vae, "enable_tiling"):
        pipe.vae.enable_tiling()
    # The Lightning LoRA is a Wan adapter; applying it to another engine would
    # either fail or silently distort.
    if engine == "wan":
        _PIPES["video_speed"] = _try_lightning(pipe, config.VIDEO_LIGHTNING_LORA, "video")
    else:
        _PIPES["video_speed"] = False
    _try_fbcache(pipe)
    _try_sage(pipe)
    _PIPES.update(video=pipe, video_mode=mode, video_engine=engine)
    print("[remote-gpu] video pipeline ready.")
    return pipe


def _wan_negative() -> str:
    return ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，"
            "最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，"
            "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，"
            "杂乱的背景，三条腿，背景人很多，倒着走")


def _encode_video(frames, fps: int) -> str:
    from diffusers.utils import export_to_video

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False, dir=runtime.TMP) as f:
        path = f.name
    try:
        export_to_video(frames, path, fps=fps)
        with open(path, "rb") as fh:
            data = fh.read()
    finally:
        Path(path).unlink(missing_ok=True)
    return base64.b64encode(data).decode()
