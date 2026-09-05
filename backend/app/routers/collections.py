"""Collections: named groupings of assets.

A collection is a view over assets, never a container that owns them — deleting
one removes the grouping and its membership rows, and touches no files.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db
from ..models import CollectionRead

router = APIRouter(tags=["collections"])


class NameReq(BaseModel):
    name: str


class MembersReq(BaseModel):
    asset_ids: list[int]


@router.get("/collections")
async def list_collections() -> list[CollectionRead]:
    return [CollectionRead.of(c, n) for c, n in db.list_collections()]


@router.post("/collections")
async def create_collection(req: NameReq) -> CollectionRead:
    return CollectionRead.of(db.create_collection(req.name), 0)


@router.post("/collections/{collection_id}/rename")
async def rename_collection(collection_id: int, req: NameReq) -> dict:
    if not db.rename_collection(collection_id, req.name):
        raise HTTPException(status_code=404, detail="collection not found, or empty name")
    return {"ok": True}


@router.delete("/collections/{collection_id}")
async def delete_collection(collection_id: int) -> dict:
    """Removes the grouping only. The assets stay exactly where they were."""
    if not db.delete_collection(collection_id):
        raise HTTPException(status_code=404, detail="collection not found")
    return {"ok": True}


@router.post("/collections/{collection_id}/add")
async def add_members(collection_id: int, req: MembersReq) -> dict:
    return {"ok": True, "added": db.add_to_collection(collection_id, req.asset_ids)}


@router.post("/collections/{collection_id}/remove")
async def remove_members(collection_id: int, req: MembersReq) -> dict:
    return {"ok": True, "removed": db.remove_from_collection(collection_id, req.asset_ids)}
