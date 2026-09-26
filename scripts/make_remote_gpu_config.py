"""Emit the non-secret worker config that the container image bakes in.

Run via `make remote-gpu-config`, then `make remote-gpu-image`.

This used to inject secrets into a copy of a single-file worker, because the
deployment unit was a file you pasted into a notebook. The unit is a container
image now, and an image has to be publishable, so it carries only the half that
makes a build reproducible - the model slots and the reviewed revision pins.
HF_TOKEN and REMOTE_GPU_SHARED_SECRET arrive as deploy-time environment.

It carries no build fingerprint either: /health reports `worker.build_id()`,
computed from the package's own sources, so a config file cannot claim a build
the code is not running.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import settings
from backend.app.model_sources import MODEL_REVISIONS


def remote_gpu_config() -> dict:
    """Everything the worker reads via `config.cfg()`, in one place.

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


def write_config(path: pathlib.Path) -> None:
    """Write the payload. Not 0600: it holds no secret, and a locked-down file
    inside an image that later runs as another user is a support ticket."""
    payload = {"config": remote_gpu_config()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {path} (no secrets; set HF_TOKEN and REMOTE_GPU_SHARED_SECRET at deploy time)")


def main() -> None:
    out = (pathlib.Path(sys.argv[1]) if len(sys.argv) > 1
           else ROOT / "docker" / "remote-gpu" / "remote-gpu-config.json")
    write_config(out)


if __name__ == "__main__":
    main()
