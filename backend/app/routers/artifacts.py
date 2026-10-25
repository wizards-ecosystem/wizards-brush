"""Artifacts over HTTP: read, download and delete typed export outputs.

Downloads are ordinary authenticated ``/api`` requests. Views carry the file's
MIME type, size and SHA-256, never where it is stored.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse

from .. import artifacts
from ..problems import RequestError
from .common import error_responses

router = APIRouter(tags=["artifacts"])


@router.get("/artifacts/{artifact_id}", responses=error_responses(404))
async def read_artifact(artifact_id: int) -> dict[str, Any]:
    return artifacts.view(await asyncio.to_thread(artifacts.get, artifact_id))


@router.get("/artifacts/{artifact_id}/file", responses=error_responses(404))
async def download_artifact(artifact_id: int) -> FileResponse:
    row = await asyncio.to_thread(artifacts.get, artifact_id)
    return FileResponse(row.path, media_type=row.mime_type, filename=row.filename,
                        headers={"X-Content-SHA256": row.sha256})


@router.delete("/artifacts/{artifact_id}", responses=error_responses(404))
async def delete_artifact(artifact_id: int) -> dict[str, bool]:
    if not await asyncio.to_thread(artifacts.delete, artifact_id):
        raise RequestError(404, f"artifact {artifact_id} does not exist", code="not_found",
                           field="artifact_id")
    return {"ok": True}
