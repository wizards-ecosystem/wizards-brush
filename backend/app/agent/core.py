"""Async façade over the job API, the asset importer and the gallery search.

Nothing in this package generates pixels. MCP (and, later, any other agent
adapter) calls these functions so a job it submits is the same job the form
and `POST /api/jobs` submit.
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any, Literal

from fastapi import HTTPException

from ..models import AssetRead, JobRead
from ..problems import RequestError
from ..routers import assets, job_api
from ..routers.job_api import DryRunResult, JobCreate, JobSubmission, JobWait

_DATA_PREFIX = "data:image/"


async def kind_rows(*, category: str | None = None, available_only: bool = True) -> list[dict[str, Any]]:
    """Short catalogue rows. Full schemas live on `describe`."""
    rows = []
    for info in await job_api.job_kinds():
        if category is not None and info.category != category:
            continue
        if available_only and not info.available:
            continue
        rows.append({
            "kind": info.kind,
            "title": info.title,
            "category": info.category,
            "lane": info.lane,
            "output": info.output,
            "available": info.available,
            "unavailable_reason": info.unavailable_reason,
        })
    return rows


async def describe(kind: str) -> dict[str, Any]:
    """One kind, including the JSON Schema `POST /api/jobs` enforces."""
    for info in await job_api.job_kinds():
        if info.kind == kind:
            return info.model_dump(mode="json")
    known = sorted(row.kind for row in await job_api.job_kinds())
    from ..problems import close_suggestions

    raise RequestError(
        404, f"unknown kind '{kind}'",
        code="kind_unavailable", field="kind", valid_options=known,
        suggestions=close_suggestions(kind, known) or ["Call list_kinds to see what this install can run."],
    )


async def submit(body: JobCreate) -> JobSubmission | DryRunResult:
    return await job_api.create_job(body)


async def wait(job_id: int, timeout: float) -> JobWait:
    return await job_api.wait_job(job_id, timeout)


async def job(job_id: int) -> JobRead:
    from .. import db

    row = db.get_job(job_id)
    if row is None:
        raise RequestError(
            404, "job not found", code="not_found", field="job_id",
            suggestions=["Check the job id, or submit the job again."],
        )
    return JobRead.of(row)


async def import_image(
    data: bytes,
    *,
    role: Literal["source", "mask"] = "source",
    filename: str = "",
) -> AssetRead:
    from .. import db

    asset_id = await asyncio.to_thread(assets.import_bytes, data, role=role, filename=filename)
    stored = db.get_asset(asset_id)
    assert stored is not None
    return AssetRead.of(stored)


async def search(
    q: str | None = None,
    *,
    kind: str | None = None,
    favorite: bool | None = None,
    semantic: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    page = await assets.list_assets(
        limit=min(max(limit, 1), 50), offset=0, kind=kind, favorite=favorite,
        q=q, semantic=semantic,
    )
    page["items"] = [public_asset(item) for item in page["items"]]
    return page


def public_job(row: JobRead) -> dict[str, Any]:
    return row.model_dump(mode="json")


def public_asset(asset: AssetRead) -> dict[str, Any]:
    data = asset.model_dump(mode="json")
    # `/files/...` is cookie-only once a token is set. Agents hold a header.
    data["file_url"] = f"/api/assets/{asset.id}/file"
    return data


def decode_data_url(value: str) -> bytes:
    """Bytes from an image data URL. http(s) URLs are refused so this cannot fetch."""
    if value.startswith(("http://", "https://", "//")):
        raise RequestError(
            400, "images must be asset ids or data URLs; http URLs are not accepted",
            code="invalid_request", field="images",
            suggestions=["Import the file first, or pass a data:image/...;base64 URL."],
        )
    if not value.startswith(_DATA_PREFIX) or ";base64," not in value:
        raise RequestError(
            400, "image data URLs must look like data:image/png;base64,...",
            code="invalid_request", field="images",
            suggestions=["Pass a base64 data:image URL, or an integer asset id."],
        )
    payload = value.split(",", 1)[1]
    try:
        data = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise RequestError(
            400, "data URL is not valid base64",
            code="invalid_request", field="images",
            suggestions=["Base64-encode the image bytes after the comma."],
        ) from exc
    if not data:
        raise RequestError(400, "empty upload", code="invalid_request", field="images")
    return data


async def image_id(value: int | str, *, role: Literal["source", "mask"] = "source") -> int:
    """An asset id, or a data URL which is imported first."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RequestError(
            400, "an image must be an asset id or a data URL",
            code="invalid_request", field="images" if role == "source" else "mask",
        )
    if isinstance(value, int) or value.isdigit():
        return int(value)
    data = decode_data_url(value)
    imported = await import_image(data, role=role, filename="agent.png")
    return imported.id


def raise_http(exc: HTTPException) -> None:
    """Re-raise as a structured job error. Exists so adapters share one conversion."""
    from ..problems import as_request_error

    raise as_request_error(exc) from None
