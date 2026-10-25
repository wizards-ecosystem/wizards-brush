"""Resources over HTTP: the catalogue and safe local intake.

Importing is two named steps with separate side effects. An upload session
receives bytes in chunks no larger than `chunk_bytes` (each request stays far
below the server's request cap), and `complete` verifies size and hash into a
receipt. `POST /resources/import` then validates the file and installs it.
Nothing here downloads from a provider, and no request names a path on this
machine. Provider links arrive with their own routes (resource-imports).
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..problems import RequestError
from ..resources import catalogue, intake, store
from .common import error_responses

router = APIRouter(tags=["resources"])


class UploadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, description="Exact size in bytes.")
    kind: Literal["adapter", "model"]
    sha256: str | None = Field(default=None, description="Optional; checked on complete.")


class UploadSession(BaseModel):
    upload_id: str
    filename: str
    kind: str
    size: int
    received: int
    state: str
    sha256: str | None = None
    chunk_bytes: int


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt: str = Field(description="A completed upload's upload_id.")
    kind: Literal["adapter", "model"] | None = None
    label: str = Field(default="", max_length=120)


class ImportResult(BaseModel):
    status: Literal["imported", "already_present"]
    resource: dict[str, Any]


@router.get("/resources")
async def list_resources(
    kind: Annotated[Literal["model", "adapter", "component"] | None, Query()] = None,
    family: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """Every known resource with its state, verification, licence and installs.

    Installs show a managed root and a relative path, never where the app is
    installed.
    """
    rows = [catalogue.view(r) for r in store.listing(kind)]
    return [r for r in rows if (family is None or r["family"] == family)
            and (state is None or r["state"] == state)]


@router.post("/resources/rescan")
async def rescan() -> dict[str, int]:
    """Re-register configured models and bring the LoRA library up to date."""
    return await asyncio.to_thread(catalogue.refresh, budget_bytes=None)


@router.post("/resources/uploads", response_model=UploadSession,
             responses=error_responses(400))
async def create_upload(body: UploadCreate) -> dict[str, Any]:
    """Start a resumable upload. The format is checked here, before any bytes."""
    return intake.create_upload(body.filename, body.size, body.kind, body.sha256)


@router.put("/resources/uploads/{upload_id}", response_model=UploadSession,
            responses=error_responses(400, 404, 409))
async def upload_chunk(upload_id: str, request: Request,
                       offset: Annotated[int, Query(ge=0)]) -> dict[str, Any]:
    """Append the request body at `offset`. A wrong offset is a 409 that says
    where to resume."""
    length = int(request.headers.get("content-length") or 0)
    if length > intake.CHUNK_BYTES:
        raise RequestError(400, f"a chunk is at most {intake.CHUNK_BYTES} bytes",
                           code="too_large", field="body")
    data = await request.body()
    return await asyncio.to_thread(intake.append_chunk, upload_id, offset, data)


@router.post("/resources/uploads/{upload_id}/complete", response_model=UploadSession,
             responses=error_responses(400, 404, 409))
async def complete_upload(upload_id: str) -> dict[str, Any]:
    """Check the size and hash; the session id is then a receipt."""
    return await asyncio.to_thread(intake.complete_upload, upload_id)


@router.delete("/resources/uploads/{upload_id}", responses=error_responses(404))
async def cancel_upload(upload_id: str) -> dict[str, bool]:
    if not intake.cancel_upload(upload_id):
        raise RequestError(404, "no such upload", code="upload_not_found", field="upload_id")
    return {"ok": True}


@router.post("/resources/import", response_model=ImportResult,
             responses=error_responses(400, 404, 409))
async def import_resource(body: ImportBody) -> dict[str, Any]:
    """Install a completed upload: validate, deduplicate, promote atomically."""
    return await asyncio.to_thread(intake.import_receipt, body.receipt, kind=body.kind,
                                   label=body.label)


@router.post("/resources/{resource_id:path}/prepare", responses=error_responses(400, 404, 409, 503))
async def prepare_remote(resource_id: str) -> dict[str, Any]:
    """Copy an imported adapter to the connected Remote GPU worker, by content.

    Runs in the background; GET this path for progress, DELETE to cancel. A
    prepared adapter is stored on the worker; applying it in jobs is a separate
    worker capability.
    """
    from ..resources import remote

    return await asyncio.to_thread(remote.start, resource_id)


@router.get("/resources/{resource_id:path}/prepare", responses=error_responses(404))
async def prepare_status(resource_id: str) -> dict[str, Any]:
    from ..resources import remote

    state = remote.status(resource_id)
    if state is None:
        raise RequestError(404, "no preparation has run for this resource",
                           code="not_found", field="resource_id")
    return state


@router.delete("/resources/{resource_id:path}/prepare", responses=error_responses(404))
async def cancel_prepare(resource_id: str) -> dict[str, bool]:
    from ..resources import remote

    if not remote.cancel(resource_id):
        raise RequestError(404, "no preparation is running for this resource",
                           code="not_found", field="resource_id")
    return {"ok": True}


@router.get("/resources/{resource_id:path}", responses=error_responses(404))
async def read_resource(resource_id: str) -> dict[str, Any]:
    row = store.get(resource_id)
    if row is None:
        raise RequestError(404, f"resource '{resource_id}' is not known here",
                           code="unknown_resource", field="resource_id",
                           suggestions=["List resources with GET /api/resources."])
    return catalogue.view(row)
