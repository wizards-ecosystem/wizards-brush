"""LoRA catalogue: what the user has dropped into LORA_DIR."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter

from ..loras import MAX_ACTIVE, WEIGHT_MAX, WEIGHT_MIN, list_loras

router = APIRouter(tags=["loras"])


@router.get("/loras")
async def get_loras(model_variant: str | None = None) -> dict:
    """Available adapters plus the limits the UI should enforce, so the caps
    live in one place instead of being duplicated in the client.

    `model_variant` tags each adapter with whether it suits the model that
    variant resolves to, so the picker can group compatible ones first. Omit it
    and everything comes back marked compatible — no target, no judgement.
    """
    model = None
    if model_variant:
        from ..generators.local_image import resolve_model

        model = resolve_model(model_variant)
    # A large library requires one directory walk plus safetensors-header
    # and sidecar reads. Keep that blocking filesystem work off the event
    # loop carrying job progress and previews.
    items = await asyncio.to_thread(list_loras, model)
    # The content identity of each file, once the catalogue has hashed it.
    # None means "not verified yet", never "unknown file".
    from ..resources import store

    by_location = {i.location: i.resource_id for i in store.installs(target="local")
                   if i.location.startswith("loras:") and i.state != "missing"}
    for item in items:
        item["resource_id"] = by_location.get(f"loras:{Path(item['path']).as_posix()}")
    return {
        "items": items,
        "model": model,
        "max_active": MAX_ACTIVE,
        "weight_min": WEIGHT_MIN,
        "weight_max": WEIGHT_MAX,
    }
