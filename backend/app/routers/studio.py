"""Studio presets over HTTP: the catalogue, revisions, custom edits and archive.

Presets are `recipe` rows with schema_version 2 (see studio/catalogue.py); the
saved-generator routes in routers/recipes.py keep serving v1 rows unchanged.
Running a preset is not a route here. A preset resolves into an ordinary job
through `POST /api/jobs`, so there is no second run engine.

Preset ids contain a "/" ("curated/fast-general"), so every route that adds a
suffix is registered before the plain `{preset_id:path}` read.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import client_tag
from ..studio import catalogue
from .common import error_responses

router = APIRouter(tags=["presets"])


class PresetSummary(BaseModel):
    id: str
    revision: int
    head_revision: int
    name: str
    description: str
    origin: Literal["curated", "custom"]
    tags: list[str]
    operation: str | None
    archived: bool
    client: str
    definition_hash: str
    created_at: Any
    updated_at: Any


class PresetRead(PresetSummary):
    definition: dict[str, Any] = Field(description="The revision's authored content.")
    cloned_from: dict[str, Any] | None = Field(
        default=None, description="Set when the save became a copy of another preset.")


class PresetRevision(BaseModel):
    revision: int
    based_on: int | None
    client: str
    definition_hash: str
    created_at: Any


class PresetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any] = Field(description="The full edited definition.")
    base_revision: int = Field(ge=1, description="The revision this edit started from.")
    on_conflict: Literal["error", "copy"] = Field(
        default="error",
        description="When base_revision is stale: refuse (409), or save this version as a "
                    "new custom preset so neither version is lost.")


class PresetClone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int | None = Field(default=None, ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=80)
    id: str | None = Field(default=None, max_length=96)


@router.get("/studio/presets", response_model=list[PresetSummary])
async def list_presets(
    include_archived: bool = False,
    origin: Annotated[Literal["curated", "custom"] | None, Query()] = None,
    tag: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[dict[str, Any]]:
    """The catalogue: curated and custom presets, head revisions, by name."""
    return catalogue.listing(include_archived=include_archived, origin=origin, tag=tag, query=q)


@router.post("/studio/presets", response_model=PresetRead, responses=error_responses(400, 409))
async def create_preset(definition: dict[str, Any]) -> dict[str, Any]:
    """Save a new custom preset at revision 1. Who saved it is recorded as `client`;
    an AI-authored preset is custom like any other."""
    return catalogue.create(definition, client=client_tag.current())


@router.get("/studio/presets/{preset_id:path}/revisions", response_model=list[PresetRevision],
            responses=error_responses(404))
async def preset_revisions(preset_id: str) -> list[dict[str, Any]]:
    """Every revision, oldest first. Revisions are never removed."""
    return catalogue.history(preset_id)


@router.post("/studio/presets/{preset_id:path}/clone", response_model=PresetRead,
             responses=error_responses(400, 404, 409))
async def clone_preset(preset_id: str, body: PresetClone) -> dict[str, Any]:
    """Copy a revision (the head by default) into a new custom preset."""
    return catalogue.clone(preset_id, revision=body.revision, name=body.name,
                           new_id=body.id, client=client_tag.current())


@router.post("/studio/presets/{preset_id:path}/archive", response_model=PresetRead,
             responses=error_responses(404))
async def archive_preset(preset_id: str) -> dict[str, Any]:
    """Leave the catalogue. Revisions stay readable for the jobs that ran them."""
    return catalogue.archive(preset_id, True)


@router.post("/studio/presets/{preset_id:path}/unarchive", response_model=PresetRead,
             responses=error_responses(404))
async def unarchive_preset(preset_id: str) -> dict[str, Any]:
    return catalogue.archive(preset_id, False)


@router.get("/studio/presets/{preset_id:path}", response_model=PresetRead,
            responses=error_responses(404))
async def read_preset(
    preset_id: str,
    revision: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """One preset: the head revision, or the one `revision` names."""
    return catalogue.get(preset_id, revision)


@router.put("/studio/presets/{preset_id:path}", response_model=PresetRead,
            responses=error_responses(400, 404, 409))
async def update_preset(preset_id: str, body: PresetUpdate) -> dict[str, Any]:
    """Save an edit as the next revision.

    A curated preset is never changed: the edit is saved as a custom copy and
    `cloned_from` says so. A stale `base_revision` is a 409 unless
    `on_conflict` is "copy".
    """
    return catalogue.update(preset_id, body.definition, base_revision=body.base_revision,
                            client=client_tag.current(), on_conflict=body.on_conflict)
