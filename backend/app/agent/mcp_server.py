"""MCP server for Brush. Tools call the job API; nothing here generates pixels.

Mounted at ``/api/mcp`` by the app. Streamable HTTP sessions are started from
the app lifespan (`open_sessions`), because a mounted app's own lifespan does
not run. The session manager can be entered only once, so each lifespan builds
a fresh one — the test suite starts the app once per module.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast

from fastapi import HTTPException
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ResourceLink,
    TextContent,
)
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Match, NoMatchFound
from starlette.types import Receive, Scope, Send

from .. import client_tag
from ..problems import RequestError, as_request_error, error_body
from ..routers import job_api
from ..routers.job_api import (
    DryRunResult,
    JobCreate,
    JobInputs,
    JobSubmission,
    JobWait,
    MemoryImage,
)
from ..version import get_version
from . import core, studio_tools

# A client that asks to wait is answered inside this budget, then told to poll.
# The slice is how often progress is read; tests shorten it.
WAIT_SLICE_SECONDS = 2.0
WAIT_BUDGET_SECONDS = 55.0
_PREVIEW_SIDE = 512
_MAX_BODY = 80 * 1024 * 1024

_INSTRUCTIONS = (
    "Brush generates and edits images and video on this machine. "
    "Call list_kinds, then describe_kind, before generate or edit. "
    "Settings are strict: an unknown name or an out-of-range value is refused "
    "with field, code, valid_options and suggestions. "
    "Saved recipes are named setups. list_recipes and get_recipe publish "
    "input_schema; run_recipe accepts only those fields. "
    "dry_run checks a request and does not queue a job. "
    "Presets are versioned setups: list_presets and get_preset describe them, "
    "resolve_preset returns the execution plan with no side effects, run_preset queues "
    "it (pass expected_plan_hash from resolve), cancel_job reports what really happened. "
    "list_targets says where work can run; nothing here rents hardware. "
    "Images come back as brush:// resource links and file_url paths; "
    "set inline_images to also receive a small preview. Do not invent settings."
)

mcp = MCPServer(
    "The Wizard's Brush",
    title="Brush",
    version=get_version(),
    instructions=_INSTRUCTIONS,
)


class Outcome(BaseModel):
    """What generate, edit, get_job and wait_job return."""

    dry_run: bool = False
    settled: bool | None = None
    job: dict[str, Any] | None = None
    assets: list[dict[str, Any]] = Field(default_factory=list)
    kind: str | None = None
    params: dict[str, Any] | None = None
    would_queue: int | None = None
    duplicate: bool = False
    job_id: int | None = None
    job_ids: list[int] = Field(default_factory=list)
    recipe_id: int | None = None
    recipe_slug: str | None = None
    recipe_name: str | None = None


class KindCatalogue(BaseModel):
    kinds: list[dict[str, Any]]


def _memory(item: core.PreparedImage) -> MemoryImage:
    return MemoryImage(item.asset_id, item.width, item.height, item.image)


def _fail(exc: HTTPException) -> ToolError:
    """Tool error whose text is the same JSON object the jobs API returns."""
    return ToolError(json.dumps(error_body(as_request_error(exc))))


def _transport_security() -> TransportSecuritySettings:
    """Leave the SDK host allow-list off.

    The list is captured when the session starts, so it cannot follow
    API_TOKEN or a Host the bind address does not name. The app's own
    middleware is the live check: a non-loopback Host is refused until
    API_TOKEN is set, and then the token is the boundary.
    """
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


class MountedMCP:
    """ASGI app for ``/api/mcp``. The real server is installed in `open_sessions`."""

    def __init__(self) -> None:
        self.inner: Any = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("path") in ("", "/"):
            scope = cast(Scope, {**scope, "path": "/"})
        inner = self.inner
        if inner is None:
            await JSONResponse({"detail": "MCP session is not running"}, status_code=503)(
                scope, receive, send,
            )
            return
        await inner(scope, receive, send)


http_app = MountedMCP()


class ExactMCP(BaseRoute):
    """POST ``/api/mcp`` with no trailing slash.

    Starlette's mount only matches ``/api/mcp/{rest}``, so the slash-less URL
    falls through to the SPA's GET catch-all and answers 405 once the frontend
    is built. Cursor and the docs use the slash-less URL.
    """

    def __init__(self, path: str, app: Any) -> None:
        self.path = path
        self.app = app

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] == "http" and scope.get("path") == self.path:
            return Match.FULL, {}
        return Match.NONE, {}

    def url_path_for(self, name: str, /, **path_params: Any) -> Any:
        raise NoMatchFound(name, path_params)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        rewritten = dict(scope)
        root = rewritten.get("root_path", "")
        rewritten["root_path"] = f"{root}{self.path}"
        rewritten["path"] = f"{rewritten['root_path']}/"
        await self.app(rewritten, receive, send)


exact_route = ExactMCP("/api/mcp", http_app)


@asynccontextmanager
async def open_sessions():
    """Run the Streamable HTTP session manager for one app lifespan."""
    http_app.inner = mcp.streamable_http_app(
        streamable_http_path="/",
        max_request_body_size=_MAX_BODY,
        transport_security=_transport_security(),
    )
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        http_app.inner = None


def _preview_png(asset_id: int) -> bytes | None:
    """A PNG no larger than 512 px on a side, or None when the file is not an image."""
    from io import BytesIO

    from PIL import Image as PILImage

    from .. import db

    row = db.get_asset(asset_id)
    if row is None or row.kind != "image" or not row.path:
        return None
    try:
        with PILImage.open(row.path) as im:
            im.load()
            converted = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            converted.thumbnail((_PREVIEW_SIDE, _PREVIEW_SIDE))
            buf = BytesIO()
            converted.save(buf, format="PNG")
            return buf.getvalue()
    except OSError:
        return None


def _finish(outcome: Outcome, *, inline: bool) -> CallToolResult:
    content: list[Any] = [TextContent(type="text", text=outcome.model_dump_json())]
    for asset in outcome.assets:
        asset_id = asset.get("id")
        if not isinstance(asset_id, int):
            continue
        content.append(ResourceLink(
            name=str(asset.get("filename") or f"asset-{asset_id}"),
            uri=f"brush://assets/{asset_id}",
            mime_type="application/json",
            description=str(asset.get("file_url") or ""),
        ))
        if inline and asset.get("kind") == "image":
            png = _preview_png(asset_id)
            if png is not None:
                content.append(Image(data=png, format="png").to_image_content())
    return CallToolResult(
        content=content,
        structured_content=outcome.model_dump(mode="json"),
    )


def _published(result: CallToolResult) -> Outcome:
    """Hand a CallToolResult back through a tool annotated as `Outcome`.

    The SDK validates `structured_content` against that annotation and keeps
    the extra content blocks (resource links, optional previews).
    """
    return cast(Outcome, result)


def _from_wait(view: JobWait, submission: JobSubmission) -> Outcome:
    return Outcome(
        settled=view.settled,
        job=core.public_job(view.job),
        assets=[core.public_asset(asset) for asset in view.assets],
        kind=view.job.kind,
        job_id=submission.job_id,
        job_ids=list(submission.job_ids),
        duplicate=submission.duplicate,
    )


async def _wait_first(job_id: int, ctx: Context) -> JobWait:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_BUDGET_SECONDS
    last = -1.0
    view = await core.wait(job_id, 0)
    while True:
        progress = float(view.job.progress or 0.0)
        if progress > last:
            last = progress
            await ctx.report_progress(progress, 1.0, view.job.message or None)
        if view.settled:
            return view
        remaining = deadline - loop.time()
        if remaining <= 0:
            return view
        view = await core.wait(job_id, min(WAIT_SLICE_SECONDS, remaining))


async def _submit(body: JobCreate, *, wait: bool, inline: bool, ctx: Context) -> Outcome:
    try:
        with client_tag.use("mcp"):
            result = await core.submit(body)
    except HTTPException as exc:
        raise _fail(exc) from None
    assert not isinstance(result, core.job_api.PreparedPlan)  # these tools send kind-form bodies
    return await _present(result, wait=wait, inline=inline, ctx=ctx)


def _with_recipe(outcome: Outcome, recipe: tuple[int, str, str] | None) -> Outcome:
    if recipe is not None:
        outcome.recipe_id, outcome.recipe_slug, outcome.recipe_name = recipe
    return outcome


async def _present(
    result: JobSubmission | DryRunResult, *, wait: bool, inline: bool, ctx: Context,
    recipe: tuple[int, str, str] | None = None,
) -> Outcome:
    if isinstance(result, DryRunResult):
        return _published(_finish(_with_recipe(Outcome(
            dry_run=True,
            kind=result.kind,
            params=result.params,
            would_queue=result.would_queue,
            duplicate=result.duplicate,
            job_id=result.job_id,
            job_ids=list(result.job_ids),
        ), recipe), inline=False))
    submission = result
    if not wait:
        try:
            row = await core.job(submission.job_id)
        except HTTPException as exc:
            raise _fail(exc) from None
        settled = row.status in {"done", "error", "canceled"}
        return _published(_finish(_with_recipe(Outcome(
            settled=settled,
            job=core.public_job(row),
            kind=row.kind,
            job_id=submission.job_id,
            job_ids=list(submission.job_ids),
            duplicate=submission.duplicate,
        ), recipe), inline=False))
    try:
        view = await _wait_first(submission.job_id, ctx)
    except HTTPException as exc:
        raise _fail(exc) from None
    return _published(_finish(_with_recipe(_from_wait(view, submission), recipe), inline=inline))


@mcp.tool()
async def list_kinds(
    category: Annotated[str | None, Field(description="generator or tool. Omit for both.")] = None,
    available_only: Annotated[bool, Field(description="Hide kinds this install cannot run.")] = True,
) -> KindCatalogue:
    """Kinds this install can run. Call describe_kind before choosing settings."""
    return KindCatalogue(kinds=await core.kind_rows(category=category, available_only=available_only))


@mcp.tool()
async def describe_kind(kind: str) -> dict[str, Any]:
    """One kind: inputs, lane, and the JSON Schema POST /api/jobs enforces."""
    try:
        return await core.describe(kind)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def generate(
    prompt: Annotated[str, Field(description="What to generate.")],
    ctx: Context,
    kind: Annotated[str, Field(description="A kind from list_kinds.")] = "image_local",
    params: dict[str, Any] | None = None,
    wait: Annotated[bool, Field(description="Wait until the job settles, or up to 55 seconds.")] = True,
    request_id: Annotated[str | None, Field(
        description="Idempotency key, 16-128 of letters, digits, _ and -.")] = None,
    inline_images: Annotated[bool, Field(
        description="Also return a downscaled PNG preview. Default is links only.")] = False,
    dry_run: Annotated[bool, Field(
        description="Validate and return the params that would be queued. Creates no job.")] = False,
) -> Outcome:
    """Queue a generation. Settings you omit take the kind's defaults."""
    settings_map = dict(params or {})
    if "prompt" in settings_map and settings_map["prompt"] != prompt:
        raise _fail(RequestError(
            400, "prompt does not match params.prompt",
            code="invalid_request", field="prompt",
            suggestions=["Pass the prompt once, either as prompt or as params.prompt."],
        ))
    settings_map["prompt"] = prompt
    body = JobCreate(kind=kind, params=settings_map, dry_run=dry_run, request_id=request_id)
    return await _submit(body, wait=wait, inline=inline_images, ctx=ctx)


@mcp.tool()
async def edit(
    images: Annotated[list[int | str], Field(
        description="Asset ids, or data:image/...;base64 URLs. http URLs are refused.")],
    ctx: Context,
    instruction: Annotated[str | None, Field(description="The edit prompt.")] = None,
    kind: Annotated[str, Field(description="A kind that takes images.")] = "image_edit",
    params: dict[str, Any] | None = None,
    mask: Annotated[int | str | None, Field(description="Mask asset id or data URL.")] = None,
    wait: bool = True,
    request_id: str | None = None,
    inline_images: bool = False,
    dry_run: bool = False,
) -> Outcome:
    """Edit existing images. Pass asset ids from an earlier job, or data URLs.

    A dry run decodes data URLs in memory. It does not add them to the gallery
    or start captioning. A repeated request_id is answered before any import.
    """
    try:
        _, prior = job_api.duplicate_of(request_id, dry_run=dry_run)
    except HTTPException as exc:
        raise _fail(exc) from None
    if prior is not None:
        return await _present(prior, wait=wait, inline=inline_images, ctx=ctx)
    settings_map = dict(params or {})
    if instruction:
        if "prompt" in settings_map and settings_map["prompt"] != instruction:
            raise _fail(RequestError(
                400, "instruction does not match params.prompt",
                code="invalid_request", field="prompt",
                suggestions=["Pass the edit text once, as instruction or as params.prompt."],
            ))
        settings_map["prompt"] = instruction
    try:
        prepared = [
            await core.prepare_image(value, persist=not dry_run) for value in images
        ]
        mask_prepared = (
            None if mask is None
            else await core.prepare_image(mask, role="mask", persist=not dry_run)
        )
    except HTTPException as exc:
        raise _fail(exc) from None
    unsaved = any(item.asset_id is None for item in prepared) or (
        mask_prepared is not None and mask_prepared.asset_id is None
    )
    if dry_run and unsaved:
        try:
            result = await job_api.dry_run_unsaved(
                kind, settings_map,
                [_memory(item) for item in prepared],
                None if mask_prepared is None else _memory(mask_prepared),
                request_id=request_id,
            )
        except HTTPException as exc:
            raise _fail(exc) from None
        return await _present(result, wait=False, inline=False, ctx=ctx)
    ids = [item.asset_id for item in prepared]
    if any(item is None for item in ids):
        raise _fail(RequestError(
            500, "edit lost an image id", code="error", field="images",
        ))
    body = JobCreate(
        kind=kind,
        params=settings_map,
        inputs=JobInputs(
            images=[int(item) for item in ids if item is not None],
            mask=None if mask_prepared is None else mask_prepared.asset_id,
        ),
        dry_run=dry_run,
        request_id=request_id,
    )
    return await _submit(body, wait=wait, inline=inline_images, ctx=ctx)


@mcp.tool()
async def get_job(job_id: int) -> Outcome:
    """The job as it is now, plus any outputs already saved."""
    try:
        view = await core.wait(job_id, 0)
    except HTTPException as exc:
        raise _fail(exc) from None
    return _published(_finish(
        _from_wait(view, JobSubmission(job_id=job_id, job_ids=[job_id])),
        inline=False,
    ))


@mcp.tool()
async def wait_job(
    job_id: int,
    timeout: Annotated[float, Field(description="Seconds to wait, at most 60.", ge=0, le=60)] = 30,
) -> Outcome:
    """Wait until the job settles or `timeout` elapses. settled false means call again."""
    try:
        view = await core.wait(job_id, min(max(float(timeout), 0.0), 60.0))
    except HTTPException as exc:
        raise _fail(exc) from None
    return _published(_finish(
        _from_wait(view, JobSubmission(job_id=job_id, job_ids=[job_id])),
        inline=False,
    ))


@mcp.tool()
async def search_library(
    q: str | None = None,
    kind: str | None = None,
    favorite: bool | None = None,
    semantic: bool = False,
    limit: Annotated[int, Field(description="At most 50.", ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Search the gallery.

    Each item's file_url is `/api/assets/{id}/file`, which accepts the API
    token. `/files/...` does not: once a token is set it accepts only the
    browser cookie.
    """
    return await core.search(q, kind=kind, favorite=favorite, semantic=semantic, limit=limit)


@mcp.tool()
async def list_recipes() -> dict[str, Any]:
    """Saved recipes. Each entry's input_schema is what run_recipe accepts."""
    return {"recipes": await core.recipe_rows()}


@mcp.tool()
async def get_recipe(
    recipe: Annotated[str, Field(description="Recipe id, slug, or name.")],
) -> dict[str, Any]:
    """One recipe: frozen params, exposed fields, and the JSON Schema for inputs."""
    try:
        return await core.recipe_detail(recipe)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def save_recipe(
    name: Annotated[str, Field(description="Display name. Also used as the slug when slug is omitted.")],
    kind: Annotated[str, Field(description="A kind from list_kinds.")],
    params: dict[str, Any] | None = None,
    expose: Annotated[list[str] | None, Field(
        description="Setting or input names a later run may change. Other settings stay frozen. "
                    "Include prompt when the recipe should take a new prompt.")] = None,
    description: str = "",
    inputs: dict[str, Any] | None = None,
    slug: str | None = None,
    field_descriptions: dict[str, str] | None = None,
    styles: Annotated[list[dict[str, str]] | None, Field(
        description="Ordered style fragments [{id, text}] composed onto the prompt the way "
                    "the form does: prompt, then each text, joined by ', '.")] = None,
) -> dict[str, Any]:
    """Save the current settings as a recipe. The result includes input_schema."""
    try:
        with client_tag.use("mcp"):
            return await core.save_recipe(
                name=name, kind=kind, params=params, expose=expose, description=description,
                inputs=inputs, slug=slug, field_descriptions=field_descriptions, styles=styles,
            )
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def run_recipe(
    recipe: Annotated[str, Field(description="Recipe id, slug, or name.")],
    ctx: Context,
    inputs: Annotated[dict[str, Any] | None, Field(
        description="Values for the fields in the recipe's input_schema. Unknown keys are refused. "
                    "Omit a key to keep its saved default.")] = None,
    wait: Annotated[bool, Field(description="Wait until the job settles, or up to 55 seconds.")] = True,
    request_id: Annotated[str | None, Field(
        description="Idempotency key, 16-128 of letters, digits, _ and -.")] = None,
    inline_images: bool = False,
    dry_run: Annotated[bool, Field(
        description="Validate and return the params that would be queued. Creates no job.")] = False,
) -> Outcome:
    """Run a saved recipe. Call get_recipe first and send only its input_schema fields."""
    try:
        with client_tag.use("mcp"):
            view = await core.run_recipe(recipe, inputs, dry_run=dry_run, request_id=request_id)
    except HTTPException as exc:
        raise _fail(exc) from None
    if view.dry_run:
        result: JobSubmission | DryRunResult = DryRunResult(
            kind=view.kind, params=view.params or {}, would_queue=view.would_queue or 0,
            duplicate=view.duplicate, job_id=view.job_id, job_ids=list(view.job_ids),
        )
    else:
        result = JobSubmission(
            job_id=int(view.job_id or 0), job_ids=list(view.job_ids),
            group_id=view.group_id, duplicate=view.duplicate,
        )
    return await _present(
        result, wait=wait, inline=inline_images, ctx=ctx,
        recipe=(view.recipe_id, view.slug, view.name),
    )


# ---- discovery: targets, presets, resources, processors ------------------------------------
@mcp.tool()
async def list_targets() -> dict[str, Any]:
    """Where work can run: capability reports for this machine and the Remote GPU,
    the routing policy, and the capability_revision. Read-only; never rents hardware.
    The policy is changed by the person in Settings, not by an agent."""
    return await studio_tools.targets()


@mcp.tool()
async def list_processors() -> dict[str, Any]:
    """Finishing processors (with their options and availability) and validators."""
    return await studio_tools.processors()


@mcp.tool()
async def list_presets(
    origin: Annotated[str | None, Field(description="curated or custom. Omit for both.")] = None,
    tag: str | None = None,
    q: Annotated[str | None, Field(description="Text to look for in name, description, tags.")] = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    """The preset catalogue: head revisions by name."""
    try:
        if origin not in (None, "curated", "custom"):
            raise RequestError(400, "origin must be curated or custom", code="invalid_option",
                               field="origin", valid_options=["curated", "custom"])
        rows = await studio_tools.presets(include_archived=include_archived,
                                          origin=origin, tag=tag, q=q)
    except HTTPException as exc:
        raise _fail(exc) from None
    return {"presets": rows}


@mcp.tool()
async def get_preset(
    preset: Annotated[str, Field(description="A preset id such as custom/storybook.")],
    revision: Annotated[int | None, Field(description="A revision; the head when omitted.",
                                          ge=1)] = None,
) -> dict[str, Any]:
    """One preset revision, including its full definition (adapters, prompt
    fragments, settings, ordered finishing, reference roles, validation)."""
    try:
        return await studio_tools.preset(preset, revision)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def preset_revisions(preset: str) -> dict[str, Any]:
    """Every revision of a preset, oldest first. Revisions are never removed."""
    try:
        return {"revisions": await studio_tools.preset_history(preset)}
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def list_resources(
    kind: Annotated[str | None, Field(description="model, adapter or component.")] = None,
    family: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """Models and adapters by identity, with state, verification, licence and
    installs. Installs show a managed root and a relative path only."""
    try:
        if kind not in (None, "model", "adapter", "component"):
            raise RequestError(400, "kind must be model, adapter or component",
                               code="invalid_option", field="kind",
                               valid_options=["model", "adapter", "component"])
        return {"resources": await studio_tools.resources(kind=kind, family=family, state=state)}
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def get_resource(resource: Annotated[str, Field(description="A resource id.")]) -> dict[str, Any]:
    """One resource."""
    try:
        return await studio_tools.resource(resource)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def rescan_resources() -> dict[str, Any]:
    """Side effect: re-register configured models and hash new LoRA library files."""
    return await studio_tools.rescan_resources()


@mcp.tool()
async def prepare_resource_on_remote(
    resource: Annotated[str, Field(description="An imported adapter's resource id.")],
) -> dict[str, Any]:
    """Side effect: copy an imported adapter to the connected Remote GPU worker by
    content, in the background. Poll resource_preparation_status. Storing it on
    the worker does not make remote jobs apply it."""
    try:
        return await studio_tools.prepare_remote(resource)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def resource_preparation_status(resource: str) -> dict[str, Any]:
    """Progress of prepare_resource_on_remote."""
    try:
        return await studio_tools.prepare_status(resource)
    except HTTPException as exc:
        raise _fail(exc) from None


# ---- presets: authoring ----------------------------------------------------------------------
@mcp.tool()
async def save_preset(
    definition: Annotated[dict[str, Any], Field(
        description="A preset definition (schema wizards-brush/preset, schema_version 2). "
                    "It is saved as custom at revision 1; curated is reserved for shipped "
                    "presets. See docs/studio-contracts.md.")],
) -> dict[str, Any]:
    """Side effect: save a new custom preset. Its client is recorded as mcp."""
    try:
        with client_tag.use("mcp"):
            return await studio_tools.save_preset(definition)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def update_preset(
    preset: str,
    definition: dict[str, Any],
    base_revision: Annotated[int, Field(description="The revision you edited.", ge=1)],
    on_conflict: Annotated[str, Field(
        description="error (refuse a stale base) or copy (save yours as a new preset).")] = "error",
) -> dict[str, Any]:
    """Side effect: save an edit as the next revision. A curated preset becomes a
    custom copy (cloned_from). A stale base is revision_conflict unless copy."""
    try:
        if on_conflict not in ("error", "copy"):
            raise RequestError(400, "on_conflict must be error or copy", code="invalid_option",
                               field="on_conflict", valid_options=["error", "copy"])
        with client_tag.use("mcp"):
            return await studio_tools.update_preset(
                preset, definition, base_revision, cast(Any, on_conflict))
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def clone_preset(
    preset: str,
    revision: Annotated[int | None, Field(ge=1)] = None,
    name: str | None = None,
    new_id: Annotated[str | None, Field(description="An id like custom/my-look.")] = None,
) -> dict[str, Any]:
    """Side effect: copy a revision into a new custom preset."""
    try:
        with client_tag.use("mcp"):
            return await studio_tools.clone_preset(preset, revision=revision, name=name,
                                                   new_id=new_id)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def archive_preset(
    preset: str,
    archived: Annotated[bool, Field(description="False unarchives.")] = True,
) -> dict[str, Any]:
    """Side effect: leave (or return to) the catalogue. Revisions stay readable."""
    try:
        return await studio_tools.archive_preset(preset, archived)
    except HTTPException as exc:
        raise _fail(exc) from None


# ---- variant sets ------------------------------------------------------------------------------
_RECIPE = ("A Variant Set recipe: {sources, masks, content, seed: {mode, value}, stages: [{name, "
           "operation | preset: {id, revision?, target, allow_remote}, axes: [{name, values}], "
           "prompt, negative_prompt, params, value_params, mask, references, finishing, "
           "validation, naming}]}. See docs/api.md#variant-sets.")


def _validation_error(exc: Exception) -> RequestError:
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        path = ".".join(str(p) for p in first.get("loc", ()))
        return RequestError(400, f"{path}: {first.get('msg')}", code="invalid_request",
                            field=path or None)
    raise exc


@mcp.tool()
async def preview_variant_set(
    recipe: Annotated[dict[str, Any] | None, Field(description=_RECIPE)] = None,
    recipe_id: Annotated[int | None, Field(description="A saved variant recipe instead.")] = None,
    sources: Annotated[list[int] | None, Field(description="Replaces the recipe's sources.")] = None,
    limit: Annotated[int, Field(ge=0, le=1000)] = 100,
) -> dict[str, Any]:
    """Every variant a set would make: count, names, prompts, seeds, preset stages'
    pinned revisions and readiness. No side effects."""
    try:
        return await studio_tools.preview_variant_set(recipe, recipe_id, sources, limit)
    except HTTPException as exc:
        raise _fail(exc) from None
    except ValueError as exc:
        raise _fail(_validation_error(exc)) from None


@mcp.tool()
async def create_variant_set(
    recipe: Annotated[dict[str, Any] | None, Field(description=_RECIPE)] = None,
    recipe_id: int | None = None,
    sources: list[int] | None = None,
    name: str = "",
    request_id: Annotated[str | None, Field(
        description="Idempotency key: resending it returns the same set.")] = None,
) -> dict[str, Any]:
    """Side effect: create a set and queue its first stage through the ordinary
    queue. Preset stages are pinned to the revision they resolved."""
    try:
        with client_tag.use("mcp"):
            return await studio_tools.create_variant_set(recipe, recipe_id, sources, name,
                                                         request_id)
    except HTTPException as exc:
        raise _fail(exc) from None
    except ValueError as exc:
        raise _fail(_validation_error(exc)) from None


@mcp.tool()
async def wait_variant_set(
    variant_set: Annotated[int, Field(ge=1)],
    timeout: Annotated[float, Field(ge=0.0, le=60.0)] = 30.0,
    items: bool = False,
) -> dict[str, Any]:
    """The set's status, waiting up to `timeout` seconds for it to settle
    (complete, incomplete or canceled). `settled: false` means wait again."""
    try:
        return await studio_tools.wait_variant_set(variant_set, timeout, items)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def cancel_variant_set(variant_set: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
    """Side effect: cancel waiting variants and tell running ones to stop."""
    try:
        return await studio_tools.cancel_variant_set(variant_set)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def retry_variant_set(
    variant_set: Annotated[int, Field(ge=1)],
    include_canceled: bool = False,
) -> dict[str, Any]:
    """Side effect: run failed, invalid and blocked variants again with their
    stored params (canceled ones too when asked). Never rewrites a prompt."""
    try:
        return await studio_tools.retry_variant_set(variant_set, include_canceled)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def rerun_variant_item(
    variant_set: Annotated[int, Field(ge=1)],
    item: Annotated[int, Field(ge=1)],
    reseed: Annotated[bool, Field(description="A new seed; otherwise the stored one.")] = False,
    cascade: Annotated[bool, Field(description="Also rerun the variants built from it.")] = False,
) -> dict[str, Any]:
    """Side effect: run one variant again from its stored params."""
    try:
        return await studio_tools.rerun_variant_item(variant_set, item, reseed, cascade)
    except HTTPException as exc:
        raise _fail(exc) from None


_SPRITE_PROFILE = (
    "{mode: grid | atlas, name, order: set | key, columns, cell_width, cell_height, "
    "fit: require_match | pad | contain, max_width, max_height, trim, allow_multiple_pages, "
    "padding, extrude, background: transparent | #rrggbb, pixel_art, pivot, durations, "
    "partial}. See docs/api.md#sprite-sheets-and-atlases.")


@mcp.tool()
async def preview_sprite_export(
    variant_set: Annotated[int, Field(ge=1)],
    profile: Annotated[dict[str, Any], Field(description=_SPRITE_PROFILE)],
) -> dict[str, Any]:
    """The pages, sizes, frame order and rectangles a sheet or atlas export of the
    set's final stage would have, and any missing frames. No side effects."""
    try:
        return await studio_tools.preview_sprites(variant_set, profile)
    except HTTPException as exc:
        raise _fail(exc) from None
    except ValueError as exc:
        raise _fail(_validation_error(exc)) from None


@mcp.tool()
async def create_sprite_export(
    variant_set: Annotated[int, Field(ge=1)],
    profile: Annotated[dict[str, Any], Field(description=_SPRITE_PROFILE)],
) -> dict[str, Any]:
    """Side effect: build, validate and publish a sheet or atlas (PNG pages plus a
    versioned manifest, zipped) as an artifact. Fetch it with fetch_artifact or
    its file_url; it stays until expires_at."""
    try:
        with client_tag.use("mcp"):
            return await studio_tools.export_sprites(variant_set, profile)
    except HTTPException as exc:
        raise _fail(exc) from None
    except ValueError as exc:
        raise _fail(_validation_error(exc)) from None


@mcp.tool()
async def get_artifact(artifact: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
    """An export artifact's type, size, SHA-256, source and expiry."""
    try:
        return await studio_tools.artifact(artifact)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def fetch_artifact(artifact: Annotated[int, Field(ge=1)]) -> CallToolResult:
    """The artifact's bytes as an embedded resource, for clients that cannot fetch
    file_url with the API token. Capped at 16 MB."""
    import base64

    try:
        data, view = await asyncio.to_thread(studio_tools.artifact_bytes, artifact)
    except HTTPException as exc:
        raise _fail(exc) from None
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(view)),
                 EmbeddedResource(type="resource", resource=BlobResourceContents(
                     uri=f"brush://artifacts/{artifact}", mimeType=view["mime_type"],
                     blob=base64.b64encode(data).decode()))],
        structured_content={"artifact": view, "bytes": len(data)},
    )


# ---- reference sets ----------------------------------------------------------------------------
_REFERENCE_DEFINITION = (
    "{description, notes, roles: [{role, label, entries: [{asset_id, mask_id, note}]}], "
    "representatives: [asset ids]}. Order is kept exactly. Asset ids come from the gallery "
    "(fetch_asset, import_mask, or a finished job).")


@mcp.tool()
async def list_reference_sets(include_archived: bool = False) -> dict[str, Any]:
    """Reference sets: named, ordered references by role (a character, a product, a
    style). Reference material only; no model is promised to preserve an identity."""
    try:
        return {"reference_sets": await studio_tools.reference_sets(include_archived)}
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def get_reference_set(
    reference_set: Annotated[int, Field(ge=1)],
    revision: Annotated[int | None, Field(ge=1, description="Omit for the latest.")] = None,
) -> dict[str, Any]:
    """One revision of a reference set, with its ordered entries and masks."""
    try:
        return await studio_tools.reference_set(reference_set, revision)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def save_reference_set(
    name: Annotated[str, Field(min_length=1, max_length=80)],
    definition: Annotated[dict[str, Any], Field(description=_REFERENCE_DEFINITION)],
) -> dict[str, Any]:
    """Side effect: create a reference set at revision 1. Its client is recorded as mcp."""
    try:
        with client_tag.use("mcp"):
            return await studio_tools.save_reference_set(name, definition)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def update_reference_set(
    reference_set: Annotated[int, Field(ge=1)],
    definition: Annotated[dict[str, Any], Field(description=_REFERENCE_DEFINITION)],
    base_revision: Annotated[int, Field(ge=1, description="The revision you edited.")],
    name: str | None = None,
) -> dict[str, Any]:
    """Side effect: save the next revision. A stale base is revision_conflict. Plans
    and queued jobs keep the revision they used."""
    try:
        with client_tag.use("mcp"):
            return await studio_tools.update_reference_set(reference_set, definition,
                                                           base_revision, name)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def archive_reference_set(
    reference_set: Annotated[int, Field(ge=1)],
    archived: Annotated[bool, Field(description="False unarchives.")] = True,
) -> dict[str, Any]:
    """Side effect: hide (or restore) a set in pickers. Its revisions stay readable."""
    try:
        return await studio_tools.archive_reference_set(reference_set, archived)
    except HTTPException as exc:
        raise _fail(exc) from None


# ---- canvas ------------------------------------------------------------------------------------
_CANVAS_DOCUMENT = (
    "{schema_version: 1, width, height, color_space: \"srgb\", background: \"transparent\" | "
    "\"#rrggbb\", layers (bottom first): [{id, name, asset_id, mask_id, transform: {x, y, "
    "scale_x, scale_y, rotation (degrees clockwise about the layer centre)}, opacity, visible, "
    "blend_mode: normal | multiply | screen}]}. Layers reference gallery assets; nothing is "
    "copied.")


@mcp.tool()
async def list_canvases(include_archived: bool = False) -> dict[str, Any]:
    """Canvas projects: layered documents over gallery assets."""
    try:
        return {"canvases": await studio_tools.canvases(include_archived)}
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def get_canvas(
    canvas: Annotated[int, Field(ge=1)],
    revision: Annotated[int | None, Field(ge=1, description="Omit for the latest.")] = None,
) -> dict[str, Any]:
    """One revision of a canvas document. `missing` lists assets that are no
    longer in the gallery; their layers stay as placeholders."""
    try:
        return await studio_tools.canvas(canvas, revision)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def create_canvas(
    name: Annotated[str, Field(min_length=1, max_length=80)],
    document: Annotated[dict[str, Any], Field(description=_CANVAS_DOCUMENT)],
) -> dict[str, Any]:
    """Side effect: create a canvas project at revision 1."""
    try:
        with client_tag.use("mcp"):
            return await studio_tools.create_canvas(name, document)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def save_canvas(
    canvas: Annotated[int, Field(ge=1)],
    document: Annotated[dict[str, Any], Field(description=_CANVAS_DOCUMENT)],
    base_revision: Annotated[int, Field(ge=1, description="The revision you edited.")],
    name: str | None = None,
) -> dict[str, Any]:
    """Side effect: save the next revision. A stale base is revision_conflict, so
    an edit made in the UI meanwhile is never overwritten."""
    try:
        with client_tag.use("mcp"):
            return await studio_tools.save_canvas(canvas, document, base_revision, name)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def flatten_canvas(
    canvas: Annotated[int, Field(ge=1)],
    revision: Annotated[int | None, Field(ge=1)] = None,
    allow_missing: Annotated[bool, Field(description="Leave layers with missing assets out "
                                                     "instead of refusing.")] = False,
) -> dict[str, Any]:
    """Side effect: render a revision into a new gallery PNG (the same renderer as
    HTTP). The document is unchanged. Fetch the result with fetch_asset."""
    try:
        return await studio_tools.flatten_canvas(canvas, revision, allow_missing)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def add_canvas_layer(
    canvas: Annotated[int, Field(ge=1)],
    base_revision: Annotated[int, Field(ge=1)],
    asset_id: Annotated[int | None, Field(ge=1)] = None,
    job_id: Annotated[int | None, Field(ge=1, description="A finished job; its first output "
                                                          "becomes the layer.")] = None,
    name: str = "",
    from_layer: Annotated[str | None, Field(description="Take this layer's transform and record "
                                                        "it as lineage, e.g. after editing "
                                                        "it.")] = None,
    mask_id: Annotated[int | None, Field(ge=1)] = None,
) -> dict[str, Any]:
    """Side effect: put an asset (or a finished edit's output) on top as a new
    layer, saving the next revision."""
    try:
        with client_tag.use("mcp"):
            return await studio_tools.add_canvas_layer(
                canvas, base_revision=base_revision, asset_id=asset_id, job_id=job_id,
                name=name, from_layer=from_layer, mask_id=mask_id)
    except HTTPException as exc:
        raise _fail(exc) from None


# ---- plans -----------------------------------------------------------------------------------
_PLAN_ARGS = ("preset_id", "revision", "operation", "model", "adapters", "prompt",
              "negative_prompt", "styles", "style_fragments", "enhance", "notes", "settings",
              "finish", "target", "allow_remote", "seed")


def _plan_body(args: dict[str, Any], *, dry_run: bool, request_id: str | None = None,
               expected_plan_hash: str | None = None) -> JobCreate:
    from pydantic import ValidationError

    fields = {k: v for k, v in args.items() if k in _PLAN_ARGS and v is not None}
    inputs = args.get("inputs") or {}
    try:
        return JobCreate.model_validate({**fields, "inputs": inputs, "dry_run": dry_run,
                                         "request_id": request_id,
                                         "expected_plan_hash": expected_plan_hash})
    except ValidationError as exc:
        first = exc.errors()[0]
        path = ".".join(str(p) for p in first.get("loc", ()))
        raise _fail(RequestError(400, f"{path}: {first.get('msg')}", code="invalid_request",
                                 field=path or None)) from None


@mcp.tool()
async def resolve_preset(
    prompt: Annotated[str, Field(description="The subject the preset's template wraps.")] = "",
    preset_id: Annotated[str | None, Field(description="A preset; omit to use operation + model.")] = None,
    revision: Annotated[int | None, Field(ge=1)] = None,
    operation: Annotated[str | None, Field(description="text_to_image, edit, inpaint, ...")] = None,
    model: Annotated[str | None, Field(description="A model resource id.")] = None,
    adapters: Annotated[list[dict[str, Any]] | None, Field(
        description="Ordered [{resource, strength}]; [] means none.")] = None,
    negative_prompt: str | None = None,
    styles: list[str] | None = None,
    style_fragments: list[dict[str, str]] | None = None,
    enhance: bool | None = None,
    notes: str | None = None,
    settings: Annotated[dict[str, Any] | None, Field(
        description="Explicit overrides, validated strictly, never clamped.")] = None,
    finish: Annotated[list[dict[str, Any]] | None, Field(
        description="Ordered finishing steps; replaces the preset's.")] = None,
    inputs: Annotated[dict[str, Any] | None, Field(
        description="{images, mask, last_frame, references: {role: [asset ids]}, "
                    "reference_sets: {role: {id, revision?}}}")] = None,
    target: Annotated[str | None, Field(description="local, remote or auto.")] = None,
    allow_remote: Annotated[bool | None, Field(
        description="Let auto use a connected remote worker and send this request's inputs "
                    "there. Never rents hardware.")] = None,
    seed: Annotated[int | None, Field(
        description="Omit for random; the chosen seed is returned. Send it back to keep the "
                    "same request.", ge=0, le=2**32 - 1)] = None,
) -> dict[str, Any]:
    """Resolve a preset request into its execution plan. No side effects: no job,
    download, upload or rental. Same request as POST /api/jobs with dry_run, so
    the creative_request_hash matches the HTTP API's for the same inputs."""
    body = _plan_body(dict(locals()), dry_run=True)
    try:
        with client_tag.use("mcp"):
            result = await core.submit(body)
    except HTTPException as exc:
        raise _fail(exc) from None
    return result.model_dump(mode="json")


@mcp.tool()
async def run_preset(
    ctx: Context,
    prompt: str = "",
    preset_id: str | None = None,
    revision: Annotated[int | None, Field(ge=1)] = None,
    operation: str | None = None,
    model: str | None = None,
    adapters: list[dict[str, Any]] | None = None,
    negative_prompt: str | None = None,
    styles: list[str] | None = None,
    style_fragments: list[dict[str, str]] | None = None,
    enhance: bool | None = None,
    notes: str | None = None,
    settings: dict[str, Any] | None = None,
    finish: list[dict[str, Any]] | None = None,
    inputs: Annotated[dict[str, Any] | None, Field(
        description="{images, mask, last_frame, references: {role: [asset ids]}, "
                    "reference_sets: {role: {id, revision?}}}")] = None,
    target: str | None = None,
    allow_remote: bool | None = None,
    seed: Annotated[int | None, Field(ge=0, le=2**32 - 1)] = None,
    expected_plan_hash: Annotated[str | None, Field(
        description="The execution_plan_hash from resolve_preset. A plan that would now run "
                    "differently is refused as plan_stale.")] = None,
    request_id: Annotated[str | None, Field(
        description="Idempotency key. The same request returns its job; a different "
                    "request under the same key is request_conflict.")] = None,
    wait: Annotated[bool, Field(description="Wait until the job settles, or up to 55 s.")] = True,
    inline_images: bool = False,
) -> Outcome:
    """Side effect: queue the plan as an ordinary job. Returns the job id at once
    when wait is false; a transport timeout is not a failure, call get_job."""
    args = {k: v for k, v in locals().items() if k != "ctx"}
    body = _plan_body(args, dry_run=False, request_id=request_id,
                      expected_plan_hash=expected_plan_hash)
    try:
        with client_tag.use("mcp"):
            result = await core.submit(body)
    except HTTPException as exc:
        raise _fail(exc) from None
    assert isinstance(result, JobSubmission)
    return await _present(result, wait=wait, inline=inline_images, ctx=ctx)


@mcp.tool()
async def replay_job(
    job_id: int,
    ctx: Context,
    mode: Annotated[str, Field(
        description="exact: the stored plan or replay_resource_unavailable. latest: the same "
                    "request against today's preset revision.")] = "exact",
    dry_run: bool = False,
    request_id: str | None = None,
    wait: bool = True,
) -> dict[str, Any]:
    """Run a preset job again. exact never substitutes a resource."""
    try:
        if mode not in ("exact", "latest"):
            raise RequestError(400, "mode must be exact or latest", code="invalid_option",
                               field="mode", valid_options=["exact", "latest"])
        with client_tag.use("mcp"):
            result = await studio_tools.replay(job_id, mode=cast(Any, mode), dry_run=dry_run,
                                               request_id=request_id)
    except HTTPException as exc:
        raise _fail(exc) from None
    if isinstance(result, JobSubmission):
        outcome = await _present(result, wait=wait, inline=False, ctx=ctx)
        return cast(Any, outcome).structured_content or {}
    return result.model_dump(mode="json")


@mcp.tool()
async def get_plan(plan_id: int) -> dict[str, Any]:
    """An execution plan and the request that produced it (job.plan_id names it)."""
    try:
        return await studio_tools.plan(plan_id)
    except HTTPException as exc:
        raise _fail(exc) from None


# ---- cancellation, masks, bytes --------------------------------------------------------------
@mcp.tool()
async def cancel_job(
    job_id: int,
    wait_seconds: Annotated[float, Field(
        description="Wait up to this long (at most 60) for the job to settle.", ge=0, le=60)] = 0,
) -> dict[str, Any]:
    """Cancel a job. state is removed_from_queue (it never ran), cancellation_requested
    (running; not canceled until it settles), canceled, settled_otherwise (it finished
    first) or already_settled. Outputs already saved are kept."""
    try:
        return await studio_tools.cancel(job_id, wait_seconds=wait_seconds)
    except HTTPException as exc:
        raise _fail(exc) from None


@mcp.tool()
async def import_mask(
    mask: Annotated[str, Field(
        description="data:image/png;base64,... (white = change, black = keep). At most 16 MB.")],
    source_asset_id: Annotated[int | None, Field(
        description="The image the mask is for; its size must match.")] = None,
) -> dict[str, Any]:
    """Side effect: store a mask as a gallery asset for edit, inpaint or Variant Sets.
    Region masks and mattes come from the existing region_mask and matte kinds."""
    try:
        data = core.decode_data_url(mask)
        stored = await studio_tools.import_mask(data, source_asset_id=source_asset_id)
    except HTTPException as exc:
        raise _fail(exc) from None
    return core.public_asset(stored)


@mcp.tool()
async def fetch_asset(asset_id: int) -> CallToolResult:
    """The full-resolution image bytes, for clients that cannot fetch file_url with
    the API token. Capped at 16 MB; larger files and videos must use file_url."""
    try:
        data, mime, view = await asyncio.to_thread(studio_tools.asset_bytes, asset_id)
    except HTTPException as exc:
        raise _fail(exc) from None
    fmt = mime.split("/", 1)[1] if "/" in mime else "png"
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(view)),
                 Image(data=data, format=fmt).to_image_content()],
        structured_content={"asset": view, "bytes": len(data)},
    )


@mcp.resource("brush://recipes", mime_type="application/json")
async def recipes_resource() -> dict[str, Any]:
    """Every saved recipe, including the JSON Schema its inputs must match."""
    return {"recipes": await core.recipe_rows()}


@mcp.resource("brush://recipes/{key}", mime_type="application/json")
async def recipe_resource(key: str) -> dict[str, Any]:
    """One recipe by id, slug, or name."""
    try:
        return await core.recipe_detail(key)
    except RequestError as exc:
        raise ResourceNotFoundError(str(exc.detail)) from None


@mcp.resource("brush://kinds", mime_type="application/json")
async def kinds_resource() -> dict[str, Any]:
    """Every kind, including ones that cannot run on this install right now."""
    return {"kinds": await core.kind_rows(available_only=False)}


@mcp.resource("brush://kinds/{kind}", mime_type="application/json")
async def kind_resource(kind: str) -> dict[str, Any]:
    """One kind and the JSON Schema its settings must match."""
    try:
        return await core.describe(kind)
    except RequestError as exc:
        raise ResourceNotFoundError(str(exc.detail)) from None


@mcp.resource("brush://jobs/{job_id}", mime_type="application/json")
async def job_resource(job_id: str) -> dict[str, Any]:
    """One job, with paths shown relative to the app folder."""
    try:
        return core.public_job(await core.job(int(job_id)))
    except (RequestError, ValueError) as exc:
        raise ResourceNotFoundError("job not found") from exc


@mcp.resource("brush://assets/{asset_id}", mime_type="application/json")
async def asset_resource(asset_id: str) -> dict[str, Any]:
    """One asset as JSON, including file_url. Fetch the bytes from that URL."""
    from .. import db
    from ..models import AssetRead

    try:
        row = db.get_asset(int(asset_id))
    except ValueError:
        row = None
    if row is None:
        raise ResourceNotFoundError("asset not found")
    return core.public_asset(AssetRead.of(row))
