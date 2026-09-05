#!/usr/bin/env python3
"""The Wizard's Brush — optional Remote GPU server, in one file.

Run this only on GPU hardware you operate or are explicitly authorized to expose
as an authenticated service. The worker has no provider SDK dependency and can
run from a shell or an interactive notebook on a compatible Linux GPU runtime.
Everything else lives on your local machine.

How to run:
  • Locally run `make remote-gpu` — it writes remote_gpu_filled.py with your
    .env keys, model revisions, and hashed dependency lock injected.
  • Copy remote_gpu_filled.py to the remote GPU and run it with Python.
  • …or run it in another environment you control; the CELL markers make it
    convenient to split for an interactive Python session.

When it prints a URL, paste it into Settings → Remote GPU.
"""
from __future__ import annotations

# ══════════════════ CELL 1 — config + install (run first) ══════════════════
# ==== keys — injected from your local .env by `make remote-gpu`.
# This source file is a template, not a runnable secret/config bundle. Never
# commit real values here; use `make remote-gpu` so immutable inputs are included.
HF_TOKEN = ""
REMOTE_GPU_SHARED_SECRET = ""
# Model/feature config injected from .env by `make remote-gpu` (may stay empty).
REMOTE_GPU_CONFIG: dict = {}
# Fingerprint of remote_gpu.py, injected by `make remote-gpu`. Reported by
# /health so the backend can tell when the remote worker is running an older
# build than the repo — otherwise a fix shipped locally looks live while the GPU
# is still executing last week's code, and the only symptom
# is behaviour that contradicts the source.
REMOTE_GPU_BUILD = ""
# Complete hashed dependency lock injected by `make remote-gpu`. Keeping this
# placeholder out of the tracked template avoids duplicating an 80 KB lock while
# preserving the generated worker's single-file deployment contract.
REMOTE_GPU_REQUIREMENTS_LOCK = ""
# =============================================================================

import os
import subprocess
import sys
from pathlib import Path

# Keep cache, downloaded packages, models, tools, and temporary files beside the
# uploaded script. A remote runtime can be ephemeral, but it follows the same
# no-home-directory-spill contract as the local app.
REMOTE_GPU_ROOT = (
    Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
) / ".wizards-brush-remote-gpu"
REMOTE_GPU_CACHE = REMOTE_GPU_ROOT / "cache"
REMOTE_GPU_CONFIG_DIR = REMOTE_GPU_ROOT / "config"
REMOTE_GPU_DATA = REMOTE_GPU_ROOT / "data"
REMOTE_GPU_STATE = REMOTE_GPU_ROOT / "state"
REMOTE_GPU_MODELS = REMOTE_GPU_ROOT / "models"
REMOTE_GPU_TMP = REMOTE_GPU_ROOT / "tmp"
REMOTE_GPU_PYTHON = REMOTE_GPU_ROOT / "python"
REMOTE_GPU_TOOLS = REMOTE_GPU_ROOT / "tools"
REMOTE_GPU_HF_CACHE = REMOTE_GPU_MODELS / "huggingface" / "hub"
REMOTE_GPU_HOME = REMOTE_GPU_ROOT / "home"
for directory in (
    REMOTE_GPU_CACHE,
    REMOTE_GPU_CONFIG_DIR,
    REMOTE_GPU_DATA,
    REMOTE_GPU_STATE,
    REMOTE_GPU_MODELS,
    REMOTE_GPU_TMP,
    REMOTE_GPU_PYTHON,
    REMOTE_GPU_TOOLS,
    REMOTE_GPU_HOME,
):
    directory.mkdir(parents=True, exist_ok=True)
os.environ.update({
    "HOME": str(REMOTE_GPU_HOME),
    "XDG_CACHE_HOME": str(REMOTE_GPU_CACHE / "xdg"),
    "XDG_CONFIG_HOME": str(REMOTE_GPU_CONFIG_DIR / "xdg"),
    "XDG_DATA_HOME": str(REMOTE_GPU_DATA / "xdg"),
    "XDG_STATE_HOME": str(REMOTE_GPU_STATE / "xdg"),
    "TMPDIR": str(REMOTE_GPU_TMP),
    "TMP": str(REMOTE_GPU_TMP),
    "TEMP": str(REMOTE_GPU_TMP),
    "PIP_CACHE_DIR": str(REMOTE_GPU_CACHE / "pip"),
    "PIP_CONFIG_FILE": str(REMOTE_GPU_CONFIG_DIR / "pip.conf"),
    "PYTHONUSERBASE": str(REMOTE_GPU_ROOT / "python-user"),
    "PYTHONPYCACHEPREFIX": str(REMOTE_GPU_CACHE / "python-bytecode"),
    "HF_HOME": str(REMOTE_GPU_MODELS / "huggingface"),
    "HF_HUB_CACHE": str(REMOTE_GPU_HF_CACHE),
    "HF_XET_CACHE": str(REMOTE_GPU_MODELS / "huggingface" / "xet"),
    "HF_ASSETS_CACHE": str(REMOTE_GPU_MODELS / "huggingface" / "assets"),
    "HUGGINGFACE_HUB_CACHE": str(REMOTE_GPU_HF_CACHE),
    "SENTENCE_TRANSFORMERS_HOME": str(REMOTE_GPU_MODELS / "sentence-transformers"),
    "TORCH_HOME": str(REMOTE_GPU_MODELS / "torch"),
    "TORCH_EXTENSIONS_DIR": str(REMOTE_GPU_CACHE / "torch-extensions"),
    "TORCHINDUCTOR_CACHE_DIR": str(REMOTE_GPU_CACHE / "torch-inductor"),
    "TRITON_CACHE_DIR": str(REMOTE_GPU_CACHE / "triton"),
    "CUDA_CACHE_PATH": str(REMOTE_GPU_CACHE / "cuda"),
    "NUMBA_CACHE_DIR": str(REMOTE_GPU_CACHE / "numba"),
    "MPLCONFIGDIR": str(REMOTE_GPU_CONFIG_DIR / "matplotlib"),
    "IMAGEIO_USERDIR": str(REMOTE_GPU_DATA / "imageio"),
    "KERAS_HOME": str(REMOTE_GPU_MODELS / "keras"),
    "ONNX_HOME": str(REMOTE_GPU_MODELS / "onnx"),
})
Path(os.environ["PIP_CONFIG_FILE"]).touch()
sys.path.insert(0, str(REMOTE_GPU_PYTHON))

HF_TOKEN = HF_TOKEN or os.environ.get("HF_TOKEN", "")
REMOTE_GPU_SHARED_SECRET = REMOTE_GPU_SHARED_SECRET or os.environ.get("REMOTE_GPU_SHARED_SECRET", "")
# The tunnel URL is public and unauthenticated at the network layer, so the
# shared secret is the ONLY thing standing between this GPU and the internet.
# An empty secret would make `X-Gen-Secret: ""` authenticate, so refuse to boot.
_WEAK_SECRETS = {"", "change-me-to-anything", "changeme", "secret", "test"}
if REMOTE_GPU_SHARED_SECRET.strip().lower() in _WEAK_SECRETS:
    raise SystemExit(
        "!! REMOTE_GPU_SHARED_SECRET is empty or a default placeholder.\n"
        "   This server would accept requests from anyone who finds the tunnel URL,\n"
        "   and it downloads arbitrary models and burns GPU time.\n"
        "   Run `make remote-gpu` locally (after setting REMOTE_GPU_SHARED_SECRET in .env,\n"
        "   e.g. `openssl rand -hex 16`) and copy the generated remote_gpu_filled.py."
    )
if not REMOTE_GPU_REQUIREMENTS_LOCK.strip():
    raise SystemExit(
        "!! This is the unconfigured remote_gpu.py template.\n"
        "   Run `make remote-gpu` in the project checkout and copy the generated\n"
        "   remote_gpu_filled.py so dependency hashes and model revisions are present."
    )


def _cfg(key: str, default: str = "") -> str:
    """Config precedence: injected REMOTE_GPU_CONFIG > env > default.

    Membership tests, not truthiness: `or` would make a legitimate 0 or "" fall
    through to the default, so PREVIEW_EVERY=0 could not turn previews off and
    FBCACHE_THRESHOLD=0 could not disable the cache."""
    if key in REMOTE_GPU_CONFIG and REMOTE_GPU_CONFIG[key] is not None:
        return str(REMOTE_GPU_CONFIG[key])
    if key.upper() in os.environ:
        return str(os.environ[key.upper()])
    return default


A100_IMAGE_MODEL = _cfg("a100_image_model", "Qwen/Qwen-Image-2512")
# Dedicated custom-pipeline comparison. HiDream is not a Diffusers repository,
# so keeping it out of the generic ALT slot lets the loader make that fact
# explicit rather than failing after a 35 GB download.
A100_IMAGE_MODEL_HIDREAM = _cfg("a100_image_model_hidream", "HiDream-ai/HiDream-O1-Image")
# Second image slot. Empty = the session offers only the primary model, and
# /health says so, so the app never shows a picker this worker cannot serve.
A100_IMAGE_MODEL_ALT = _cfg("a100_image_model_alt", "")
QWEN_EDIT_MODEL = _cfg("qwen_edit_model", "Qwen/Qwen-Image-Edit-2511")
VIDEO_MODEL = _cfg("video_model", "Wan-AI/Wan2.2-TI2V-5B-Diffusers")
HUNYUAN_VIDEO_MODEL = _cfg("hunyuan_video_model", "")
LTX_VIDEO_MODEL = _cfg("ltx_video_model", "")
IMAGE_LIGHTNING_LORA = _cfg("image_lightning_lora", "")
EDIT_LIGHTNING_LORA = _cfg("edit_lightning_lora", "")
VIDEO_LIGHTNING_LORA = _cfg("video_lightning_lora", "")
FBCACHE_THRESHOLD = float(_cfg("fbcache_threshold", "0.05").strip() or 0)
ENABLE_SAGE_ATTENTION = _cfg("enable_sage_attention", "false").lower() == "true"
PREVIEW_EVERY = int(_cfg("preview_every", "2").strip() or 0)
OFFLOAD = os.environ.get("A100_OFFLOAD", "false").lower() == "true"  # 80GB A100 -> keep on GPU
MODEL_REVISIONS = REMOTE_GPU_CONFIG.get("model_revisions", {})
if not isinstance(MODEL_REVISIONS, dict) or not MODEL_REVISIONS:
    raise SystemExit(
        "!! The generated worker has no reviewed model revisions.\n"
        "   Re-run `make remote-gpu` from the current project checkout."
    )


def _model_revision(repo_id: str) -> str | None:
    if not isinstance(MODEL_REVISIONS, dict):
        return None
    revision = MODEL_REVISIONS.get(repo_id)
    return str(revision) if revision else None

HIDREAM_CODE = REMOTE_GPU_TOOLS / "hidream-o1"
HIDREAM_CODE_REV = "2c2d29ff729e48f33e41f49edfdbd81d5ac103b4"


def _prepare_hidream() -> None:
    """Fetch the official HiDream runner at a reviewed, reproducible commit.

    The model repository contains weights but not the Pixel-DiT sampling code.
    Its official source defaults to FlashAttention and documents changing that
    flag when the optional kernel is unavailable. Remote runtime images vary, so
    this copy uses the documented portable path instead of compiling a CUDA
    extension at startup.
    """
    if not A100_IMAGE_MODEL_HIDREAM:
        return
    git_dir = HIDREAM_CODE / ".git"
    if not git_dir.is_dir():
        HIDREAM_CODE.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(HIDREAM_CODE), "init", "-q"], check=True)
        subprocess.run([
            "git", "-C", str(HIDREAM_CODE), "remote", "add", "origin",
            "https://github.com/HiDream-ai/HiDream-O1-Image.git",
        ], check=True)
    current = subprocess.run(
        ["git", "-C", str(HIDREAM_CODE), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    if current != HIDREAM_CODE_REV:
        subprocess.run([
            "git", "-C", str(HIDREAM_CODE), "fetch", "-q", "--depth", "1",
            "origin", HIDREAM_CODE_REV,
        ], check=True)
        subprocess.run([
            "git", "-C", str(HIDREAM_CODE), "checkout", "-q", "--detach", "FETCH_HEAD",
        ], check=True)
    pipeline_file = HIDREAM_CODE / "models" / "pipeline.py"
    source = pipeline_file.read_text()
    portable = source.replace('"use_flash_attn": True', '"use_flash_attn": False')
    if portable == source and '"use_flash_attn": False' not in source:
        raise RuntimeError("the pinned HiDream runner no longer exposes its documented attention flag")
    if portable != source:
        pipeline_file.write_text(portable)


def _drop_shadowing_torch() -> None:
    """Delete any torch/numpy that pip dragged into REMOTE_GPU_PYTHON.

    `pip install --target` resolves dependencies as well, so installing diffusers,
    accelerate or peft pulls a second torch into REMOTE_GPU_PYTHON. REMOTE_GPU_PYTHON is first
    on sys.path, so that copy shadows the runtime-provided copy while its
    torchvision, built against the original, is still what gets imported. The two
    disagree about registered ops and torchvision dies with

        RuntimeError: operator torchvision::nms does not exist

    which surfaces far from here: transformers imports torchvision.io, so the whole
    Qwen2.5-VL text encoder fails to load and every image-edit job returns
    "Could not import module 'Qwen2_5_VLForConditionalGeneration'".

    Removing the shadowing copies is what makes the docstring above true.
    """
    import itertools
    import shutil

    names = ("torch", "torchvision", "torchaudio", "torchgen", "functorch", "numpy")
    patterns = itertools.chain.from_iterable(
        (n, f"{n}.libs", f"{n}-*.dist-info", f"{n}-*.egg-info") for n in names
    )
    for hit in itertools.chain.from_iterable(REMOTE_GPU_PYTHON.glob(p) for p in patterns):
        print(f"[remote-gpu] dropping shadowing {hit.name} from the overlay")
        if hit.is_dir():
            shutil.rmtree(hit, ignore_errors=True)
        else:
            hit.unlink(missing_ok=True)


def _bootstrap() -> None:
    """Install service deps idempotently; torch/numpy stay runtime-provided.

    Provision a CUDA-compatible torch build on a VM before running this file.
    Some managed GPU environments provide one already.
    """
    import hashlib
    import platform

    if (sys.version_info[:2] != (3, 12) or sys.platform != "linux"
            or platform.machine().lower() not in {"x86_64", "amd64"}):
        raise RuntimeError(
            "the hashed Remote GPU dependency lock supports Linux x86-64 with Python 3.12"
        )

    requirement_id = hashlib.sha256(REMOTE_GPU_REQUIREMENTS_LOCK.encode()).hexdigest()
    marker = REMOTE_GPU_STATE / f"dependencies-{requirement_id}"
    if not marker.exists():
        lock_path = REMOTE_GPU_CONFIG_DIR / f"requirements-{requirement_id}.txt"
        lock_path.write_text(REMOTE_GPU_REQUIREMENTS_LOCK, encoding="utf-8")
        lock_path.chmod(0o600)
        subprocess.run([
            sys.executable, "-m", "pip", "install", "-q", "--upgrade",
            "--target", str(REMOTE_GPU_PYTHON), "--require-hashes",
            "--only-binary=:all:", "--no-deps", "-r", str(lock_path),
        ], check=True)
        marker.touch()
    _drop_shadowing_torch()
    _prepare_hidream()
    # Some runtime images ship torchao 0.10, which peft's LoRA loader rejects.
    # We do not use it (full bf16), so remove it to avoid that version conflict.
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "torchao"], check=False)
    cloudflared = REMOTE_GPU_TOOLS / "cloudflared-2026.8.3"
    cloudflared_sha256 = "f29324fe934d1e100617484c78deef803c4dc2cd351d645bbde42e96b4fccc5e"

    def file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    if cloudflared.exists() and file_sha256(cloudflared) != cloudflared_sha256:
        raise RuntimeError("cached cloudflared checksum mismatch; remove it and run again")
    if not cloudflared.exists():
        import urllib.request

        partial = REMOTE_GPU_TMP / "cloudflared.part"
        try:
            urllib.request.urlretrieve(
                "https://github.com/cloudflare/cloudflared/releases/download/2026.8.3/"
                "cloudflared-linux-amd64",
                partial,
            )
            if file_sha256(partial) != cloudflared_sha256:
                raise RuntimeError("downloaded cloudflared checksum mismatch")
            partial.chmod(0o755)
            os.replace(partial, cloudflared)
        finally:
            partial.unlink(missing_ok=True)


_bootstrap()  # run before importing the heavy libs below

import base64
import gc
import io
import tempfile
from typing import Annotated

import torch
from fastapi import FastAPI, Header, HTTPException
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

os.environ["HF_TOKEN"] = HF_TOKEN
os.environ["HUGGING_FACE_HUB_TOKEN"] = HF_TOKEN
os.environ["REMOTE_GPU_SHARED_SECRET"] = REMOTE_GPU_SHARED_SECRET

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
    if FBCACHE_THRESHOLD <= 0:
        return
    try:
        from diffusers.hooks import FirstBlockCacheConfig

        pipe.transformer.enable_cache(FirstBlockCacheConfig(threshold=FBCACHE_THRESHOLD))
        print(f"[remote-gpu] first-block cache on (threshold={FBCACHE_THRESHOLD})")
    except Exception as e:  # noqa: BLE001
        print(f"[remote-gpu] fbcache unavailable: {e}")


def _try_sage(pipe) -> None:
    if not ENABLE_SAGE_ATTENTION:
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
            token=HF_TOKEN or None,
            cache_dir=str(REMOTE_GPU_HF_CACHE),
            revision=_model_revision(model),
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
        f for f in HfApi().list_repo_files(repo, revision=_model_revision(repo))
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
                cache_dir=str(REMOTE_GPU_HF_CACHE),
                revision=_model_revision(repo),
            )
        else:
            print(f"[remote-gpu] !! no {kind} Lightning weight matched in {repo} — "
                  "diffusers will pick one, which may be the wrong adapter")
            pipe.load_lora_weights(
                repo, adapter_name="lightning", cache_dir=str(REMOTE_GPU_HF_CACHE),
                revision=_model_revision(repo),
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
_PIPES: dict = {"image": None, "edit": None, "video_mode": None, "video": None,
                "video_engine": None, "image_speed": False,
                "edit_speed": False, "video_speed": False}


def _evict_all() -> None:
    _PIPES.update(image=None, image_variant=None, edit=None, video=None,
                  video_mode=None, video_engine=None)
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


IMAGE_MODELS = {"quality": A100_IMAGE_MODEL, "hidream": A100_IMAGE_MODEL_HIDREAM,
                "alt": A100_IMAGE_MODEL_ALT}


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
    version = tuple(int(part) for part in torch.__version__.split("+", 1)[0].split(".")[:2])
    if version < (2, 10):
        raise RuntimeError(
            f"HiDream-O1 requires torch >=2.10; this runtime has {torch.__version__}")
    if str(HIDREAM_CODE) not in sys.path:
        sys.path.insert(0, str(HIDREAM_CODE))
    from models.pipeline import generate_image  # type: ignore[import-not-found]
    from models.qwen3_vl_transformers import (  # type: ignore[import-not-found]
        Qwen3VLForConditionalGeneration,
    )
    from transformers import AutoProcessor

    processor = AutoProcessor.from_pretrained(
        model_id, token=HF_TOKEN or None, cache_dir=str(REMOTE_GPU_HF_CACHE),
        revision=_model_revision(model_id))
    custom_model = Qwen3VLForConditionalGeneration.from_pretrained(
        model_id,
        torch_dtype=torch.bfloat16,
        device_map="cuda",
        token=HF_TOKEN or None,
        cache_dir=str(REMOTE_GPU_HF_CACHE),
        revision=_model_revision(model_id),
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
    return IMAGE_MODELS.get(variant) or A100_IMAGE_MODEL


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
                cache_dir=str(REMOTE_GPU_HF_CACHE),
                add_watermarker=False,
                revision=_model_revision(model),
            )
            _tune_scheduler(pipe)
            pipe.to("cuda") if not OFFLOAD else pipe.enable_model_cpu_offload()
        # This is specifically the Qwen-Image-2512 adapter. Never attempt to
        # merge it into an alternate family or a custom pipe.
        _PIPES["image_speed"] = (
            _try_lightning(pipe, IMAGE_LIGHTNING_LORA, "image")
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
        print(f"[remote-gpu] loading edit model {QWEN_EDIT_MODEL} …")
        cls = (getattr(diffusers, "QwenImageEditPlusPipeline", None)
               or getattr(diffusers, "QwenImageEditPipeline", None)
               or diffusers.DiffusionPipeline)
        pipe = cls.from_pretrained(
            QWEN_EDIT_MODEL,
            torch_dtype=torch.bfloat16,
            cache_dir=str(REMOTE_GPU_HF_CACHE),
            add_watermarker=False,
            revision=_model_revision(QWEN_EDIT_MODEL),
        )
        pipe.to("cuda") if not OFFLOAD else pipe.enable_model_cpu_offload()
        _PIPES["edit_speed"] = _try_lightning(pipe, EDIT_LIGHTNING_LORA, "edit")
        _try_fbcache(pipe)
        _try_sage(pipe)
        _PIPES["edit"] = pipe
        print("[remote-gpu] edit pipeline ready.")
    return _PIPES["edit"]


def _load_wan(mode: str):
    from diffusers import AutoencoderKLWan, WanImageToVideoPipeline, WanPipeline

    print(f"[remote-gpu] loading Wan {mode.upper()} pipeline {VIDEO_MODEL} (first load downloads ~20-30GB) …")
    vae = AutoencoderKLWan.from_pretrained(
        VIDEO_MODEL,
        subfolder="vae",
        torch_dtype=torch.float32,
        cache_dir=str(REMOTE_GPU_HF_CACHE),
        revision=_model_revision(VIDEO_MODEL),
    )
    cls = WanPipeline if mode == "t2v" else WanImageToVideoPipeline
    return cls.from_pretrained(
        VIDEO_MODEL,
        vae=vae,
        torch_dtype=torch.bfloat16,
        cache_dir=str(REMOTE_GPU_HF_CACHE),
        add_watermarker=False,
        revision=_model_revision(VIDEO_MODEL),
    )


def _load_hunyuan(mode: str):
    import diffusers

    if not HUNYUAN_VIDEO_MODEL:
        raise RuntimeError("HUNYUAN_VIDEO_MODEL is not configured")
    print(f"[remote-gpu] loading Hunyuan {mode.upper()} pipeline {HUNYUAN_VIDEO_MODEL} …")
    names = (["HunyuanVideo15ImageToVideoPipeline", "HunyuanVideoImageToVideoPipeline"]
             if mode == "i2v" else ["HunyuanVideo15Pipeline", "HunyuanVideoPipeline"])
    for name in names:
        cls = getattr(diffusers, name, None)
        if cls is not None:
            return cls.from_pretrained(
                HUNYUAN_VIDEO_MODEL,
                torch_dtype=torch.bfloat16,
                cache_dir=str(REMOTE_GPU_HF_CACHE),
                add_watermarker=False,
                revision=_model_revision(HUNYUAN_VIDEO_MODEL),
            )
    from diffusers import DiffusionPipeline

    return DiffusionPipeline.from_pretrained(
        HUNYUAN_VIDEO_MODEL,
        torch_dtype=torch.bfloat16,
        cache_dir=str(REMOTE_GPU_HF_CACHE),
        add_watermarker=False,
        revision=_model_revision(HUNYUAN_VIDEO_MODEL),
    )


def _free_disk_gb(path: str | Path = REMOTE_GPU_ROOT) -> float:
    import shutil

    return shutil.disk_usage(path).free / 1e9


def _load_ltx(mode: str):
    """LTX-2: the only open-weights family that generates synced audio.

    Checks free disk first. The 2.x checkpoints are 150-200 GB; a standard 80 GB
    GPU runtime has well under that once Qwen and Wan are cached, and the failure
    without this check is a download that runs for many minutes and then dies
    with no space left — after evicting whatever pipeline was resident.
    """
    import diffusers

    if not LTX_VIDEO_MODEL:
        raise RuntimeError("LTX_VIDEO_MODEL is not configured")

    free = _free_disk_gb()
    if free < 60:
        raise HTTPException(
            status_code=507,
            detail=(f"only {free:.0f} GB free — LTX-2 checkpoints are 150-200 GB. "
                    "Free space or use a runtime with a larger disk; the download "
                    "would fail partway and take the resident pipeline with it."),
        )

    print(f"[remote-gpu] loading LTX {mode.upper()} pipeline {LTX_VIDEO_MODEL} "
          f"({free:.0f} GB free) …")
    names = (["LTX2ImageToVideoPipeline", "LTXImageToVideoPipeline"]
             if mode == "i2v" else ["LTX2Pipeline", "LTXPipeline"])
    for name in names:
        cls = getattr(diffusers, name, None)
        if cls is not None:
            return cls.from_pretrained(
                LTX_VIDEO_MODEL,
                torch_dtype=torch.bfloat16,
                cache_dir=str(REMOTE_GPU_HF_CACHE),
                add_watermarker=False,
                revision=_model_revision(LTX_VIDEO_MODEL),
            )
    from diffusers import DiffusionPipeline

    return DiffusionPipeline.from_pretrained(
        LTX_VIDEO_MODEL,
        torch_dtype=torch.bfloat16,
        cache_dir=str(REMOTE_GPU_HF_CACHE),
        add_watermarker=False,
        revision=_model_revision(LTX_VIDEO_MODEL),
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
    if OFFLOAD:
        pipe.enable_model_cpu_offload()
    else:
        pipe.to("cuda")
    if hasattr(pipe, "vae") and hasattr(pipe.vae, "enable_tiling"):
        pipe.vae.enable_tiling()
    # The Lightning LoRA is a Wan adapter; applying it to another engine would
    # either fail or silently distort.
    if engine == "wan":
        _PIPES["video_speed"] = _try_lightning(pipe, VIDEO_LIGHTNING_LORA, "video")
    else:
        _PIPES["video_speed"] = False
    _try_fbcache(pipe)
    _try_sage(pipe)
    _PIPES.update(video=pipe, video_mode=mode, video_engine=engine)
    print("[remote-gpu] video pipeline ready.")
    return pipe


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
        import hmac

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"x-gen-secret", b"").decode("latin-1")
        if not hmac.compare_digest(supplied, self.secret):
            await self._respond(send, 401, "bad or missing X-Gen-Secret")
            return
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


app.add_middleware(_RemoteIngressMiddleware, secret=REMOTE_GPU_SHARED_SECRET)


def _auth(secret: str | None) -> None:
    import hmac
    if not hmac.compare_digest(secret or "", REMOTE_GPU_SHARED_SECRET):
        raise HTTPException(status_code=401, detail="bad or missing X-Gen-Secret")


# ---- async job infra -------------------------------------------------------
# Generation/first-load can take minutes; a single long HTTP request would hit
# Cloudflare's ~100s tunnel timeout (error 524) applies when the optional tunnel
# is used. POSTs therefore enqueue and return a
# token immediately; the client polls the short /result/{token} until done.
import queue as _queue
import threading
import time
import uuid

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
        if PREVIEW_EVERY and (step + 1) % PREVIEW_EVERY == 0 and (step + 1) < total:
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
    # to the quality slot rather than failing — see image_model_for().
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


def _wan_negative() -> str:
    return ("色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，"
            "最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，"
            "画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，"
            "杂乱的背景，三条腿，背景人很多，倒着走")


def _encode_video(frames, fps: int) -> str:
    from diffusers.utils import export_to_video

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False, dir=REMOTE_GPU_TMP) as f:
        path = f.name
    try:
        export_to_video(frames, path, fps=fps)
        with open(path, "rb") as fh:
            data = fh.read()
    finally:
        Path(path).unlink(missing_ok=True)
    return base64.b64encode(data).decode()


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
    if _supports_flf2v(VIDEO_MODEL):
        features.append("flf2v")
    if IMAGE_LIGHTNING_LORA:
        features.append("speed_image")
    if EDIT_LIGHTNING_LORA:
        features.append("speed_edit")
    if VIDEO_LIGHTNING_LORA:
        features.append("speed_video")
    if HUNYUAN_VIDEO_MODEL:
        features.append("hunyuan")
    if LTX_VIDEO_MODEL:
        features.append("ltx")
    if A100_IMAGE_MODEL_ALT:
        features.append("image_alt")
    if A100_IMAGE_MODEL_HIDREAM:
        features.append("image_hidream")
    loaded_models = []
    if _PIPES.get("image") is not None:
        loaded_models.append(image_model_for(str(_PIPES.get("image_variant") or "quality")))
    if _PIPES.get("edit") is not None:
        loaded_models.append(QWEN_EDIT_MODEL)
    if _PIPES.get("video") is not None:
        video_engine = str(_PIPES.get("video_engine") or "wan")
        loaded_models.append({"hunyuan": HUNYUAN_VIDEO_MODEL,
                              "ltx": LTX_VIDEO_MODEL}.get(video_engine, VIDEO_MODEL))
    configured_models = [
        (A100_IMAGE_MODEL, "Image", "image"),
        (A100_IMAGE_MODEL_HIDREAM, "HiDream image", "image"),
        (A100_IMAGE_MODEL_ALT, "Alternate image", "image"),
        (QWEN_EDIT_MODEL, "Image edit", "image-edit"),
        (VIDEO_MODEL, "Wan video", "video"),
        (HUNYUAN_VIDEO_MODEL, "Hunyuan video", "video"),
        (LTX_VIDEO_MODEL, "LTX video", "video"),
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
        "disk_free_gb": round(_free_disk_gb(), 1),
        "gpu": name,
        "image_model": A100_IMAGE_MODEL,
        "image_model_hidream": A100_IMAGE_MODEL_HIDREAM,
        "image_model_alt": A100_IMAGE_MODEL_ALT,
        # Which one is resident right now, so the app can warn that a switch
        # costs a reload before the user queues one.
        "image_variant": _PIPES.get("image_variant"),
        "video_model": VIDEO_MODEL,
        "edit_model": QWEN_EDIT_MODEL,
        "models_loaded": loaded_models,
        "models": [
            {"id": model, "label": label, "kind": kind,
             "ready": model in loaded_models,
             "status": "ready" if model in loaded_models else "unknown"}
            for model, label, kind in configured_models if model
        ],
        "image_loaded": _PIPES["image"] is not None,
        "video_mode": _PIPES["video_mode"],
        "features": features,
        "build": REMOTE_GPU_BUILD,
        "speed_loaded": {
            "image": bool(_PIPES.get("image_speed")),
            "edit": bool(_PIPES.get("edit_speed")),
            "video": bool(_PIPES.get("video_speed")),
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
        import inspect

        _raise_if_canceled(token)
        pipe = get_image_pipe(req.model_variant)
        if req.speed:
            _require_speed("image")
        _set_speed(pipe, req.speed)
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
                if PREVIEW_EVERY and (step + 1) % PREVIEW_EVERY == 0:
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
            free_memory()  # free per image so a large batch can't accumulate to an OOM
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
        import inspect

        _raise_if_canceled(token)
        pipe = get_edit_pipe()
        if req.speed:
            _require_speed("edit")
        _set_speed(pipe, req.speed)
        sig = inspect.signature(pipe.__call__).parameters
        imgs = [ImageOps.exif_transpose(_decode_image(b)).convert("RGB") for b in req.images_b64[:3]]
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
        free_memory()
        return {"image_b64": base64.b64encode(buf.getvalue()).decode()}

    return _enqueue(run, req.client_job_id)


def _video_run(req: VideoReq, mode: str):
    def run(token):
        import inspect

        _raise_if_canceled(token)
        pipe = get_video_pipe(mode, req.engine)
        if req.engine != "hunyuan":
            if req.speed:
                _require_speed("video")
            _set_speed(pipe, req.speed)
        sig = inspect.signature(pipe.__call__).parameters

        if mode == "i2v":
            img = ImageOps.exif_transpose(_decode_image(req.image_b64))
            orient = ("portrait" if img.height > img.width
                      else ("square" if img.height == img.width else "landscape"))
            w, h = res_for(req.resolution, orient)
            first = fit_image(img, w, h)
        else:
            w, h = res_for(req.resolution, req.orientation)
            first = None

        neg = req.negative_prompt or (_wan_negative() if req.engine != "hunyuan" else "")
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
            if not (_supports_flf2v(VIDEO_MODEL) and "last_image" in sig):
                raise HTTPException(
                    status_code=409,
                    detail=(f"{VIDEO_MODEL} does not support first+last-frame "
                            "conditioning: it has no image_encoder, so a last frame "
                            "has nothing to condition. Use a Wan FLF2V checkpoint, "
                            "or drop the last frame."),
                )
            call["last_image"] = fit_image(_decode_image(req.last_image_b64), w, h)
        frames = pipe(**call).frames[0]
        out = _encode_video(frames, req.fps)
        free_memory()
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


# ══════════════════ CELL 3 — launch (paste everything below into a new cell, run last) ══════════════════
# ---- launcher: uvicorn + optional cloudflared tunnel -----------------------
def _launch() -> None:
    import re
    import threading
    import time

    import nest_asyncio
    import uvicorn

    nest_asyncio.apply()
    threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning"),
        daemon=True,
    ).start()
    time.sleep(4)
    print("Local server up on :8000")

    proc = subprocess.Popen(
        [
            str(REMOTE_GPU_TOOLS / "cloudflared-2026.8.3"), "tunnel", "--url",
            "http://localhost:8000", "--no-autoupdate",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    for line in proc.stdout:  # type: ignore[union-attr]
        m = re.search(r"https://[-a-z0-9]+\.trycloudflare\.com", line)
        if m:
            print("\n" + "=" * 70)
            print("  PUBLIC URL  ->  ", m.group(0))
            print("  Paste it into the app:  Settings -> Remote GPU URL")
            print("=" * 70)
            break
    proc.wait()  # block so the server keeps running


if __name__ == "__main__":
    _launch()
