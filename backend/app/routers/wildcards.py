"""CRUD for __wildcard__ files (wildcards/*.txt) used by the prompt engine."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import prompt_engine as pe

router = APIRouter(tags=["wildcards"])


@router.get("/wildcards")
async def list_wildcards() -> list[dict]:
    return pe.list_wildcards()


@router.get("/wildcards/{name}")
async def get_wildcard(name: str) -> dict:
    clean = _clean(name)
    items = pe.load_wildcard(clean)
    if not items and not (pe.WILDCARDS_DIR / f"{clean}.txt").exists():
        raise HTTPException(status_code=404, detail="wildcard not found")
    return {"name": clean, "items": items}


class WildcardBody(BaseModel):
    items: list[str]


@router.post("/wildcards/{name}")
async def save_wildcard(name: str, body: WildcardBody) -> dict:
    try:
        pe.save_wildcard(_clean(name), body.items)
    except ValueError as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    return {"ok": True, "name": _clean(name), "count": len([i for i in body.items if i.strip()])}


@router.delete("/wildcards/{name}")
async def delete_wildcard(name: str) -> dict:
    return {"ok": pe.delete_wildcard(_clean(name))}


def _clean(name: str) -> str:
    try:
        return pe.safe_wildcard_name(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
