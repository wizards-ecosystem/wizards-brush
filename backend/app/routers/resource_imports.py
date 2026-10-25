"""Provider imports over HTTP: inspect a link, then import an exact choice.

Two named steps with different side effects. `inspect` reads provider
metadata and nothing else: no row, no download. Creating an import binds an
exact file or repository commit and queues a durable download task; it needs
a concrete selection when the link is ambiguous and an explicit licence
acknowledgement when the licence is unknown.
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import client_tag
from ..resources import imports, providers
from .common import error_responses

router = APIRouter(tags=["resources"])


class InspectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    link: str = Field(min_length=1, max_length=2000, description="A huggingface.co or "
                      "civitai.com link, or hf:<repo>[@rev] / civitai:model/<id>/...")


class ImportCreate(InspectBody):
    selection: str | None = Field(
        default=None, description="A candidate id from inspect. Required when the link offers "
                                  "more than one usable file or version.")
    kind: Literal["adapter", "model"] | None = Field(
        default=None, description="For a single Hugging Face file: adapter or model.")
    acknowledge_license: bool = Field(
        default=False, description="Required when the licence is unknown: you reviewed it.")


@router.post("/resource-imports/inspect", responses=error_responses(400, 404))
async def inspect(body: InspectBody) -> dict[str, Any]:
    """What a link points at: exact versions, files, hashes, licence and access.
    Reads provider metadata only."""
    return await asyncio.to_thread(providers.inspect, body.link)


@router.post("/resource-imports", responses=error_responses(400, 404, 409))
async def create_import(body: ImportCreate) -> dict[str, Any]:
    """Queue the download of one exact file or repository commit."""
    return await asyncio.to_thread(
        imports.create, body.link, selection=body.selection, kind=body.kind,
        acknowledge_license=body.acknowledge_license, client=client_tag.current())


@router.get("/resource-imports")
async def list_imports(limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[dict[str, Any]]:
    return imports.listing(limit)


@router.get("/resource-imports/{import_id}", responses=error_responses(404))
async def read_import(import_id: int) -> dict[str, Any]:
    return imports.get(import_id)


@router.post("/resource-imports/{import_id}/cancel", responses=error_responses(404))
async def cancel_import(import_id: int) -> dict[str, Any]:
    return imports.cancel(import_id)


@router.post("/resource-imports/{import_id}/retry", responses=error_responses(404, 409))
async def retry_import(import_id: int) -> dict[str, Any]:
    """Run a failed, interrupted or canceled import again, resuming its partial
    file when the provider supports ranged requests."""
    return imports.retry(import_id)
