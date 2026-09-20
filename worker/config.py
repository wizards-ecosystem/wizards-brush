"""What this worker is configured to run, and the secret that guards it.

Secrets come from the environment, never from source. The previous design
injected them into a copy of the worker with an AST rewrite, because the
deployment unit was a file you pasted into a notebook; an image has to be
publishable, so it carries only the reproducible half — model slots and
reviewed revision pins — and the deployment supplies the rest.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import runtime

# Baked at image build time by `make remote-gpu-config`. Non-secret by
# construction: model repositories and the revision pins that make a build
# reproducible instead of "whatever the Hub served that day".
BUILD = ""
_FILE_CONFIG: dict = {}
_CONFIG_FILE = os.environ.get("REMOTE_GPU_CONFIG_FILE", "").strip()
if _CONFIG_FILE:
    _loaded = json.loads(Path(_CONFIG_FILE).read_text(encoding="utf-8"))
    if not isinstance(_loaded, dict):
        raise SystemExit(f"!! {_CONFIG_FILE} is not a JSON object")
    _FILE_CONFIG = _loaded.get("config") or {}
    if not isinstance(_FILE_CONFIG, dict):
        raise SystemExit(f"!! {_CONFIG_FILE} has a non-object 'config'")
    BUILD = str(_loaded.get("build", ""))


def cfg(key: str, default: str = "", *, legacy: str = "") -> str:
    """Config precedence: environment > baked image config > default.

    The environment wins so one image can be retargeted at deploy time, which is
    most of the reason to build an image at all.

    Membership tests, not truthiness: `or` would make a legitimate 0 or "" fall
    through to the default, so PREVIEW_EVERY=0 could not turn previews off and
    FBCACHE_THRESHOLD=0 could not disable the cache.
    """
    for name in (key, legacy):
        if not name:
            continue
        if name.upper() in os.environ:
            return str(os.environ[name.upper()])
        if name in _FILE_CONFIG and _FILE_CONFIG[name] is not None:
            return str(_FILE_CONFIG[name])
    return default


# The `a100_*` names are what the app's generated config still emits, so they
# stay readable here while the honest names take precedence. Nothing in this
# worker has required an A100 since it became a container: the live check is
# VRAM at load time, not a model number.
IMAGE_MODEL = cfg("image_model", "Qwen/Qwen-Image-2512", legacy="a100_image_model")
# HiDream is not a Diffusers repository, so it gets its own slot rather than the
# generic ALT one — that keeps the loader able to say so before a 35 GB download.
IMAGE_MODEL_HIDREAM = cfg("image_model_hidream", "HiDream-ai/HiDream-O1-Image",
                          legacy="a100_image_model_hidream")
# Empty = the session offers only the primary model, and /health says so, so the
# app never shows a picker this worker cannot serve.
IMAGE_MODEL_ALT = cfg("image_model_alt", "", legacy="a100_image_model_alt")
EDIT_MODEL = cfg("qwen_edit_model", "Qwen/Qwen-Image-Edit-2511")
VIDEO_MODEL = cfg("video_model", "Wan-AI/Wan2.2-TI2V-5B-Diffusers")
HUNYUAN_VIDEO_MODEL = cfg("hunyuan_video_model", "")
LTX_VIDEO_MODEL = cfg("ltx_video_model", "")
IMAGE_LIGHTNING_LORA = cfg("image_lightning_lora", "")
EDIT_LIGHTNING_LORA = cfg("edit_lightning_lora", "")
VIDEO_LIGHTNING_LORA = cfg("video_lightning_lora", "")
FBCACHE_THRESHOLD = float(cfg("fbcache_threshold", "0.05").strip() or 0)
ENABLE_SAGE_ATTENTION = cfg("enable_sage_attention", "false").lower() == "true"
PREVIEW_EVERY = int(cfg("preview_every", "2").strip() or 0)
OFFLOAD = cfg("remote_gpu_offload", "false", legacy="a100_offload").lower() == "true"

HF_TOKEN = os.environ.get("HF_TOKEN", "")
SHARED_SECRET = os.environ.get("REMOTE_GPU_SHARED_SECRET", "")

# The service is reachable at a public HTTPS address the moment it starts, and
# the platform puts no authentication in front of it. An empty secret would make
# `X-Gen-Secret: ""` authenticate, so refuse to boot rather than serve a GPU and
# an arbitrary-model downloader to anyone who finds the hostname.
_WEAK_SECRETS = {"", "change-me-to-anything", "changeme", "secret", "test"}
if SHARED_SECRET.strip().lower() in _WEAK_SECRETS:
    raise SystemExit(
        "!! REMOTE_GPU_SHARED_SECRET is empty or a default placeholder.\n"
        "   Set it in the deployment environment, e.g. `openssl rand -hex 32`.\n"
        "   See docs/runpod.md."
    )

MODEL_REVISIONS = _FILE_CONFIG.get("model_revisions") or {}
if not isinstance(MODEL_REVISIONS, dict) or not MODEL_REVISIONS:
    raise SystemExit(
        "!! The worker has no reviewed model revisions, so every download would\n"
        "   take whatever the Hub is serving today.\n"
        "   Rebuild the image after `make remote-gpu-config`, or point\n"
        "   REMOTE_GPU_CONFIG_FILE at its output."
    )

os.environ["HF_TOKEN"] = HF_TOKEN
os.environ["HUGGING_FACE_HUB_TOKEN"] = HF_TOKEN


def model_revision(repo_id: str) -> str | None:
    revision = MODEL_REVISIONS.get(repo_id)
    return str(revision) if revision else None


HIDREAM_CODE = runtime.TOOLS / "hidream-o1"
HIDREAM_CODE_REV = "2c2d29ff729e48f33e41f49edfdbd81d5ac103b4"
