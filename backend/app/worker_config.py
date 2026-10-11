"""The non-secret half of the Remote GPU worker's configuration, in one place.

Two consumers, one payload. `scripts/make_remote_gpu_config.py` bakes it into
an image you build yourself; the Runpod provisioner hands it to a pod as
environment, which the worker reads *before* its baked file. That second path
is what lets the published image - built from the shipped defaults, with no
`.env` in sight - run this install's model slots: without it a `.env` that
named an alternate image model configured nothing a pod ever saw, and the
picker that depends on the worker advertising the slot never appeared.

Secrets are not here by construction. HF_TOKEN and REMOTE_GPU_SHARED_SECRET are
added by the deployment, never by this module.
"""
from __future__ import annotations

from .config import settings
from .model_sources import MODEL_REVISIONS


def payload() -> dict:
    """Everything the worker reads via `config.cfg()`, under the names it reads.

    Exposed rather than inlined so the test that checks the worker reads no key
    this omits can compare against the real payload instead of a hand-copied
    list - which is precisely what went stale when LTX was added.
    """
    return {
        "a100_image_model": settings.a100_image_model,
        "a100_image_model_hidream": settings.a100_image_model_hidream,
        "a100_image_model_alt": settings.a100_image_model_alt,
        "qwen_edit_model": settings.qwen_edit_model,
        "video_model": settings.video_model,
        "hunyuan_video_model": settings.hunyuan_video_model,
        "ltx_video_model": settings.ltx_video_model,
        "image_lightning_lora": settings.image_lightning_lora,
        "edit_lightning_lora": settings.edit_lightning_lora,
        "video_lightning_lora": settings.video_lightning_lora,
        "preview_every": settings.preview_every,
        "fbcache_threshold": settings.fbcache_threshold,
        "enable_sage_attention": settings.enable_sage_attention,
        "model_revisions": MODEL_REVISIONS,
    }


def deploy_env() -> dict[str, str]:
    """The payload as the environment a pod receives.

    Scalars only: the worker's `cfg()` reads `KEY.upper()` from the environment
    and parses booleans as the string "true", so `str()` of each value is the
    wire format. The revision pins are a mapping and stay in the baked file -
    they only exist for the shipped defaults, and a slot an install points at
    another repository follows that repository's current revision.
    """
    return {key.upper(): str(value) for key, value in payload().items()
            if not isinstance(value, dict)}
