"""Shipped Variant Set recipes: Portrait pack and Product cutout.

These are configured workflows, not a new engine. They appear as ordinary
saved recipes (created once on startup if missing) so they go through the same
compiler, finishing and validation as anything a person wrote.
"""
from __future__ import annotations

from typing import Any

from ..validators import cutout_spec
from . import store

PORTRAIT_PACK = {
    "version": 1,
    "sources": [],
    "masks": {},
    "content": {
        "look": {
            "knit": "a terracotta knit sweater with natural drape and visible stitch",
            "shirt": "a white cotton shirt, fabric weave readable, natural collar roll",
        },
        "hair": {
            "loose": "loose natural hair, individual strands, no beauty-filter smoothness",
            "ponytail": "a low nape ponytail, hairline and flyaways preserved",
        },
    },
    "seed": {"mode": "per_variant", "value": -1},
    "stages": [
        {
            "name": "Wardrobe",
            "operation": "image_edit",
            "axes": [{"name": "look", "values": ["knit", "shirt"]}],
            "prompt": "{{look}}",
            "params": {
                "input_fidelity": "high",
                "finish": "photoreal",
                "quality": "Draft",
                "region": "upper-clothes",
                "background": "opaque",
            },
            "mask": None,
            "finishing": [],
            "validation": {"unmasked_match": True, "face_identity_min": 0.32},
        },
        {
            "name": "Hair",
            "operation": "image_edit",
            "axes": [{"name": "hair", "values": ["loose", "ponytail"]}],
            "prompt": "{{hair}}",
            "params": {
                "input_fidelity": "high",
                "finish": "photoreal",
                "quality": "Draft",
                "region": "hair",
            },
            "finishing": [],
            "validation": {"unmasked_match": True, "face_identity_min": 0.32},
        },
    ],
}

PRODUCT_CUTOUT = {
    "version": 1,
    "sources": [],
    "masks": {},
    "content": {},
    "seed": {"mode": "fixed", "value": -1},
    "stages": [
        {
            "name": "Cutout",
            "operation": "image_edit",
            "axes": [],
            "prompt": "the product isolated for a catalogue, true materials and lighting",
            "params": {
                "finish": "photoreal",
                "quality": "High",
                "background": "transparent",
                "input_fidelity": "high",
            },
            "finishing": [{"processor": "background_removal"}],
            "validation": cutout_spec().model_dump(),
        },
    ],
}

SHIPPED: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("Portrait pack",
     "High-fidelity clothing and hair edits that lock the face. Lightning speed.",
     PORTRAIT_PACK),
    ("Product cutout",
     "Catalogue cutout with real alpha, coverage and a safe margin — not four corners.",
     PRODUCT_CUTOUT),
)


def ensure_shipped() -> int:
    """Insert the two flagship recipes if this database does not already have them."""
    have = {r.name for r in store.list_recipes()}
    created = 0
    for name, description, recipe in SHIPPED:
        if name in have:
            continue
        store.save_recipe(name, description, recipe)
        created += 1
    return created
