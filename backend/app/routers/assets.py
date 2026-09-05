"""Gallery: list / search / organize / fetch / delete / export generated assets."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .. import db, scoring
from ..config import settings
from ..models import AssetRead
from ..version import get_version

router = APIRouter(tags=["assets"])


@router.post("/metadata/read")
async def read_metadata(image: UploadFile) -> dict:
    """Inspect an image for generation settings without saving/adopting it."""
    from ..metadata import read_image_metadata
    from .common import MAX_UPLOAD_BYTES, validate_upload_dimensions

    data = await image.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="empty image")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"image too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)",
        )
    try:
        validate_upload_dimensions(data)
        return await asyncio.to_thread(read_image_metadata, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/assets")
async def list_assets(
    limit: int = 120, offset: int = 0, kind: str | None = None, generator: str | None = None,
    favorite: bool | None = None, min_rating: int = 0, q: str | None = None,
    since_days: int | None = None, sort: str = "newest", collection_id: int | None = None,
    semantic: bool = False,
) -> dict:
    """The gallery query.

    `semantic=true` ranks by meaning rather than by whether the caption happened
    to contain the search word. It falls back to the ordinary text search when
    embeddings are unavailable — a search that returns keyword matches is far
    better than one that returns an error.
    """
    if semantic and q:
        ranked = await asyncio.to_thread(_semantic_ids, q, kind)
        if ranked is not None:
            page = ranked[offset:offset + limit]
            rows = db.get_assets(page)
            by_id = {a.id: a for a in rows}
            ordered = [by_id[i] for i in page if i in by_id]
            counts = db.duplicate_counts([a.id for a in ordered if a.id])
            return {"items": [AssetRead.of(a, counts.get(a.id or -1, 1)) for a in ordered],
                    "total": len(ranked), "limit": limit, "offset": offset,
                    "ranked_by": "meaning"}
    rows, total = db.search_assets(
        kind=kind, generator=generator, favorite=favorite, min_rating=min_rating, q=q,
        since_days=since_days, sort=sort, limit=limit, offset=offset,
        collection_id=collection_id,
    )
    counts = db.duplicate_counts([a.id for a in rows if a.id])
    return {"items": [AssetRead.of(a, counts.get(a.id or -1, 1)) for a in rows],
            "total": total, "limit": limit, "offset": offset}


def _semantic_ids(q: str, kind: str | None) -> list[int] | None:
    """Asset ids ranked by similarity to `q`, or None if that is not possible.

    None rather than an empty list: "we cannot do this" and "nothing matched"
    call for different behaviour, and only the first should fall back to the
    keyword search.
    """
    from .. import enrichment

    vector = enrichment.embed_text(q)
    if vector is None:
        return None
    hits = db.search_by_vector(vector, limit=500, kind=kind)
    return [aid for aid, _score in hits] if hits else None


@router.post("/assets/{asset_id}/used")
async def mark_used(asset_id: int) -> dict:
    """Record that this asset was actually used, not merely viewed.

    Called when an image is exported, downloaded, remixed, or sent to a tool.
    Favouriting is intent; this is behaviour, and the two disagree often enough
    that keeping them apart is what makes "show me what actually worked"
    answerable.
    """
    if db.get_asset(asset_id) is None:
        raise HTTPException(status_code=404, detail="asset not found")
    db.mark_used(asset_id)
    a = db.get_asset(asset_id)
    return {"ok": True, "used_count": int(a.used_count or 0) if a else 0}


# ---- trash ----------------------------------------------------------------
# Deletes are reversible now. bulk-delete and DELETE /assets/{id} move rows to
# the trash; only /assets/trash/purge touches the filesystem.
@router.get("/assets/trash")
async def list_trash(limit: int = 120, offset: int = 0) -> dict:
    rows, total = db.search_assets(limit=min(limit, 500), offset=offset,
                                   sort="newest", deleted=True)
    return {"items": [AssetRead.of(a) for a in rows], "total": total,
            "limit": limit, "offset": offset}


@router.post("/assets/restore")
async def restore(req: IdsReq) -> dict:
    return {"ok": True, "restored": db.restore_assets(req.ids)}


@router.post("/assets/trash/purge")
async def purge(req: IdsReq) -> dict:
    """Permanently delete. Empty `ids` empties the whole trash."""
    return {"ok": True, "purged": db.purge_deleted(req.ids or None)}


@router.get("/assets/{asset_id}")
async def get_asset(asset_id: int) -> AssetRead:
    a = db.get_asset(asset_id)
    if not a:
        raise HTTPException(status_code=404, detail="asset not found")
    return AssetRead.of(a)


def _first_frame_png(p: Path) -> bytes:
    from ..utils.io import first_frame

    buf = io.BytesIO()
    first_frame(p).save(buf, "PNG")
    return buf.getvalue()


@router.get("/assets/{asset_id}/frame")
async def get_frame(asset_id: int, which: str = "last") -> Response:
    """A single frame of a video asset as PNG (used by 'Extend' to prefill i2v)."""
    import asyncio

    from ..utils.io import last_frame_png

    a = db.get_asset(asset_id)
    if not a or a.kind != "video":
        raise HTTPException(status_code=404, detail="video asset not found")
    p = Path(a.path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="video file missing")
    try:
        # ffmpeg decode of a long clip takes seconds — never block the event loop
        # (it also carries the jobs WebSocket).
        if which == "first":
            data = await asyncio.to_thread(_first_frame_png, p)
        else:
            data = await asyncio.to_thread(last_frame_png, p)
    except Exception:  # noqa: BLE001 — corrupt/zero-frame file
        raise HTTPException(status_code=422, detail="could not decode a frame from this video") from None
    return Response(content=data, media_type="image/png")


class FavReq(BaseModel):
    favorite: bool


class RatingReq(BaseModel):
    rating: int


class HumanGradeReq(BaseModel):
    """The two answers required to make a review comparable."""
    prompt_fidelity: int = Field(ge=1, le=5)
    visual_quality: int = Field(ge=1, le=5)
    notes: str = Field(default="", max_length=800)


class TagsReq(BaseModel):
    tags: list[str]


class IdsReq(BaseModel):
    ids: list[int]


@router.post("/assets/{asset_id}/favorite")
async def set_favorite(asset_id: int, req: FavReq) -> dict:
    a = db.update_asset(asset_id, favorite=req.favorite)
    return {"ok": bool(a)}


@router.get("/scores")
async def scores() -> dict:
    """How each model and adapter is doing, from the ratings given so far.

    Derived on read rather than kept as a running total: ratings change, images
    get trashed, and a stored counter would drift out of agreement with the
    library it claims to describe. The scan is over one indexed column on a
    single-user database.
    """
    return scoring.leaderboards()


@router.post("/assets/{asset_id}/rating")
async def set_rating(asset_id: int, req: RatingReq) -> dict:
    a = db.update_asset(asset_id, rating=max(0, min(req.rating, 5)))
    return {"ok": bool(a)}


@router.post("/assets/{asset_id}/grade")
async def set_human_grade(asset_id: int, req: HumanGradeReq) -> dict:
    """Save a rubric review and its rounded quick-star equivalent atomically."""
    grade, rating = scoring.build_human_grade(req.model_dump())
    a = db.set_asset_grade(asset_id, grade, rating)
    return {"ok": bool(a), "rating": rating, "grade": grade}


@router.post("/assets/{asset_id}/tags")
async def set_tags(asset_id: int, req: TagsReq) -> dict:
    clean = [t.strip() for t in req.tags if t.strip()][:20]
    a = db.update_asset(asset_id, tags=clean)
    return {"ok": bool(a), "tags": clean}


@router.post("/assets/bulk-delete")
async def bulk_delete(req: IdsReq) -> dict:
    return {"ok": True, "deleted": db.delete_assets(req.ids)}


@router.post("/assets/export")
async def export_zip(req: IdsReq) -> FileResponse:
    """Zip originals plus a durable library manifest.

    Media bytes alone lose ratings, rubric grades, tags, captions, job/model
    identity and video settings. The manifest makes an export a portable record,
    while individual video sidecars keep each clip self-describing on its own.
    Spooled to disk, not BytesIO — a selection of videos is easily multi-GB.
    """
    import asyncio
    import os
    import tempfile

    from starlette.background import BackgroundTask

    settings.ensure_dirs()
    include_generation = settings.effective_bool("embed_metadata")
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=settings.temp_path)
    os.close(fd)

    def _build() -> None:
        rows = db.get_assets(req.ids)  # one SELECT, not one session per id
        selected = {asset.id: asset for asset in rows}
        ordered = [selected[asset_id] for asset_id in req.ids if asset_id in selected]
        used_names: set[str] = set()
        manifest_assets: list[dict] = []
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for asset in ordered:
                source = Path(asset.path)
                if not source.exists():
                    continue
                archive_name = asset.filename
                if archive_name in used_names:
                    archive_name = f"asset-{asset.id}-{archive_name}"
                used_names.add(archive_name)
                zf.write(source, arcname=archive_name)
                sidecar = source.with_suffix(source.suffix + ".json")
                if include_generation and asset.kind == "video" and sidecar.exists():
                    zf.write(sidecar, arcname=archive_name + ".json")
                content = db.content_of(int(asset.id)) if asset.id is not None else None
                manifest_assets.append({
                    "asset_id": asset.id,
                    "archive_path": archive_name,
                    "kind": asset.kind,
                    "width": asset.width,
                    "height": asset.height,
                    "generator": asset.generator,
                    "job_id": asset.job_id,
                    "created_at": asset.created_at.isoformat(),
                    "content": ({"hash": content.hash, "size_bytes": content.size_bytes,
                                 "mime_type": content.mime_type} if content else None),
                    "generation": asset.meta if include_generation else None,
                    "library": {
                        "favorite": bool(asset.favorite),
                        "rating": int(asset.rating or 0),
                        "grade": asset.grade,
                        "tags": asset.tags,
                        "caption": asset.caption or "",
                        "used_count": int(asset.used_count or 0),
                    },
                })
            manifest = {
                "schema": "wizards-brush-export/v1",
                "app": {"name": "The Wizard's Brush", "version": get_version()},
                "exported_at": datetime.now(UTC).isoformat(),
                "assets": manifest_assets,
            }
            zf.writestr("wizards-brush-manifest.json", json.dumps(
                manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    try:
        await asyncio.to_thread(_build)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return FileResponse(tmp, media_type="application/zip", filename="gen-export.zip",
                        background=BackgroundTask(lambda: Path(tmp).unlink(missing_ok=True)))


@router.delete("/assets/{asset_id}")
async def delete_asset(asset_id: int) -> dict:
    return {"ok": db.delete_asset(asset_id)}
