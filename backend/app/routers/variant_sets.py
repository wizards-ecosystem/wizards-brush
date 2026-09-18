"""Variant Sets and recipes over HTTP.

Recipes are saved definitions (configuration only); sets are executions. Every
body is a closed schema (unknown keys are refused, sizes are bounded) and every
recipe is compiled against the live registry before anything is stored, so a
malformed or out-of-policy request is a 400/409 here rather than a failing job
later — and nothing a set persists could not have been sent by an ordinary
generate request.
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import db, finishing, validators
from ..config import settings
from ..models import AssetKind
from ..queue import lane_for, on_job_terminal
from ..redaction import redact
from ..variant_sets import expansion, operations, service, store
from ..variant_sets.recipe import RecipeError, RecipeSpec, compile_recipe
from ..variant_sets.schemas import ItemView, RecipeView, SetDetail, SetView, SetWait
from .common import MAX_UPLOAD_BYTES, error_responses, load_uploaded_image, normalize_request_id

router = APIRouter(tags=["variant-sets"])

# Children finishing is how a set advances to its next stage.
on_job_terminal(service.LISTENER, service.on_job_terminal)


def _http(error: service.VariantSetError) -> HTTPException:
    return HTTPException(status_code=error.status, detail=error.detail)


class RecipeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=service.MAX_NAME_CHARS)
    description: str = Field(default="", max_length=2000)
    recipe: RecipeSpec


class CollectionChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "new", "existing"] = "none"
    id: int | None = None


class CreateSetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=service.MAX_NAME_CHARS)
    recipe: RecipeSpec | None = None
    recipe_id: int | None = None
    # Replaces the recipe's sources, so one saved recipe can run on new material.
    sources: list[int] | None = Field(default=None, max_length=3)
    request_id: str | None = None
    collection: CollectionChoice = Field(default_factory=CollectionChoice)


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe: RecipeSpec | None = None
    recipe_id: int | None = None
    sources: list[int] | None = Field(default=None, max_length=3)
    limit: int = Field(default=100, ge=0, le=expansion.HARD_MAX_COMBINATIONS)


class RetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_canceled: bool = False


class RetryResult(BaseModel):
    retried: int = Field(description="Items reset for another attempt.")
    submitted: int = Field(description="Jobs queued now; later-stage items wait for parents.")


class CancelResult(BaseModel):
    canceled: int = Field(description="Variants stopped: waiting ones canceled, running ones told to stop.")


class OkResult(BaseModel):
    ok: bool = True


class RerunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reseed: bool = False
    cascade: bool = False


def _recipe_from(body_recipe: RecipeSpec | None, recipe_id: int | None,
                 sources: list[int] | None) -> tuple[RecipeSpec, int | None]:
    if (body_recipe is None) == (recipe_id is None):
        raise HTTPException(status_code=400, detail="give exactly one of `recipe` or `recipe_id`")
    if body_recipe is None:
        row = store.get_recipe(int(recipe_id or 0))
        if row is None:
            raise HTTPException(status_code=404, detail="recipe not found")
        try:
            spec = RecipeSpec.model_validate(row.config)
        except ValidationError as error:
            raise HTTPException(status_code=409,
                                detail=f"the saved recipe no longer validates: {error}") from None
    else:
        spec = body_recipe
    if sources is not None:
        spec = spec.model_copy(update={"sources": list(sources)})
    return spec, recipe_id


async def _remote_blocker(spec: RecipeSpec) -> str | None:
    """Why this recipe's Remote GPU stages cannot run now, or None."""
    from .job_api import remote_blocker, remote_state

    remote = [i + 1 for i, stage in enumerate(spec.stages) if lane_for(stage.operation) == "remote"]
    if not remote:
        return None
    blocker = remote_blocker(await remote_state())
    if blocker is None:
        return None
    which = f"stage {remote[0]}" if len(remote) == 1 else "stages " + ", ".join(map(str, remote))
    return f"{blocker} ({which} of this set run there.)"


def _local_requirements(spec: RecipeSpec) -> None:
    """The same refusals the generate routes make, made before anything is queued."""
    from .images import require_local_gpu, require_model_access

    local = [stage for stage in spec.stages if lane_for(stage.operation) == "local"]
    if not local:
        return
    require_local_gpu()
    variants = {stage.params.get("model_variant") for stage in local}
    variants |= {overrides.get("model_variant") for stage in local
                 for per_value in stage.value_params.values() for overrides in per_value.values()}
    for variant in variants:
        require_model_access({"model_variant": variant})


# ---- capabilities --------------------------------------------------------------------------
@router.get("/variant-sets/capabilities")
async def capabilities() -> dict[str, Any]:
    """What a recipe may use here and now: operations the live registry offers
    (with their input requirements), finishing processors, validation checks,
    and the combination cap."""
    specs = operations.registry_specs()
    return {
        "operations": [{
            "kind": op.kind, "title": specs[op.kind].get("title", op.kind),
            "min_sources": op.min_sources, "max_sources": op.max_sources, "mask": op.mask,
            "takes_source": op.takes_source, "note": op.note,
            "lane": lane_for(op.kind),
        } for op in operations.available(specs)],
        "set_controlled": sorted(operations.SET_CONTROLLED),
        "finishing": await asyncio.to_thread(finishing.describe),
        "validators": validators.registered(),
        "cap": expansion.configured_cap(),
        "limits": {"stages": expansion.MAX_STAGES, "axes_per_stage": expansion.MAX_AXES_PER_STAGE,
                   "values_per_axis": expansion.MAX_VALUES_PER_AXIS,
                   "value_chars": expansion.MAX_VALUE_CHARS},
    }


# ---- recipes ----------------------------------------------------------------------------------
def _recipe_view(row: Any) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "description": row.description,
            "recipe": row.config, "created_at": row.created_at, "updated_at": row.updated_at}


def _validate_definition(spec: RecipeSpec) -> dict[str, Any]:
    """A recipe is checked on save like a set, except that its sources may be
    left for the set to supply."""
    try:
        probe = spec if spec.sources else _with_placeholder_sources(spec)
        compiled = compile_recipe(probe, specs=operations.registry_specs())
    except RecipeError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    snapshot = compiled.snapshot()
    snapshot["sources"] = list(spec.sources)
    return snapshot


def _with_placeholder_sources(spec: RecipeSpec) -> RecipeSpec:
    op = operations.OPERATIONS.get(spec.stages[0].operation)
    need = op.min_sources if op is not None else 0
    return spec.model_copy(update={"sources": [0] * need})


@router.get("/variant-recipes", response_model=list[RecipeView])
async def list_recipes() -> list[dict[str, Any]]:
    return [_recipe_view(r) for r in store.list_recipes()]


@router.post("/variant-recipes", response_model=RecipeView, responses=error_responses(400))
async def create_recipe(body: RecipeBody) -> dict[str, Any]:
    row = store.save_recipe(body.name.strip(), body.description, _validate_definition(body.recipe))
    return _recipe_view(row)


@router.get("/variant-recipes/{recipe_id}", response_model=RecipeView,
            responses=error_responses(404))
async def get_recipe(recipe_id: int) -> dict[str, Any]:
    row = store.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    return _recipe_view(row)


@router.put("/variant-recipes/{recipe_id}", response_model=RecipeView,
            responses=error_responses(400, 404))
async def update_recipe(recipe_id: int, body: RecipeBody) -> dict[str, Any]:
    """Edit a definition. Sets made from it keep their own snapshot, so their
    meaning never changes."""
    row = store.save_recipe(body.name.strip(), body.description,
                            _validate_definition(body.recipe), recipe_id=recipe_id)
    if row is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    return _recipe_view(row)


@router.post("/variant-recipes/{recipe_id}/clone", response_model=RecipeView,
             responses=error_responses(404))
async def clone_recipe(recipe_id: int) -> dict[str, Any]:
    row = store.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(status_code=404, detail="recipe not found")
    name = f"{row.name} (copy)"[:service.MAX_NAME_CHARS]
    return _recipe_view(store.save_recipe(name, row.description, row.config))


@router.delete("/variant-recipes/{recipe_id}", response_model=OkResult,
               responses=error_responses(404))
async def delete_recipe(recipe_id: int) -> dict[str, Any]:
    if not store.delete_recipe(recipe_id):
        raise HTTPException(status_code=404, detail="recipe not found")
    return {"ok": True}


# ---- sets ---------------------------------------------------------------------------------------
@router.post("/variant-sets/preview", responses=error_responses(400, 404, 409))
async def preview_set(body: PreviewBody) -> dict[str, Any]:
    """Every combination, its effective prompt and its output name — without
    creating anything. Naming collisions are reported, not raised, so the form
    can show them."""
    spec, _ = _recipe_from(body.recipe, body.recipe_id, body.sources)
    try:
        result = await asyncio.to_thread(service.preview, spec, limit=body.limit)
    except service.VariantSetError as error:
        raise _http(error) from None
    if blocker := await _remote_blocker(spec):
        result["warnings"] = [*result.get("warnings", []), blocker]
    return result


@router.post("/variant-sets", response_model=SetDetail, response_model_exclude_unset=True,
             responses=error_responses(400, 404, 409, 503))
async def create_set(body: CreateSetBody) -> dict[str, Any]:
    """Create a set and queue its first stage.

    Idempotent on `request_id`: resubmitting it returns the original set with
    `created: false` instead of queuing it again.
    """
    spec, recipe_id = _recipe_from(body.recipe, body.recipe_id, body.sources)
    request_id = normalize_request_id(body.request_id)
    if body.collection.mode == "existing" and body.collection.id is None:
        raise HTTPException(status_code=400, detail="choose the collection to add results to")
    _local_requirements(spec)
    # The editor refuses a set whose stages need an offline Remote GPU; so does
    # the API - except a resubmitted request_id, which always returns its set.
    resubmitted = bool(request_id and store.set_for_request(request_id))
    if not resubmitted and (blocker := await _remote_blocker(spec)):
        raise HTTPException(status_code=503, detail=blocker)
    try:
        made = await service.create_set(
            spec, name=body.name, recipe_id=recipe_id, request_id=request_id,
            collection_id=body.collection.id if body.collection.mode == "existing" else None,
            new_collection=body.collection.mode == "new",
        )
    except service.VariantSetError as error:
        raise _http(error) from None
    assert made.set.id is not None
    return {**service.set_view(made.set), "created": made.created}


@router.get("/variant-sets", response_model=list[SetView])
async def list_sets(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    return [service.set_view(r) for r in store.list_sets(limit=max(1, min(limit, 500)),
                                                         offset=max(0, offset))]


async def _detail(set_id: int, *, items: bool) -> dict[str, Any]:
    if store.get_set(set_id) is None:
        raise HTTPException(status_code=404, detail="variant set not found")
    await service.reconcile_set(set_id)
    row = store.get_set(set_id)
    assert row is not None
    out = {**service.set_view(row), "recipe": row.recipe}
    if items:
        out["items"] = service.item_views(store.items_for_set(set_id))
    return out


@router.get("/variant-sets/{set_id}", response_model=SetDetail,
            response_model_exclude_unset=True, responses=error_responses(404))
async def get_set(set_id: int, items: bool = True) -> dict[str, Any]:
    """The set, its frozen recipe snapshot and every item's live state.

    Reading also reconciles: anything a missed notification left behind is
    settled before it is shown.
    """
    return await _detail(set_id, items=items)


@router.get("/variant-sets/{set_id}/wait", response_model=SetWait,
            response_model_exclude_unset=True, responses=error_responses(404))
async def wait_set(
    set_id: int,
    timeout: float = Query(30.0, ge=0.0, le=60.0,
                           description="Seconds to wait for the set to settle."),
    items: bool = Query(False, description="Include every item in the answer."),
) -> dict[str, Any]:
    """Long-poll: return once the set is no longer active, or at `timeout`.

    A set settles as `complete`, `incomplete` (some variants failed, were
    invalid or blocked; `POST .../retry` runs them again) or `canceled`.
    `settled: false` means it is still running; wait again.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        row = store.get_set(set_id)
        if row is None:
            raise HTTPException(status_code=404, detail="variant set not found")
        if row.status != store.ACTIVE or loop.time() >= deadline:
            break
        await asyncio.sleep(min(0.5, max(0.0, deadline - loop.time())))
    view = await _detail(set_id, items=items)   # reconciles, so the answer is settled truth
    return {"set": view, "settled": view["status"] != store.ACTIVE}


@router.get("/variant-sets/{set_id}/items", response_model=list[ItemView],
            response_model_exclude_unset=True, responses=error_responses(404))
async def list_items(
    set_id: int, stage: int | None = None, state: str | None = None,
    limit: int | None = Query(None, ge=1, le=10_000, description="Page size; all by default."),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    """Items in stage-then-combination order, optionally filtered and paged."""
    if store.get_set(set_id) is None:
        raise HTTPException(status_code=404, detail="variant set not found")
    rows = store.items_for_set(set_id, stage=stage)
    if state:
        rows = [r for r in rows if r.state == state]
    rows = rows[offset:offset + limit] if limit is not None else rows[offset:]
    return service.item_views(rows)


@router.get("/variant-sets/{set_id}/items/{item_id}", response_model=ItemView,
            response_model_exclude_unset=True, responses=error_responses(404))
async def get_item(set_id: int, item_id: int) -> dict[str, Any]:
    item = store.get_item(item_id)
    if item is None or item.set_id != set_id:
        raise HTTPException(status_code=404, detail="variant not found in this set")
    view = service.item_views([item])[0]
    view["params"] = redact(item.params)   # input paths, relative to the app folder
    return view


@router.post("/variant-sets/{set_id}/retry", response_model=RetryResult,
             responses=error_responses(404, 409))
async def retry(set_id: int, body: RetryBody | None = None) -> dict[str, Any]:
    try:
        return await service.retry(set_id, include_canceled=bool(body and body.include_canceled))
    except service.VariantSetError as error:
        raise _http(error) from None


@router.post("/variant-sets/{set_id}/items/{item_id}/rerun", response_model=ItemView,
             response_model_exclude_unset=True, responses=error_responses(404, 409))
async def rerun_item(set_id: int, item_id: int, body: RerunBody | None = None) -> dict[str, Any]:
    options = body or RerunBody()
    try:
        item = await service.rerun_item(set_id, item_id, reseed=options.reseed,
                                        cascade=options.cascade)
    except service.VariantSetError as error:
        raise _http(error) from None
    return service.item_views([item])[0]


@router.post("/variant-sets/{set_id}/cancel", response_model=CancelResult,
             responses=error_responses(404))
async def cancel(set_id: int) -> dict[str, Any]:
    try:
        return await service.cancel(set_id)
    except service.VariantSetError as error:
        raise _http(error) from None


@router.delete("/variant-sets/{set_id}", response_model=OkResult,
               responses=error_responses(404, 409))
async def delete_set(set_id: int) -> dict[str, Any]:
    """Remove the set record. Its jobs and images stay in the library."""
    try:
        service.delete(set_id)
    except service.VariantSetError as error:
        raise _http(error) from None
    return {"ok": True}


# ---- masks ----------------------------------------------------------------------------------------
@router.post("/variant-sets/masks", responses=error_responses(400))
async def upload_mask(mask: UploadFile) -> dict[str, Any]:
    """Store a mask as a library asset so recipes can name it durably.

    Masks are ordinary image assets with the generator `mask` (white = change,
    black = keep), which is what lets a future segmentation tool produce
    reusable masks without a new table: it only has to save one.
    """
    from ..utils.io import save_image

    data = await mask.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="empty mask")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="mask too large")

    def _store() -> int:
        try:
            image = load_uploaded_image(data).convert("L")
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from None
        if image.getbbox() is None:
            raise HTTPException(status_code=400, detail="the mask is empty: paint what should change")
        settings.ensure_dirs()
        saved = save_image(image, "mask")   # no generation metadata: a mask is not generated
        asset = db.add_asset(AssetKind.image.value, saved.path, thumb=saved.thumb,
                             width=saved.width, height=saved.height, generator="mask",
                             meta={"mask": {"source": "upload"}},
                             content_hash=saved.content_hash, size_bytes=saved.size_bytes,
                             mime_type="image/png")
        assert asset.id is not None
        return int(asset.id)

    asset_id = await asyncio.to_thread(_store)
    return {"asset_id": asset_id}


# ---- export ---------------------------------------------------------------------------------------
class ExportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_intermediate: bool = False


@router.get("/variant-sets/{set_id}/manifest", responses=error_responses(404))
async def manifest(set_id: int, include_intermediate: bool = False) -> dict[str, Any]:
    """The manifest an export would contain, without building the archive."""
    from ..variant_sets import export

    try:
        return await asyncio.to_thread(
            export.manifest, set_id, include_intermediate=include_intermediate,
            include_generation=settings.effective_bool("embed_metadata"))
    except service.VariantSetError as error:
        raise _http(error) from None


@router.post("/variant-sets/{set_id}/export", responses=error_responses(404))
async def export_set(set_id: int, body: ExportBody | None = None) -> FileResponse:
    """Every successful output under its deterministic name, plus a manifest
    mapping each file to its variant. The same writer as the gallery export."""
    import os
    import tempfile
    from pathlib import Path

    from starlette.background import BackgroundTask

    from .. import exports
    from ..variant_sets import export
    from ..variant_sets.expansion import slug

    options = body or ExportBody()
    include_generation = settings.effective_bool("embed_metadata")
    try:
        plan = await asyncio.to_thread(export.plan, set_id,
                                       include_intermediate=options.include_intermediate,
                                       include_generation=include_generation)
    except service.VariantSetError as error:
        raise _http(error) from None
    settings.ensure_dirs()
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=settings.temp_path)
    os.close(fd)

    def _build() -> None:
        exports.write_zip(Path(tmp), plan.entries, include_generation=include_generation,
                          extra=plan.extra)
        for entry in plan.entries:          # an export is a use, as in the gallery
            if entry.asset.id is not None:
                db.mark_used(int(entry.asset.id))

    try:
        await asyncio.to_thread(_build)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    filename = f"{slug(plan.row.name) or 'variant-set'}-{plan.row.id}.zip"
    return FileResponse(tmp, media_type="application/zip", filename=filename,
                        background=BackgroundTask(lambda: Path(tmp).unlink(missing_ok=True)))
