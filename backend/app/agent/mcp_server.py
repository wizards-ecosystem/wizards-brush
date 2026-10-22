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
from mcp.types import CallToolResult, ResourceLink, TextContent
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
from . import core

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
) -> dict[str, Any]:
    """Save the current settings as a recipe. The result includes input_schema."""
    try:
        with client_tag.use("mcp"):
            return await core.save_recipe(
                name=name, kind=kind, params=params, expose=expose, description=description,
                inputs=inputs, slug=slug, field_descriptions=field_descriptions,
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
