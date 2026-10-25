"""Canvas projects over HTTP: layered documents, flatten, and edit hand-off.

Saving is a compare-and-set on `base_revision`. Views carry asset ids, never
file paths. Flattening writes a new gallery image; editing a layer runs an
ordinary job through POST /api/jobs, and its output comes back as a new layer.
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import canvas, client_tag, db
from ..models import AssetRead
from .common import error_responses

router = APIRouter(tags=["canvas"])


class CanvasCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    document: dict[str, Any] = Field(
        description="{schema_version: 1, width, height, color_space: srgb, background, "
                    "layers: [{id, name, asset_id, mask_id, transform: {x, y, scale_x, "
                    "scale_y, rotation}, opacity, visible, blend_mode}]}")


class CanvasSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: dict[str, Any]
    base_revision: int = Field(ge=1, description="The revision this edit started from.")
    name: str | None = Field(default=None, min_length=1, max_length=80)


class FlattenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int | None = Field(default=None, ge=1)
    allow_missing: bool = Field(default=False, description="Leave missing layers out.")


class EditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer_id: str | None = Field(default=None, max_length=32,
                                 description="Edit this layer's source; omit to flatten first.")
    mask_id: int | None = Field(default=None, ge=1)
    revision: int | None = Field(default=None, ge=1)


class LayerAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=1)
    asset_id: int | None = Field(default=None, ge=1)
    job_id: int | None = Field(default=None, ge=1,
                               description="A finished job; its first output becomes the layer.")
    name: str = Field(default="", max_length=80)
    from_layer: str | None = Field(default=None, max_length=32,
                                   description="Take this layer's transform; recorded as lineage.")
    mask_id: int | None = Field(default=None, ge=1)


@router.get("/canvas")
async def list_projects(include_archived: bool = False) -> list[dict[str, Any]]:
    return await asyncio.to_thread(canvas.listing, include_archived=include_archived)


@router.post("/canvas", responses=error_responses(400, 404))
async def create_project(body: CanvasCreate) -> dict[str, Any]:
    return await asyncio.to_thread(canvas.create, body.name, body.document,
                                   client=client_tag.current())


@router.get("/canvas/{project_id}", responses=error_responses(404))
async def read_project(project_id: int,
                       revision: Annotated[int | None, Query(ge=1)] = None) -> dict[str, Any]:
    """A revision of the document. `missing` lists assets that are no longer in
    the gallery; their layers stay as placeholders."""
    return await asyncio.to_thread(canvas.get, project_id, revision)


@router.put("/canvas/{project_id}", responses=error_responses(400, 404, 409))
async def save_project(project_id: int, body: CanvasSave) -> dict[str, Any]:
    """Save the next revision. A stale `base_revision` is a 409 revision_conflict."""
    return await asyncio.to_thread(canvas.save, project_id, body.document,
                                   base_revision=body.base_revision, name=body.name,
                                   client=client_tag.current())


@router.post("/canvas/{project_id}/archive", responses=error_responses(404))
async def archive_project(project_id: int) -> dict[str, Any]:
    return await asyncio.to_thread(canvas.archive, project_id, True)


@router.post("/canvas/{project_id}/unarchive", responses=error_responses(404))
async def unarchive_project(project_id: int) -> dict[str, Any]:
    return await asyncio.to_thread(canvas.archive, project_id, False)


@router.delete("/canvas/{project_id}", responses=error_responses(404))
async def delete_project(project_id: int) -> dict[str, Any]:
    """Delete the project and its revisions. Gallery assets are never deleted."""
    return await asyncio.to_thread(canvas.delete, project_id)


@router.post("/canvas/{project_id}/flatten", responses=error_responses(404, 409))
async def flatten_project(project_id: int, body: FlattenBody | None = None) -> dict[str, Any]:
    """Render a revision into a new gallery PNG. The document is not changed."""
    body = body or FlattenBody()
    result = await asyncio.to_thread(canvas.flatten, project_id, body.revision,
                                     allow_missing=body.allow_missing)
    asset = db.get_asset(result["asset_id"])
    return {**result, "asset": AssetRead.of(asset).model_dump(mode="json") if asset else None}


@router.post("/canvas/{project_id}/edit-inputs", responses=error_responses(400, 404, 409))
async def edit_inputs(project_id: int, body: EditBody) -> dict[str, Any]:
    """The `inputs` for POST /api/jobs to edit a layer (or an explicit flatten).
    Creates no job."""
    return await asyncio.to_thread(canvas.edit_inputs, project_id, layer_id=body.layer_id,
                                   mask_id=body.mask_id, revision=body.revision)


@router.post("/canvas/{project_id}/layers", responses=error_responses(400, 404, 409))
async def add_layer(project_id: int, body: LayerAdd) -> dict[str, Any]:
    """Add an asset, or a finished job's output, as the new top layer."""
    return await asyncio.to_thread(
        canvas.add_layer, project_id, base_revision=body.base_revision, asset_id=body.asset_id,
        job_id=body.job_id, name=body.name, from_layer=body.from_layer, mask_id=body.mask_id,
        client=client_tag.current())
