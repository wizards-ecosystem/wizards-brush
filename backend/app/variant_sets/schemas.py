"""Response shapes for the Variant Set API.

Declared so the OpenAPI document describes what a client receives, not just
what it sends. The routes return the service's plain dicts; FastAPI validates
them against these and serializes with `exclude_unset`, so a key the route did
not include (a set's items when `items=false`) is absent rather than null and
the JSON is exactly what it was before these existed.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import AssetRead

ItemState = Literal["pending", "queued", "succeeded", "invalid", "failed", "canceled",
                    "blocked"]
SetStatus = Literal["active", "complete", "incomplete", "canceled"]


class AxisView(BaseModel):
    name: str
    values: list[str]


class StageView(BaseModel):
    index: int
    name: str
    operation: str | None
    axes: list[AxisView]


class SetView(BaseModel):
    id: int
    name: str
    status: SetStatus
    operation: str
    recipe_id: int | None
    group_id: str = Field(description="Every child job carries it as its group_id.")
    expected: int
    counts: dict[str, Any] = Field(
        description="Items per state, `running` split out of `queued`, and per-stage counts.")
    collection_id: int | None
    source_asset_ids: list[int]
    stages: list[StageView]
    replay: dict[str, Any]
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ValidationResultView(BaseModel):
    validator: str
    status: Literal["pass", "warn", "fail"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ItemView(BaseModel):
    id: int
    stage: int
    ordinal: int
    key: str = Field(description="Canonical `axis=value,...` key; stable across retries.")
    values: dict[str, str]
    state: ItemState
    state_reason: str
    job_id: int | None
    job_status: str | None
    progress: float | None
    attempts: int
    parent_item_id: int | None
    source_asset_ids: list[int]
    asset_ids: list[int]
    asset: AssetRead | None
    validation_state: str
    validation: list[ValidationResultView]
    output_name: str
    prompt: str
    seed: int | None
    history: list[dict[str, Any]]
    params: dict[str, Any] | None = Field(
        default=None, description="The child job's params; single-item reads only.")


class SetDetail(SetView):
    recipe: dict[str, Any] | None = Field(
        default=None, description="The frozen recipe snapshot this set executes.")
    items: list[ItemView] | None = None
    created: bool | None = Field(
        default=None, description="On create: false when request_id named an earlier set.")


class SetWait(BaseModel):
    set: SetDetail
    settled: bool = Field(description="False when the wait timed out first; wait again.")


class RecipeView(BaseModel):
    id: int
    name: str
    description: str
    recipe: dict[str, Any]
    created_at: datetime
    updated_at: datetime
