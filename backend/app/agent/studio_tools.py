"""Agent façade for presets, plans, targets, resources, masks and cancellation.

Each helper calls the HTTP route function (or the service it wraps), so MCP and
HTTP cannot disagree about a preset, a plan or a hash. Outputs go through
jsonable_encoder (timestamps become strings); nothing here adds a path the
route did not already redact.
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from ..models import AssetRead
from ..problems import RequestError
from ..routers import assets, job_api
from . import core

_TERMINAL = frozenset({"done", "error", "canceled"})
# Full-resolution bytes returned through MCP for clients that cannot fetch an
# authenticated URL. Larger files are refused with the file_url to use instead.
FETCH_MAX_BYTES = 16 * 1024 * 1024
MASK_MAX_BYTES = 16 * 1024 * 1024


def _json(value: Any) -> Any:
    return jsonable_encoder(value)


# ---- discovery -------------------------------------------------------------------------------
async def targets() -> dict[str, Any]:
    from ..routers import targets as targets_router

    return _json(await targets_router.read_targets())


async def processors() -> dict[str, Any]:
    from ..routers import variant_sets

    caps = await variant_sets.capabilities()
    return _json({"finishing": caps["finishing"], "validators": caps["validators"]})


async def resources(*, kind: Any = None, family: str | None = None,
                    state: str | None = None) -> list[dict[str, Any]]:
    from ..routers import resources as resources_router

    return _json(await resources_router.list_resources(kind=kind, family=family, state=state))


async def resource(resource_id: str) -> dict[str, Any]:
    from ..routers import resources as resources_router

    return _json(await resources_router.read_resource(resource_id))


async def rescan_resources() -> dict[str, int]:
    from ..routers import resources as resources_router

    return _json(await resources_router.rescan())


async def prepare_remote(resource_id: str) -> dict[str, Any]:
    from ..routers import resources as resources_router

    return _json(await resources_router.prepare_remote(resource_id))


async def prepare_status(resource_id: str) -> dict[str, Any]:
    from ..routers import resources as resources_router

    return _json(await resources_router.prepare_status(resource_id))


# ---- presets ---------------------------------------------------------------------------------
async def presets(**filters: Any) -> list[dict[str, Any]]:
    from ..routers import studio

    return _json(await studio.list_presets(**filters))


async def preset(preset_id: str, revision: int | None = None) -> dict[str, Any]:
    from ..routers import studio

    return _json(await studio.read_preset(preset_id, revision))


async def preset_history(preset_id: str) -> list[dict[str, Any]]:
    from ..routers import studio

    return _json(await studio.preset_revisions(preset_id))


async def save_preset(definition: dict[str, Any]) -> dict[str, Any]:
    from ..routers import studio

    return _json(await studio.create_preset(definition))


async def update_preset(preset_id: str, definition: dict[str, Any], base_revision: int,
                        on_conflict: Literal["error", "copy"]) -> dict[str, Any]:
    from ..routers import studio

    return _json(await studio.update_preset(preset_id, studio.PresetUpdate(
        definition=definition, base_revision=base_revision, on_conflict=on_conflict)))


async def clone_preset(preset_id: str, *, revision: int | None, name: str | None,
                       new_id: str | None) -> dict[str, Any]:
    from ..routers import studio

    return _json(await studio.clone_preset(
        preset_id, studio.PresetClone(revision=revision, name=name, id=new_id)))


# ---- variant sets ----------------------------------------------------------------------------
async def preview_variant_set(recipe: dict[str, Any] | None, recipe_id: int | None,
                              sources: list[int] | None, limit: int) -> dict[str, Any]:
    from ..routers import variant_sets as routes

    return _json(await routes.preview_set(routes.PreviewBody.model_validate(
        {"recipe": recipe, "recipe_id": recipe_id, "sources": sources, "limit": limit})))


async def create_variant_set(recipe: dict[str, Any] | None, recipe_id: int | None,
                             sources: list[int] | None, name: str,
                             request_id: str | None) -> dict[str, Any]:
    from ..routers import variant_sets as routes

    return _json(await routes.create_set(routes.CreateSetBody.model_validate(
        {"recipe": recipe, "recipe_id": recipe_id, "sources": sources, "name": name,
         "request_id": request_id})))


async def wait_variant_set(set_id: int, timeout: float, items: bool) -> dict[str, Any]:
    from ..routers import variant_sets as routes

    return _json(await routes.wait_set(set_id, timeout=timeout, items=items))


async def cancel_variant_set(set_id: int) -> dict[str, Any]:
    from ..routers import variant_sets as routes

    return _json(await routes.cancel(set_id))


async def retry_variant_set(set_id: int, include_canceled: bool) -> dict[str, Any]:
    from ..routers import variant_sets as routes

    return _json(await routes.retry(set_id, routes.RetryBody(include_canceled=include_canceled)))


async def rerun_variant_item(set_id: int, item_id: int, reseed: bool,
                             cascade: bool) -> dict[str, Any]:
    from ..routers import variant_sets as routes

    return _json(await routes.rerun_item(set_id, item_id,
                                         routes.RerunBody(reseed=reseed, cascade=cascade)))


async def preview_sprites(set_id: int, profile: dict[str, Any]) -> dict[str, Any]:
    from ..routers import variant_sets as routes

    return _json(await routes.preview_sprites(set_id, routes.SpriteProfile.model_validate(profile)))


async def export_sprites(set_id: int, profile: dict[str, Any]) -> dict[str, Any]:
    from ..routers import variant_sets as routes

    return _json(await routes.export_sprites(set_id, routes.SpriteProfile.model_validate(profile)))


# ---- artifacts ---------------------------------------------------------------------------------
ARTIFACT_MAX_BYTES = 16 * 1024 * 1024


async def artifact(artifact_id: int) -> dict[str, Any]:
    from ..routers import artifacts as routes

    return _json(await routes.read_artifact(artifact_id))


def artifact_bytes(artifact_id: int) -> tuple[bytes, dict[str, Any]]:
    """An artifact's bytes for clients that cannot fetch file_url, within a cap."""
    from pathlib import Path

    from .. import artifacts

    row = artifacts.get(artifact_id)
    if row.size_bytes > ARTIFACT_MAX_BYTES:
        raise RequestError(413, f"artifact {artifact_id} is {row.size_bytes} bytes; fetch "
                                f"{artifacts.view(row)['file_url']} with the API token instead",
                           code="too_large", field="artifact_id")
    return Path(row.path).read_bytes(), _json(artifacts.view(row))


# ---- reference sets --------------------------------------------------------------------------
async def reference_sets(include_archived: bool = False) -> list[dict[str, Any]]:
    from ..routers import reference_sets as routes

    return _json(await routes.list_sets(include_archived))


async def reference_set(set_id: int, revision: int | None = None) -> dict[str, Any]:
    from ..routers import reference_sets as routes

    return _json(await routes.read_set(set_id, revision))


async def save_reference_set(name: str, definition: dict[str, Any]) -> dict[str, Any]:
    from ..routers import reference_sets as routes

    return _json(await routes.create_set(routes.ReferenceSetCreate(name=name,
                                                                   definition=definition)))


async def update_reference_set(set_id: int, definition: dict[str, Any], base_revision: int,
                               name: str | None) -> dict[str, Any]:
    from ..routers import reference_sets as routes

    return _json(await routes.update_set(set_id, routes.ReferenceSetUpdate(
        definition=definition, base_revision=base_revision, name=name)))


async def archive_reference_set(set_id: int, archived: bool) -> dict[str, Any]:
    from ..routers import reference_sets as routes

    if archived:
        return _json(await routes.archive_set(set_id))
    return _json(await routes.unarchive_set(set_id))


# ---- canvas ----------------------------------------------------------------------------------
async def canvases(include_archived: bool = False) -> list[dict[str, Any]]:
    from ..routers import canvas as routes

    return _json(await routes.list_projects(include_archived))


async def canvas(project_id: int, revision: int | None = None) -> dict[str, Any]:
    from ..routers import canvas as routes

    return _json(await routes.read_project(project_id, revision))


async def create_canvas(name: str, document: dict[str, Any]) -> dict[str, Any]:
    from ..routers import canvas as routes

    return _json(await routes.create_project(routes.CanvasCreate(name=name, document=document)))


async def save_canvas(project_id: int, document: dict[str, Any], base_revision: int,
                      name: str | None) -> dict[str, Any]:
    from ..routers import canvas as routes

    return _json(await routes.save_project(project_id, routes.CanvasSave(
        document=document, base_revision=base_revision, name=name)))


async def flatten_canvas(project_id: int, revision: int | None,
                         allow_missing: bool) -> dict[str, Any]:
    from ..routers import canvas as routes

    return _json(await routes.flatten_project(project_id, routes.FlattenBody(
        revision=revision, allow_missing=allow_missing)))


async def add_canvas_layer(project_id: int, *, base_revision: int, asset_id: int | None,
                           job_id: int | None, name: str, from_layer: str | None,
                           mask_id: int | None) -> dict[str, Any]:
    from ..routers import canvas as routes

    return _json(await routes.add_layer(project_id, routes.LayerAdd(
        base_revision=base_revision, asset_id=asset_id, job_id=job_id, name=name,
        from_layer=from_layer, mask_id=mask_id)))


async def archive_preset(preset_id: str, archived: bool) -> dict[str, Any]:
    from ..routers import studio

    if archived:
        return _json(await studio.archive_preset(preset_id))
    return _json(await studio.unarchive_preset(preset_id))


# ---- plans -----------------------------------------------------------------------------------
async def plan(plan_id: int) -> dict[str, Any]:
    return _json((await job_api.read_plan(plan_id)).model_dump())


async def replay(job_id: int, *, mode: Literal["exact", "latest"], dry_run: bool,
                 request_id: str | None) -> Any:
    return await job_api.replay(job_id, job_api.ReplayBody(
        mode=mode, dry_run=dry_run, request_id=request_id))


# ---- cancellation ----------------------------------------------------------------------------
async def cancel(job_id: int, *, wait_seconds: float = 0.0) -> dict[str, Any]:
    """Cancel through the queue the jobs route uses, then say what really happened.

    Queued work is removed and settles at once. A running job is only asked to
    stop: the lane checks at its next progress step, and a Remote GPU job's own
    polling loop sends the worker its cancel. Until the job row settles that is
    `cancellation_requested`, never "canceled".
    """
    from .. import db, queue

    row = db.get_job(job_id)
    if row is None:
        raise RequestError(404, "job not found", code="not_found", field="job_id",
                           suggestions=["Check the job id."])
    before = row.status
    if before in _TERMINAL:
        return {"job_id": job_id, "state": "already_settled", "status": before,
                "changed": False, "settled": True}
    lane = queue.lane_for_job(row)
    changed = queue.cancel(job_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, min(float(wait_seconds), 60.0))
    current = db.get_job(job_id)
    while (current is not None and current.status not in _TERMINAL
           and loop.time() < deadline):
        await asyncio.sleep(0.1)
        current = db.get_job(job_id)
    status = current.status if current is not None else before
    settled = status in _TERMINAL
    if before == "queued" and settled:
        state = "removed_from_queue"
    elif settled:
        state = "canceled" if status == "canceled" else "settled_otherwise"
    else:
        state = "cancellation_requested"
    out: dict[str, Any] = {"job_id": job_id, "state": state, "status": status,
                           "changed": changed, "settled": settled, "lane": lane}
    if not settled:
        out["note"] = (
            "The worker is told to stop by the job's own polling loop; cancellation is "
            "unconfirmed until the job settles. Outputs already saved are kept."
            if lane == "remote" else
            "The lane stops the job at its next progress step; it is not canceled until it "
            "settles. Outputs already saved are kept.")
    return out


# ---- masks and bytes -------------------------------------------------------------------------
async def import_mask(data: bytes, *, source_asset_id: int | None = None) -> AssetRead:
    """Store a mask (white = change) as a gallery asset, through the same import
    the upload route uses, after checking it matches its source image."""
    from .. import db

    if len(data) > MASK_MAX_BYTES:
        raise RequestError(400, f"a mask is at most {MASK_MAX_BYTES >> 20} MB",
                           code="invalid_mask", field="mask")
    try:
        image = await asyncio.to_thread(assets.validate_image_bytes, data, "mask")
    except HTTPException as exc:
        raise RequestError(400, str(exc.detail), code="invalid_mask", field="mask") from None
    if source_asset_id is not None:
        source = db.get_asset(source_asset_id)
        if source is None or source.kind != "image":
            raise RequestError(404, f"source asset {source_asset_id} is not a gallery image",
                               code="not_found", field="source_asset_id")
        if (image.width, image.height) != (source.width, source.height):
            raise RequestError(
                400, f"the mask is {image.width}x{image.height} but the source is "
                     f"{source.width}x{source.height}; a mask must match its image",
                code="invalid_mask", field="mask",
                suggestions=["Make the mask at the source image's size."])
    return await core.import_image(data, role="mask", filename="mask.png")


def asset_bytes(asset_id: int) -> tuple[bytes, str, dict[str, Any]]:
    """(bytes, mime type, public view) of a stored image, bounded."""
    from pathlib import Path

    from .. import db

    row = db.get_asset(asset_id)
    if row is None:
        raise RequestError(404, "asset not found", code="not_found", field="asset_id")
    view = core.public_asset(AssetRead.of(row))
    path = Path(row.path)
    if row.kind != "image":
        raise RequestError(400, "only images are returned inline; fetch a video from file_url",
                           code="invalid_request", field="asset_id",
                           suggestions=[f"GET {view['file_url']} with the API token header."])
    if not path.is_file():
        raise RequestError(409, "the asset's file is missing", code="conflict", field="asset_id")
    size = path.stat().st_size
    if size > FETCH_MAX_BYTES:
        raise RequestError(
            413, f"the file is {size >> 20} MB; inline fetch is capped at "
                 f"{FETCH_MAX_BYTES >> 20} MB", code="too_large", field="asset_id",
            suggestions=[f"GET {view['file_url']} with the API token header."])
    return path.read_bytes(), str(view.get("mime_type") or "image/png"), view
