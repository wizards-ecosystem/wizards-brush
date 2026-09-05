"""Install the curated image stack into the project-owned model stores.

The default ``local`` profile downloads every enabled local checkpoint plus a
small, general-purpose Z-Image art palette. The ``a100`` profile is useful when
this checkout itself runs on the accelerator host; the optional Remote GPU
downloads the same models and adapters lazily through ``remote_gpu.py``.

Examples:
    make models
    uv run python scripts/download_models.py --profile a100
    uv run python scripts/download_models.py --profile all
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.config import settings
from backend.app.generators import variants
from backend.app.generators.base import setup_hf_env
from backend.app.generators.vram import (
    DISK_RESERVE_BYTES,
    MIN_MODEL_FETCH_BYTES,
    model_download_shortfall,
)
from backend.app.model_sources import revision_for


@dataclass(frozen=True)
class LocalLora:
    repo_id: str
    weight_name: str
    target_name: str
    recommended_weight: float
    why: str


# A compact palette, not a dump of whichever adapters happen to be popular.
# Each adapter is Apache-2.0, trained specifically for the official Z-Image
# Turbo checkpoint, and adds a materially distinct art direction.
GENERAL_LOCAL_LORAS: tuple[LocalLora, ...] = (
    LocalLora(
        repo_id="tarn59/pixel_art_style_lora_z_image_turbo",
        weight_name="pixel_art_style_z_image_turbo.safetensors",
        target_name="zimage-pixel-art.safetensors",
        recommended_weight=1.0,
        why='Pixel-art rendering; add "Pixel art style." to the prompt.',
    ),
    LocalLora(
        repo_id="renderartist/Classic-Painting-Z-Image-Turbo-LoRA",
        weight_name="Classic_Painting_Z_Image_Turbo_v1_renderartist_1750.safetensors",
        target_name="zimage-classic-painting.safetensors",
        recommended_weight=0.8,
        why='Public-domain classic-painting texture; trigger with "class1cpa1nt".',
    ),
    LocalLora(
        repo_id="ostris/z_image_turbo_childrens_drawings",
        weight_name="z_image_turbo_childrens_drawings.safetensors",
        target_name="zimage-childrens-drawing.safetensors",
        recommended_weight=1.0,
        why="Turns a descriptive prompt into a loose children's drawing.",
    ),
)


# Pin the exact small adapter file. These repositories also carry fp32 adapters
# and full fp8 checkpoints; downloading a whole snapshot wastes tens of GB and
# used to let diffusers choose the wrong file at runtime.
A100_LIGHTNING_WEIGHTS: tuple[tuple[str, str], ...] = (
    (
        "lightx2v/Qwen-Image-2512-Lightning",
        "Qwen-Image-2512-Lightning-4steps-V1.0-bf16.safetensors",
    ),
    (
        "lightx2v/Qwen-Image-Edit-2511-Lightning",
        "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
    ),
)


def _assert_model_space(model: str) -> None:
    """Use the same cold-fetch guard as lazy generation before each snapshot."""
    shortfall = model_download_shortfall(model)
    if shortfall is None:
        return
    _required, free = shortfall
    raise RuntimeError(
        f"Refusing to download {model}: {free / 1024 ** 3:.1f} GB is free on "
        f"{settings.hf_hub_path}, but a new model needs at least "
        f"{MIN_MODEL_FETCH_BYTES / 1024 ** 3:.0f} GB plus the "
        f"{DISK_RESERVE_BYTES / 1024 ** 3:.0f} GB safety reserve. Free space and rerun.")


def _models(profile: str) -> list[str]:
    models: list[str] = []
    if profile in {"local", "all"}:
        models.extend(variants.repo_of(v) for v in variants.available("local"))
        if settings.enrich_captions:
            models.append("florence-community/Florence-2-base-ft")
        if settings.enable_controlnet:
            models.extend([settings.controlnet_model, "lllyasviel/Annotators"])
    if profile in {"a100", "all"}:
        models.extend([
            settings.a100_image_model,
            settings.a100_image_model_hidream,
            settings.a100_image_model_alt,
            settings.qwen_edit_model,
        ])
    return list(dict.fromkeys(model for model in models if model))


def _install_local_lora(item: LocalLora, *, token: str | None) -> None:
    from huggingface_hub import hf_hub_download

    source = Path(hf_hub_download(
        repo_id=item.repo_id,
        filename=item.weight_name,
        token=token,
        cache_dir=str(settings.hf_hub_path),
        revision=revision_for(item.repo_id),
    ))
    target = settings.loras_dir / item.target_name
    if not target.is_file() or target.stat().st_size != source.stat().st_size:
        print(f"Installing {item.repo_id}/{item.weight_name} -> {target.name} …")
        shutil.copyfile(source, target)
    sidecar = {
        "source": f"https://huggingface.co/{item.repo_id}",
        "repo_id": item.repo_id,
        "weight_name": item.weight_name,
        "revision": revision_for(item.repo_id),
        "license": "Apache-2.0",
        "variants": ["turbo"],
        "recommended_weight": item.recommended_weight,
        "why": item.why,
    }
    target.with_suffix(".json").write_text(
        json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", choices=("local", "a100", "all"), default="local",
        help="model store to populate (default: local)",
    )
    parser.add_argument(
        "--models-only", action="store_true",
        help="skip adapter installation",
    )
    args = parser.parse_args(argv)

    settings.ensure_dirs()
    setup_hf_env()
    from huggingface_hub import hf_hub_download, snapshot_download

    token = settings.hf_token or None
    for model in _models(args.profile):
        _assert_model_space(model)
        print(f"Downloading {model} into {os.environ['HF_HOME']} …")
        snapshot_download(
            repo_id=model, token=token, cache_dir=str(settings.hf_hub_path),
            revision=revision_for(model),
        )

    if not args.models_only and args.profile in {"local", "all"}:
        for item in GENERAL_LOCAL_LORAS:
            _install_local_lora(item, token=token)

    if not args.models_only and args.profile in {"a100", "all"}:
        for repo_id, weight_name in A100_LIGHTNING_WEIGHTS:
            print(f"Downloading {repo_id}/{weight_name} …")
            hf_hub_download(
                repo_id=repo_id,
                filename=weight_name,
                token=token,
                cache_dir=str(settings.hf_hub_path),
                revision=revision_for(repo_id),
            )

    print(f"Done. The {args.profile} image profile is available from the project cache.")


if __name__ == "__main__":
    main()
