"""Saved generation recipes: CRUD and run, on the same job path as the form.

`POST /api/recipes/{key}/run` validates the caller's inputs against the recipe's
schema, merges them onto the frozen params, and calls `create_job`. A recipe
run is an ordinary job: the queue, provenance, client tag, dry_run and
structured errors do not grow a second implementation.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import client_tag, db, log
from .. import recipes as saved
from ..problems import RequestError
from .common import error_responses
from .job_api import (
    DryRunResult,
    JobCreate,
    JobInputs,
    JobSubmission,
    PromptComposition,
    create_job,
)

logger = log.get("recipes")
router = APIRouter(tags=["recipes"])


class RecipeInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: list[int] = Field(default_factory=list, max_length=8)
    mask: int | None = None
    last_frame: int | None = None


class RecipeStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=48)
    text: str = Field(max_length=600)


class RecipeWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    kind: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    inputs: RecipeInputs = Field(default_factory=RecipeInputs)
    expose: list[str] = Field(default_factory=list, max_length=32)
    slug: str | None = Field(default=None, max_length=64)
    field_descriptions: dict[str, str] = Field(default_factory=dict)
    styles: list[RecipeStyle] = Field(
        default_factory=list, max_length=32,
        description="Style fragments, in order, composed onto the subject prompt as the "
                    "form does: subject, then each text, joined by ', '. Text is frozen.")


class RecipeFieldModel(BaseModel):
    name: str
    type: str
    label: str
    description: str = ""
    default: Any = None
    constraints: dict[str, Any] = Field(default_factory=dict)


class RecipeSummary(BaseModel):
    id: int
    name: str
    slug: str
    description: str
    kind: str
    fields: list[RecipeFieldModel]
    input_schema: dict[str, Any]
    styles: list[RecipeStyle] = Field(default_factory=list)
    client: str
    created_at: Any
    updated_at: Any


class RecipeRead(RecipeSummary):
    params: dict[str, Any]
    inputs: RecipeInputs


class RecipeRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="Values for the recipe's exposed fields. Unknown keys are refused. "
                    "Omitted keys keep the saved default.")
    dry_run: bool = Field(
        default=False,
        description="Validate and return the params that would be queued, without creating a job.")
    request_id: str | None = Field(
        default=None,
        description="Idempotency key (16-128 of A-Z a-z 0-9 _ -), forwarded to the job.")


class RecipeRun(BaseModel):
    recipe_id: int
    slug: str
    name: str
    kind: str
    dry_run: bool = False
    params: dict[str, Any] | None = None
    would_queue: int | None = None
    duplicate: bool = False
    job_id: int | None = None
    job_ids: list[int] = Field(default_factory=list)
    group_id: str | None = None


class OkResult(BaseModel):
    ok: bool = True


def _read(row: Any) -> RecipeRead:
    return RecipeRead.model_validate(saved.public_recipe(row))


def _summary(row: Any) -> RecipeSummary:
    return RecipeSummary.model_validate(saved.public_recipe(row, summary=True))


def _missing(key: str) -> RequestError:
    return RequestError(
        404, f"recipe '{key}' was not found",
        code="not_found", field="recipe",
        suggestions=["Call GET /api/recipes and pass an id, slug, or name."],
    )


def _conflict(field: str, detail: str) -> RequestError:
    return RequestError(
        409, detail, code="conflict", field=field,
        suggestions=["Choose a different name, or update the existing recipe."],
    )


def _allocate_slug(name: str, *, exclude_id: int | None) -> str:
    base = saved.slugify(name)
    slug = base
    for number in range(2, 1000):
        if db.recipe_by_slug(slug, exclude_id=exclude_id) is None:
            return slug
        slug = f"{base[:60].rstrip('-')}-{number}"
    raise _conflict("slug", f"no free slug near '{base}'")


def _claim_slug(slug: str, *, exclude_id: int | None) -> str:
    text = saved.check_slug(slug)
    if db.recipe_by_slug(text, exclude_id=exclude_id) is not None:
        raise _conflict("slug", f"slug '{text}' is already used")
    return text


def _store(body: RecipeWrite, *, exclude_id: int | None, previous_slug: str | None) -> dict[str, Any]:
    name = saved.clean_name(body.name)
    if db.recipe_by_name(name, exclude_id=exclude_id) is not None:
        raise _conflict("name", f"a recipe named '{name}' already exists")
    compiled = saved.compile_recipe(
        body.kind, body.params, list(body.expose), body.inputs.model_dump(), body.field_descriptions,
        [style.model_dump() for style in body.styles],
    )
    saved.validate_assets(compiled["kind"], compiled["inputs"])
    explicit = (body.slug or "").strip()
    if explicit:
        slug = _claim_slug(explicit, exclude_id=exclude_id)
    elif previous_slug:
        slug = _claim_slug(previous_slug, exclude_id=exclude_id)
    else:
        slug = _allocate_slug(name, exclude_id=exclude_id)
    return {"name": name, "slug": slug, **compiled, "description": body.description.strip()}


@router.get("/recipes")
async def list_recipes(
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[RecipeSummary]:
    """Saved recipes, newest first. Each row includes the schema a run must match."""
    return [_summary(row) for row in db.list_recipes(limit)]


@router.post("/recipes", responses=error_responses(400, 404, 409))
async def create_recipe(body: RecipeWrite) -> RecipeRead:
    """Save a kind's settings. `expose` names the fields a later run may change."""
    try:
        stored = _store(body, exclude_id=None, previous_slug=None)
        row = db.insert_recipe(
            name=stored["name"], slug=stored["slug"], description=stored["description"],
            kind=stored["kind"], params=stored["params"], fields=stored["fields"],
            inputs=stored["inputs"], defaulted=list(stored["defaulted"]),
            client=client_tag.current(), styles=list(stored["styles"]),
        )
    except db.RecipeConflict as error:
        raise _conflict("name", "that name or slug is already used") from error
    logger.info("saved recipe %s (%s)", row.slug, row.kind)
    return _read(row)


@router.get("/recipes/{key}", responses=error_responses(404))
async def read_recipe(key: str) -> RecipeRead:
    """One recipe by id, slug, or name, including frozen params and `input_schema`."""
    row = db.find_recipe(key)
    if row is None:
        raise _missing(key)
    return _read(row)


@router.put("/recipes/{key}", responses=error_responses(400, 404, 409))
async def update_recipe(key: str, body: RecipeWrite) -> RecipeRead:
    """Replace a recipe. The slug stays unless `slug` is sent. Runs already queued are unchanged."""
    current = db.find_recipe(key)
    if current is None or current.id is None:
        raise _missing(key)
    try:
        kept = None if (body.slug or "").strip() else current.slug
        stored = _store(body, exclude_id=current.id, previous_slug=kept)
        row = db.update_recipe(
            current.id, name=stored["name"], slug=stored["slug"], description=stored["description"],
            kind=stored["kind"], params=stored["params"], fields=stored["fields"],
            inputs=stored["inputs"], defaulted=list(stored["defaulted"]),
            styles=list(stored["styles"]),
        )
    except db.RecipeConflict as error:
        raise _conflict("name", "that name or slug is already used") from error
    if row is None:
        raise _missing(key)
    return _read(row)


@router.delete("/recipes/{key}", responses=error_responses(404))
async def remove_recipe(key: str) -> OkResult:
    """Delete a recipe. Jobs already queued from it are left alone."""
    row = db.find_recipe(key)
    if row is None or row.id is None or not db.delete_recipe(row.id):
        raise _missing(key)
    return OkResult()


@router.post("/recipes/{key}/run", responses=error_responses(400, 404, 409, 503))
async def run_recipe(key: str, body: RecipeRunBody) -> RecipeRun:
    """Run a recipe. Inputs are checked against its schema, then submitted as a normal job.

    `dry_run` returns the params that would be stored and does not queue a job.
    It uses the same checks as `POST /api/jobs`: a local GPU kind is still
    refused when this machine cannot run one, and a remote kind is not.
    """
    row = db.find_recipe(key)
    if row is None or row.id is None:
        raise _missing(key)
    params, inputs = saved.merge_run(
        row.kind, row.params, row.inputs, row.fields, body.inputs, row.defaulted,
    )
    composition = None
    if row.styles:
        # The subject is the run's prompt; the frozen styles follow it exactly
        # as they did on the form, and the job keeps the parts apart.
        composition = PromptComposition(subject=str(params.pop("prompt", "") or ""),
                                        styles=row.styles)
    result = await create_job(JobCreate(
        kind=row.kind, params=params, inputs=JobInputs.model_validate(inputs),
        dry_run=body.dry_run, request_id=body.request_id, composition=composition,
    ))
    return _run_view(row, result)


def _run_view(row: Any, result: JobSubmission | DryRunResult) -> RecipeRun:
    if isinstance(result, DryRunResult):
        return RecipeRun(
            recipe_id=int(row.id), slug=row.slug, name=row.name, kind=result.kind,
            dry_run=True, params=result.params, would_queue=result.would_queue,
            duplicate=result.duplicate, job_id=result.job_id, job_ids=list(result.job_ids),
        )
    return RecipeRun(
        recipe_id=int(row.id), slug=row.slug, name=row.name, kind=row.kind,
        duplicate=result.duplicate, job_id=result.job_id, job_ids=list(result.job_ids),
        group_id=result.group_id,
    )

