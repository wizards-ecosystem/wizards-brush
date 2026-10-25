"""Reference sets over HTTP: revisioned, ordered references by role.

Saving is a compare-and-set: an update names the `base_revision` it started
from, and a save from an older revision is a 409 rather than a silent
overwrite. Views carry asset ids, never file paths.
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import client_tag, references
from .common import error_responses

router = APIRouter(tags=["references"])


class ReferenceSetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    definition: dict[str, Any] = Field(
        description="{description, notes, roles: [{role, label, entries: [{asset_id, mask_id, "
                    "note}]}], representatives}")


class ReferenceSetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]
    base_revision: int = Field(ge=1, description="The revision this edit started from.")
    name: str | None = Field(default=None, min_length=1, max_length=80)


@router.get("/reference-sets")
async def list_sets(include_archived: bool = False) -> list[dict[str, Any]]:
    return await asyncio.to_thread(references.listing, include_archived=include_archived)


@router.post("/reference-sets", responses=error_responses(400, 404, 409))
async def create_set(body: ReferenceSetCreate) -> dict[str, Any]:
    """Create a set from gallery assets, as revision 1."""
    return await asyncio.to_thread(references.create, body.name, body.definition,
                                   client=client_tag.current())


@router.get("/reference-sets/{set_id}", responses=error_responses(404))
async def read_set(set_id: int,
                   revision: Annotated[int | None, Query(ge=1)] = None) -> dict[str, Any]:
    return await asyncio.to_thread(references.get, set_id, revision)


@router.put("/reference-sets/{set_id}", responses=error_responses(400, 404, 409))
async def update_set(set_id: int, body: ReferenceSetUpdate) -> dict[str, Any]:
    """Save a new revision. Plans that used an earlier revision keep it."""
    return await asyncio.to_thread(references.update, set_id, body.definition,
                                   base_revision=body.base_revision, name=body.name,
                                   client=client_tag.current())


@router.post("/reference-sets/{set_id}/archive", responses=error_responses(404))
async def archive_set(set_id: int) -> dict[str, Any]:
    return await asyncio.to_thread(references.archive, set_id, True)


@router.post("/reference-sets/{set_id}/unarchive", responses=error_responses(404, 409))
async def unarchive_set(set_id: int) -> dict[str, Any]:
    return await asyncio.to_thread(references.archive, set_id, False)
