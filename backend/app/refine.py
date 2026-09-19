"""Turn a one-sentence Refine request into an ordinary inpaint or restyle job.

Not a chatbot and not a persisted job kind. The API facade (`kind=refine`)
parses a region from the sentence, builds an SCHP/BiSeNet mask when the change
is local, and submits `inpaint_remote` or `image_edit` with high fidelity.
"""
from __future__ import annotations

import re
from typing import Any

from PIL import Image, ImageOps

REGION_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mouth", ("smile", "mouth", "lips", "grin", "expression", "closed-mouth",
               "open mouth", "frown")),
    ("eyes", ("eyes", "eyelid", "gaze", "wink")),
    ("hair", ("hair", "ponytail", "bun", "bangs", "fringe", "braid", "updo",
              "nape", "parting")),
    ("upper-clothes", ("shirt", "knit", "sweater", "jacket", "coat", "blouse",
                       "hoodie", "top", "tee", "t-shirt", "clothes", "clothing",
                       "outfit", "garment", "dress")),
)

SCENE_HINTS = ("background", "studio", "backdrop", "scene", "lighting", "restyle",
               "style of", "as a painting")


def infer_region(prompt: str) -> str | None:
    """A named parsing region, or None when the sentence is a full-frame restyle."""
    text = f" {str(prompt or '').lower()} "
    scene = any(f" {hint} " in text or hint in text for hint in SCENE_HINTS)
    regional = any(any(h in text for h in hints) for _region, hints in REGION_HINTS)
    if scene and not regional:
        return None
    for region, hints in REGION_HINTS:
        if any(re.search(rf"\b{re.escape(hint)}\b", text) for hint in hints):
            return region
    return None


def is_restyle(prompt: str) -> bool:
    return infer_region(prompt) is None


def build_mask(image: Image.Image, prompt: str, *, region: str | None = None,
               grow: int = 4) -> Image.Image:
    from .generators import parsing

    wanted = (region or infer_region(prompt) or "subject_minus_face").strip().lower()
    return parsing.mask_for(ImageOps.exif_transpose(image), wanted, grow=grow)


def refine_defaults(prompt: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Settings the facade applies unless the caller already set them."""
    params = dict(raw or {})
    params.setdefault("input_fidelity", "high")
    params.setdefault("finish", "photoreal")
    params.setdefault("quality", "Draft")
    params.setdefault("speed_mode", True)
    if "strength" not in params:
        region = infer_region(prompt)
        params["strength"] = 0.55 if region in {"mouth", "eyes", "hair"} else 0.75
    params["prompt"] = str(prompt or "")[:2000]
    return params
