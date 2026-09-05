"""Prompt history, user-created presets, and library stats.

These live in their own tables so 'Clear all' (which wipes assets + jobs) never
deletes the user's saved prompts and presets.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from .. import db
from ..models import PromptHistoryRead, UserPresetRead

router = APIRouter(tags=["library"])


# ---- prompt history -------------------------------------------------------
@router.get("/history")
async def list_history(limit: int = 100, favorite: bool | None = None) -> list[PromptHistoryRead]:
    return [PromptHistoryRead.of(h) for h in db.list_history(limit=limit, favorite=favorite)]


class HistFav(BaseModel):
    favorite: bool


@router.post("/history/{hist_id}/favorite")
async def fav_history(hist_id: int, req: HistFav) -> dict:
    return {"ok": db.set_history_favorite(hist_id, req.favorite)}


@router.delete("/history/{hist_id}")
async def delete_history(hist_id: int) -> dict:
    return {"ok": db.delete_history(hist_id)}


# ---- user presets ---------------------------------------------------------
class PresetCreate(BaseModel):
    type: str = "prompt"          # prompt | negative | style | params | search
    name: str
    payload: dict = {}
    scope: str = "all"            # generator id, or "all"


@router.get("/presets/user")
async def list_user_presets(type: str | None = None) -> list[UserPresetRead]:
    presets = db.list_user_presets(type=type)
    previews = db.preset_previews(presets)
    return [UserPresetRead.of(p, previews.get(p.id or -1)) for p in presets]


class PresetPreview(BaseModel):
    asset_id: int | None = None   # None clears the thumbnail


@router.post("/presets/user/{preset_id}/preview")
async def set_preset_preview(preset_id: int, req: PresetPreview) -> UserPresetRead:
    """Star a gallery image as this preset's thumbnail.

    Nothing is generated and nothing new is stored — the preset points at an
    image that already exists, so a style becomes something you can recognise
    rather than a name in a list.
    """
    from fastapi import HTTPException

    if req.asset_id is not None and db.get_asset(req.asset_id) is None:
        raise HTTPException(status_code=404, detail="asset not found")
    p = db.set_preset_preview(preset_id, req.asset_id)
    if p is None:
        raise HTTPException(status_code=404, detail="preset not found")
    return UserPresetRead.of(p, db.preset_previews([p]).get(p.id or -1))


@router.post("/presets/user")
async def create_user_preset(req: PresetCreate) -> UserPresetRead:
    from fastapi import HTTPException

    if not req.name.strip():
        raise HTTPException(status_code=400, detail="name required")
    # "search" reuses this table rather than adding one: a saved search is a
    # named payload that must survive `clear_all()`, which is exactly what
    # UserPreset already is.
    if req.type not in ("prompt", "negative", "style", "params", "search"):
        raise HTTPException(status_code=400, detail=f"unknown preset type '{req.type}'")
    p = db.add_user_preset(req.type, req.name.strip(), req.payload, req.scope)
    return UserPresetRead.of(p)


@router.delete("/presets/user/{preset_id}")
async def delete_user_preset(preset_id: int) -> dict:
    return {"ok": db.delete_user_preset(preset_id)}


# ---- library stats --------------------------------------------------------
@router.get("/stats")
async def stats() -> dict:
    # stats() walks the whole output tree (glob + stat); keep that disk I/O off
    # the event loop so a large library never stalls the jobs WebSocket.
    return await asyncio.to_thread(db.stats)
