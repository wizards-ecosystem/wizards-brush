"""The unified job API: every generator and tool through one JSON endpoint.

The per-generator routes take multipart uploads because a browser form holds
files. A program usually holds something better — gallery asset ids, from its
own earlier jobs or from `POST /api/assets/import` — and wants one request
shape for everything:

    GET  /api/jobs/kinds       every kind, the inputs it takes and a JSON
                               Schema for its settings
    POST /api/jobs             {"kind", "params", "inputs", "request_id"}
    GET  /api/jobs/{id}/wait   long-poll until the job settles, with its outputs

There is no second implementation behind it. Settings go through the builders
the upload routes use (`images.build_params`, `videos.build_params`,
`tools.TOOLS`), so a job made here is indistinguishable from one made in the
UI: rerun, retry, queue ordering and Variant Sets treat it the same. What it
adds is strictness. The form routes clamp a bad value silently because a form
cannot send one; a program can, so here an unknown setting or an out-of-range
value is a 400 that names it.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .. import controls, db
from ..models import Asset, AssetKind, AssetRead, JobKind, JobRead, JobStatus
from ..problems import RequestError, as_request_error, close_suggestions
from ..queue import lane_for
from . import images, tools, videos
from .common import (
    _submission_response,
    as_int,
    error_responses,
    get_handler,
    normalize_request_id,
    submit,
)

router = APIRouter(tags=["jobs"])

TERMINAL = frozenset({JobStatus.done.value, JobStatus.error.value, JobStatus.canceled.value})
MAX_WAIT_SECONDS = 60.0
_POLL_SECONDS = 0.4


@dataclass(frozen=True)
class Inputs:
    """What a kind takes besides its settings, as gallery asset ids."""
    min_images: int = 0
    max_images: int = 0
    image_kind: str = AssetKind.image.value     # the asset kind `images` must be
    mask: Literal["none", "required"] = "none"
    last_frame: bool = False


_IMAGE_KINDS = frozenset({
    JobKind.image_local.value, JobKind.image_colab.value, JobKind.img2img.value,
    JobKind.inpaint.value, JobKind.inpaint_remote.value, JobKind.outpaint.value,
    JobKind.image_edit.value, JobKind.control_local.value,
})
_VIDEO_KINDS = frozenset({JobKind.t2v.value, JobKind.i2v.value, JobKind.long_video.value})
# The routes that check the local card before queuing, and the ones that also
# refuse a gated model with no cached weights; mirrored so the answer matches.
_LOCAL_GPU = frozenset({JobKind.image_local.value, JobKind.img2img.value,
                        JobKind.inpaint.value, JobKind.outpaint.value,
                        JobKind.control_local.value})
_MODEL_ACCESS = _LOCAL_GPU - {JobKind.control_local.value}
_FANOUT = frozenset({JobKind.image_local.value, JobKind.image_colab.value})

GENERATOR_INPUTS: dict[str, Inputs] = {
    JobKind.image_local.value: Inputs(),
    JobKind.image_colab.value: Inputs(),
    JobKind.img2img.value: Inputs(1, 1),
    JobKind.inpaint.value: Inputs(1, 1, mask="required"),
    JobKind.inpaint_remote.value: Inputs(1, 1, mask="required"),
    JobKind.outpaint.value: Inputs(1, 1),
    JobKind.image_edit.value: Inputs(1, 3),
    JobKind.control_local.value: Inputs(1, 1),
    JobKind.t2v.value: Inputs(),
    JobKind.i2v.value: Inputs(1, 1, last_frame=True),
    JobKind.long_video.value: Inputs(),
}


def inputs_for(kind: str, spec: dict[str, Any] | None = None) -> Inputs:
    """The inputs `kind` takes right now.

    i2v's last frame is only offered while the connected worker can condition
    on one, which the registry already knows; see its `image_inputs`.
    """
    if kind in tools.TOOLS:
        tool = tools.TOOLS[kind]
        return Inputs(1, 1, image_kind=tool.input)
    base = GENERATOR_INPUTS[kind]
    if base.last_frame and spec is not None:
        offered = {str(i.get("name")) for i in spec.get("image_inputs") or []}
        if "last_frame" not in offered:
            return Inputs(base.min_images, base.max_images, base.image_kind, base.mask, False)
    return base


# ---- request / response models -------------------------------------------------------------
class JobInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: list[int] = Field(default_factory=list, max_length=3,
                              description="Source asset ids, in order (image_edit takes 1-3).")
    mask: int | None = Field(default=None, description="Mask asset id: white = change.")
    last_frame: int | None = Field(default=None, description="i2v only: a last-frame asset id.")


class JobCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [
            {"kind": "image_edit", "params": {"prompt": "Make the jacket red"},
             "inputs": {"images": [12]}, "request_id": "my-client-000000000001"},
            {"kind": "upscale", "params": {"scale": 2}, "inputs": {"images": [12]}},
        ]},
    )

    kind: str = Field(description="A kind from GET /api/jobs/kinds.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Settings; see the kind's `params` schema. Omitted ones take their defaults.")
    inputs: JobInputs = Field(default_factory=JobInputs)
    dry_run: bool = Field(
        default=False,
        description="Validate and return the params that would be queued, without creating a job.")
    request_id: str | None = Field(
        default=None,
        description="Idempotency key (16-128 of A-Z a-z 0-9 _ -). Resubmitting it returns "
                    "the original jobs instead of queuing new ones.")


class JobSubmission(BaseModel):
    job_id: int = Field(description="The first (usually the only) job.")
    job_ids: list[int] = Field(description="Every job queued; several for a {a|b} fan-out.")
    group_id: str | None = None
    duplicate: bool = Field(
        default=False, description="True when request_id named an earlier submission.")


class DryRunResult(BaseModel):
    """A validated request that was not queued."""

    dry_run: Literal[True] = True
    kind: str
    params: dict[str, Any]
    would_queue: int = Field(description="How many jobs a real submit would create. 0 when "
                                         "request_id already names one.")
    duplicate: bool = False
    job_id: int | None = None
    job_ids: list[int] = Field(default_factory=list)


class KindImages(BaseModel):
    min: int
    max: int
    asset_kind: str


class KindInputs(BaseModel):
    images: KindImages
    mask: Literal["none", "required"]
    last_frame: bool


class JobKindInfo(BaseModel):
    kind: str
    title: str
    description: str = ""
    category: Literal["generator", "tool"]
    output: str
    lane: Literal["local", "remote"]
    available: bool
    unavailable_reason: str | None = None
    inputs: KindInputs
    params: dict[str, Any] = Field(description="JSON Schema for `params`.")


class JobWait(BaseModel):
    job: JobRead
    settled: bool = Field(description="False when the wait timed out first; wait again.")
    assets: list[AssetRead] = Field(
        default_factory=list, description="Outputs, including any saved before a failure.")


# ---- catalogue ------------------------------------------------------------------------------
async def remote_state() -> dict[str, Any]:
    """The Remote GPU's connection state, at most a few seconds old."""
    from .. import remote_gpu_client

    return await remote_gpu_client.remote_gpu_status()


def remote_blocker(state: dict[str, Any] | None) -> str | None:
    """Why remote-lane work cannot run right now, or None when it can.

    Configured is not the same as reachable: a remote kind is only available
    while the worker actually answers, or a job would be queued only to fail.
    """
    if state is None or state.get("connected"):
        return None
    reason = str(state.get("reason") or "no answer")
    if reason == "no url set":
        return ("The Remote GPU is not set up: add its URL and secret in "
                "Settings -> Connections.")
    return (f"The Remote GPU is not connected ({reason}). Start its worker, then check "
            "Settings -> Connections.")


def _catalogue(remote: dict[str, Any] | None = None) -> dict[str, JobKindInfo]:
    """Every kind with its availability. `remote` is the Remote GPU's state;
    without it, remote kinds are judged on configuration alone."""
    from ..variant_sets.operations import registry_specs

    offline = remote_blocker(remote)
    out: dict[str, JobKindInfo] = {}
    for kind, spec in registry_specs().items():
        if kind not in GENERATOR_INPUTS:
            continue
        inputs = inputs_for(kind, spec)
        reason = spec.get("unavailable_reason") or (
            offline if lane_for(kind) == "remote" else None)
        out[kind] = JobKindInfo(
            kind=kind, title=str(spec.get("title") or kind),
            description=str(spec.get("subtitle") or ""), category="generator",
            output=str(spec.get("output") or "image"),
            lane="local" if lane_for(kind) == "local" else "remote",
            available=not reason, unavailable_reason=reason,
            inputs=_inputs_view(inputs),
            params=controls.params_schema(_controls_of(spec)),
        )
    for kind, tool in tools.TOOLS.items():
        reason = tool.unavailable() or (offline if lane_for(kind) == "remote" else None)
        out[kind] = JobKindInfo(
            kind=kind, title=tool.title, description=tool.description, category="tool",
            output=tool.output, lane="local" if lane_for(kind) == "local" else "remote",
            available=not reason, unavailable_reason=reason,
            inputs=_inputs_view(inputs_for(kind)),
            params=controls.params_schema({str(c["name"]): c for c in tool.controls}),
        )
    if (target := out.get("inpaint_remote") or out.get("image_edit")) is not None:
        from ..generators import parsing

        extra: dict[str, dict[str, Any]] = {
            "region": {
                "name": "region", "label": "Region", "type": "select", "default": "",
                "options": ["", *parsing.REGIONS],
            },
            "click_x": {"name": "click_x", "label": "Click X", "type": "number",
                        "default": -1, "min": -1, "max": 16384},
            "click_y": {"name": "click_y", "label": "Click Y", "type": "number",
                        "default": -1, "min": -1, "max": 16384},
        }
        spec = registry_specs().get(target.kind, {})
        known = {**_controls_of(spec), **extra}
        out["refine"] = JobKindInfo(
            kind="refine", title="Refine",
            description="One-sentence edit of a gallery image. Routes to masked "
                        "inpaint or a restyle; each turn is an ordinary job.",
            category="generator", output="image", lane=target.lane,
            available=target.available, unavailable_reason=target.unavailable_reason,
            inputs=_inputs_view(Inputs(1, 1)),
            params=controls.params_schema(known),
        )
    return out


def _controls_of(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(c["name"]): c for c in spec.get("controls") or []}


def _inputs_view(inputs: Inputs) -> KindInputs:
    return KindInputs(
        images=KindImages(min=inputs.min_images, max=inputs.max_images,
                          asset_kind=inputs.image_kind),
        mask=inputs.mask, last_frame=inputs.last_frame)


@router.get("/jobs/kinds")
async def job_kinds() -> list[JobKindInfo]:
    """Every kind `POST /api/jobs` accepts here, with its inputs and settings schema.

    Live, like the UI's own registry: a generator whose device or model is not
    configured is absent, and one that cannot run right now - its local device
    failed its probe, or the Remote GPU is not connected - is listed with
    `available: false` and the reason.
    """
    return list(_catalogue(await remote_state()).values())


# ---- submission -----------------------------------------------------------------------------
def require_input_asset(asset_id: int, kind: str, role: str):
    """The gallery asset `role` names, or a structured refusal.

    Recipes and the job API share this so a frozen asset id is judged the same
    way as one passed to `POST /api/jobs`.
    """
    return _asset(asset_id, kind, role)


def _asset(asset_id: int, kind: str, role: str):
    a = db.get_asset(asset_id)
    if a is None:
        raise RequestError(
            404, f"{role}: asset {asset_id} does not exist or is in the trash",
            code="not_found", field=role,
            suggestions=["Import the image first, then pass its asset id."],
        )
    if a.kind != kind:
        raise RequestError(
            400, f"{role}: asset {asset_id} is a {a.kind}, not a {kind}",
            code="invalid_request", field=role,
            suggestions=[f"Pass a {kind} asset id."],
        )
    if not Path(a.path).exists():
        raise RequestError(
            409, f"{role}: asset {asset_id}'s file is missing from disk",
            code="conflict", field=role,
            suggestions=["Re-import the file; the library row points at nothing."],
        )
    return a


def _resolve(kind: str, spec: Inputs, given: JobInputs) -> tuple[list[str], str | None, str | None]:
    count = len(given.images)
    if not spec.min_images <= count <= spec.max_images:
        want = (f"{spec.min_images}" if spec.min_images == spec.max_images
                else f"{spec.min_images}-{spec.max_images}")
        hint = (f"Pass {want} {spec.image_kind} asset id(s) in inputs.images."
                if spec.min_images else "This kind takes no images; use generate, not edit.")
        raise RequestError(
            400, f"{kind} takes {want} input {spec.image_kind}(s); got {count}",
            code="invalid_request", field="inputs.images",
            suggestions=[hint],
        )
    sources = [_asset(i, spec.image_kind, f"inputs.images[{n}]")
               for n, i in enumerate(given.images)]
    mask_path = None
    if given.mask is not None and spec.mask == "none":
        raise RequestError(
            400, f"{kind} does not take a mask",
            code="invalid_request", field="inputs.mask",
            suggestions=["Omit inputs.mask for this kind."],
        )
    if spec.mask == "required" and given.mask is None:
        raise RequestError(
            400, f"{kind} needs inputs.mask: white = change, black = keep",
            code="invalid_request", field="inputs.mask",
            suggestions=["Add inputs.mask: white marks what should change, same size as the image."],
        )
    if given.mask is not None:
        mask = _asset(given.mask, AssetKind.image.value, "inputs.mask")
        src = sources[0]
        if (mask.width, mask.height) != (src.width, src.height):
            raise RequestError(
                400,
                f"inputs.mask is {mask.width}x{mask.height} but the image is "
                f"{src.width}x{src.height}; a mask must match its image",
                code="invalid_request", field="inputs.mask",
                suggestions=["Use a mask the same width and height as the image."],
            )
        mask_path = mask.path
    last_path = None
    if given.last_frame is not None:
        if not spec.last_frame:
            raise RequestError(
                400, f"{kind} does not take a last frame here",
                code="invalid_request", field="inputs.last_frame",
                suggestions=["Omit inputs.last_frame for this kind."],
            )
        last_path = _asset(given.last_frame, AssetKind.image.value, "inputs.last_frame").path
    return [a.path for a in sources], mask_path, last_path


def _submission(result: dict[str, Any], *, duplicate: bool) -> JobSubmission:
    ids = [int(i) for i in result.get("job_ids") or [result["job_id"]]]
    return JobSubmission(job_id=int(result["job_id"]), job_ids=ids,
                         group_id=result.get("group_id"), duplicate=duplicate)


@router.post("/jobs", responses=error_responses(400, 404, 409, 503))
async def create_job(body: JobCreate) -> JobSubmission | DryRunResult:
    """Queue one generation or tool job from asset ids and JSON settings.

    Settings are validated strictly against the kind's schema: an unknown name or
    a value outside its control's range is refused with a 400 naming it, rather
    than clamped. A `combinatorial` prompt with `{a|b}` choices fans out into one
    job per combination, exactly as the form does.

    The request itself is judged before the server's state: a malformed request
    is a 400 even when the kind is unavailable here, because a 503 would tell the
    client to retry something that can never succeed. `dry_run` runs that same
    check and returns the params that would be stored, without creating a job
    or writing a mask file. It does not ask whether the kind can run right now,
    so a remote kind still validates while its worker is offline.
    """
    try:
        return await _create_job(body)
    except HTTPException as exc:
        raise as_request_error(exc) from None


async def _create_job(body: JobCreate) -> JobSubmission | DryRunResult:
    info = _require_known(body.kind)
    request_id, prior = duplicate_of(body.request_id, dry_run=body.dry_run)
    if prior is not None:
        return prior

    if body.kind in tools.TOOLS:
        tool = tools.TOOLS[body.kind]
        clean = _validated(body.kind, body.params, {str(c["name"]): c for c in tool.controls})
        _resolve(body.kind, inputs_for(body.kind), body.inputs)
        source = db.get_asset(body.inputs.images[0])
        assert source is not None  # _resolve already refused a missing id
        built = tool.build(source, clean)
        if body.dry_run:
            return _dry(body.kind, built, 1)
        await _require_runnable(info)
        result = await tools.submit_tool(body.kind, body.inputs.images[0], clean,
                                         request_id=request_id)
        return _submission(result, duplicate=False)

    if body.kind == "refine":
        return await _create_refine(body, info, request_id)

    from ..variant_sets.operations import registry_specs

    spec = registry_specs()[body.kind]
    clean = _validated(body.kind, body.params, _controls_of(spec))
    paths, mask_path, last_path = _resolve(body.kind, inputs_for(body.kind, spec), body.inputs)
    if body.kind in _LOCAL_GPU:
        images.require_local_gpu()
    raw = {**clean, "request_id": request_id}
    if body.kind in _IMAGE_KINDS:
        params = images.build_params(body.kind, raw, paths, mask_path)
    else:
        params = videos.build_params(body.kind, raw, paths, last_path)
    if body.kind in _MODEL_ACCESS:
        images.require_model_access(params)
    if body.dry_run:
        return _dry(body.kind, params, _would_queue(body.kind, params))
    await _require_runnable(info)
    handler = get_handler(body.kind)
    if handler is None:  # pragma: no cover — every catalogued kind registers one at import
        raise HTTPException(status_code=500, detail=f"no handler for {body.kind}")
    fanned = await images.maybe_fanout(body.kind, params, handler) if body.kind in _FANOUT else None
    return _submission(fanned or await submit(body.kind, params, handler), duplicate=False)


async def require_runnable(kind: str) -> None:
    """503 when ``kind`` cannot run here now.

    A dry run judges the request without asking this. Callers that store
    uploads only after ``dry_run_unsaved`` still ask here before writing.
    """
    await _require_runnable(_require_known(kind))


async def _require_runnable(info: JobKindInfo) -> None:
    """503 when the kind cannot run here now; remote kinds need a live worker."""
    if not info.available:
        remote = info.lane == "remote" or "remote gpu" in (info.unavailable_reason or "").lower()
        raise RequestError(
            503, info.unavailable_reason or "unavailable",
            code="remote_offline" if remote else "unavailable", field="kind",
            suggestions=(["Start the Remote GPU worker, then retry."] if remote else []),
        )
    if info.lane == "remote" and (blocker := remote_blocker(await remote_state())):
        raise RequestError(
            503, blocker, code="remote_offline", field="kind",
            suggestions=["Start the Remote GPU worker, then retry."],
        )


def _validated(kind: str, params: dict[str, Any],
               known: dict[str, dict[str, Any]]) -> dict[str, Any]:
    try:
        return controls.check_params(params, known, subject=kind)
    except controls.ControlError as error:
        raise RequestError(
            400, str(error), code=error.code, field=error.field,
            valid_options=error.valid_options, suggestions=error.suggestions,
        ) from None


def _dry(kind: str, params: dict[str, Any], would_queue: int) -> DryRunResult:
    from ..redaction import redact

    return DryRunResult(kind=kind, params=redact(params), would_queue=would_queue)


def _would_queue(kind: str, params: dict[str, Any]) -> int:
    if kind not in _FANOUT or not params.get("combinatorial"):
        return 1
    from ..prompt_engine import count_variants

    return max(1, count_variants(str(params.get("prompt") or "")))


@dataclass(frozen=True)
class MemoryImage:
    """An edit input that may exist only as decoded pixels."""

    asset_id: int | None
    width: int
    height: int
    image: Any = None


def duplicate_of(
    request_id: str | None, *, dry_run: bool,
) -> tuple[str | None, JobSubmission | DryRunResult | None]:
    """The earlier submission for `request_id`, before any new files are written.

    Callers that import images must ask this first. A retry otherwise stores
    the new bytes and then discovers the key was already used.
    """
    normalized = normalize_request_id(request_id)
    if not normalized:
        return None, None
    existing = db.job_for_request(normalized)
    if existing is None:
        return normalized, None
    submission = _submission(_submission_response(existing), duplicate=True)
    if not dry_run:
        return normalized, submission
    return normalized, DryRunResult(
        kind=existing.kind,
        params=_dry(existing.kind, existing.params, 0).params,
        would_queue=0,
        duplicate=True,
        job_id=submission.job_id,
        job_ids=list(submission.job_ids),
    )


def kind_contract(kind: str) -> tuple[dict[str, dict[str, Any]], Inputs]:
    """Controls and input rules for a kind `POST /api/jobs` accepts.

    Availability is not judged. A recipe can be saved while its device is
    offline; running it still asks whether the kind can run, unless the caller
    set `dry_run`.
    """
    found = _contract(kind)
    if found is None:
        known = sorted(_catalogue())
        raise RequestError(
            400,
            f"unknown or unavailable kind '{kind}' (available: {', '.join(known)})",
            code="kind_unavailable", field="kind", valid_options=known,
            suggestions=close_suggestions(kind, known),
        )
    return found


def _contract(kind: str) -> tuple[dict[str, dict[str, Any]], Inputs] | None:
    if kind in tools.TOOLS:
        tool = tools.TOOLS[kind]
        return {str(c["name"]): c for c in tool.controls}, inputs_for(kind)
    if kind == "refine":
        if "refine" not in _catalogue():
            return None
        return _refine_known(), Inputs(1, 1)
    from ..variant_sets.operations import registry_specs

    specs = registry_specs()
    if kind not in specs or kind not in GENERATOR_INPUTS:
        return None
    spec = specs[kind]
    return _controls_of(spec), inputs_for(kind, spec)


def _require_known(kind: str) -> JobKindInfo:
    catalogue = _catalogue()
    info = catalogue.get(kind)
    if info is None:
        known = sorted(catalogue)
        raise RequestError(
            400,
            f"unknown or unavailable kind '{kind}' (available: {', '.join(known)})",
            code="kind_unavailable", field="kind", valid_options=known,
            suggestions=close_suggestions(kind, known),
        )
    return info


def _image_settings(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Image-job settings with no input files attached."""
    profile = JobKind.image_local.value if kind == JobKind.control_local.value else kind
    params = images.common_params(raw, profile)
    if kind == JobKind.control_local.value:
        return images.control_extras(params, raw)
    if kind == JobKind.outpaint.value:
        return images.outpaint_extras(params, raw)
    return params


def _measure(image: MemoryImage, asset_kind: str, role: str) -> tuple[int, int]:
    if image.asset_id is None:
        return image.width, image.height
    row = _asset(image.asset_id, asset_kind, role)
    return int(row.width or 0), int(row.height or 0)


def _expect_memory(kind: str, spec: Inputs, given: list[MemoryImage],
                   mask: MemoryImage | None) -> None:
    """The same input rules as `_resolve`, for images that were not stored."""
    count = len(given)
    if not spec.min_images <= count <= spec.max_images:
        want = (f"{spec.min_images}" if spec.min_images == spec.max_images
                else f"{spec.min_images}-{spec.max_images}")
        hint = (f"Pass {want} {spec.image_kind} asset id(s) in inputs.images."
                if spec.min_images else "This kind takes no images; use generate, not edit.")
        raise RequestError(
            400, f"{kind} takes {want} input {spec.image_kind}(s); got {count}",
            code="invalid_request", field="inputs.images",
            suggestions=[hint],
        )
    sources = [_measure(image, spec.image_kind, f"inputs.images[{n}]")
               for n, image in enumerate(given)]
    if mask is not None and spec.mask == "none":
        raise RequestError(
            400, f"{kind} does not take a mask",
            code="invalid_request", field="inputs.mask",
            suggestions=["Omit inputs.mask for this kind."],
        )
    if spec.mask == "required" and mask is None:
        raise RequestError(
            400, f"{kind} needs inputs.mask: white = change, black = keep",
            code="invalid_request", field="inputs.mask",
            suggestions=["Add inputs.mask: white marks what should change, same size as the image."],
        )
    if mask is not None:
        mask_size = _measure(mask, AssetKind.image.value, "inputs.mask")
        if mask_size != sources[0]:
            raise RequestError(
                400,
                f"inputs.mask is {mask_size[0]}x{mask_size[1]} but the image is "
                f"{sources[0][0]}x{sources[0][1]}; a mask must match its image",
                code="invalid_request", field="inputs.mask",
                suggestions=["Use a mask the same width and height as the image."],
            )


def _refine_known() -> dict[str, dict[str, Any]]:
    from ..generators import parsing
    from ..variant_sets.operations import registry_specs

    target_kind = (JobKind.inpaint_remote.value
                   if JobKind.inpaint_remote.value in registry_specs()
                   else JobKind.image_edit.value)
    extra: dict[str, dict[str, Any]] = {
        "region": {"name": "region", "type": "select", "default": "",
                   "options": ["", *parsing.REGIONS]},
        "click_x": {"name": "click_x", "type": "number", "default": -1, "min": -1, "max": 16384},
        "click_y": {"name": "click_y", "type": "number", "default": -1, "min": -1, "max": 16384},
    }
    return {**_controls_of(registry_specs()[target_kind]), **extra}


def _plan_refine(source: Any, raw: dict[str, Any], paths: list[str], *,
                 dry_run: bool) -> tuple[str, dict[str, Any]]:
    """Choose inpaint or restyle. A dry run returns before any mask file is written."""
    from ..generators import parsing
    from ..refine import infer_region, is_restyle
    from ..utils.io import save_image
    from ..variant_sets.operations import registry_specs

    prompt = str(raw.get("prompt") or "")
    region = str(raw.get("region") or "").strip()
    click_x = as_int(raw.get("click_x"), -1, -1, 16384)
    click_y = as_int(raw.get("click_y"), -1, -1, 16384)
    if click_x >= 0 and click_y >= 0:
        region = parsing.region_at(source, click_x, click_y)
        raw["region"] = region
    restyle = is_restyle(prompt) and not region
    if restyle and str(raw.get("input_fidelity") or "high") != "high":
        return JobKind.image_edit.value, _refine_params(JobKind.image_edit.value, raw, paths)
    if not region:
        region = infer_region(prompt) or "subject_minus_face"
        raw["region"] = region
    try:
        mask = parsing.mask_for(source, region)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None
    if mask.getbbox() is None:
        raise HTTPException(status_code=400, detail=f"region {region!r} is empty on this image")
    raw.setdefault("input_fidelity", "high")
    inpaint = JobKind.inpaint_remote.value in registry_specs()
    kind = JobKind.inpaint_remote.value if inpaint else JobKind.image_edit.value
    if dry_run:
        params = _refine_params(kind, raw, paths)
        params.pop("mask_path", None)
        if not inpaint:
            params["input_fidelity"] = "high"
        return kind, params
    mask_path = str(save_image(mask.convert("L"), "mask").path)
    if inpaint:
        return kind, images.build_params(kind, raw, paths, mask_path)
    params = images.build_params(kind, raw, paths)
    params["mask_path"] = mask_path
    params["input_fidelity"] = "high"
    return kind, params


def _refine_params(kind: str, raw: dict[str, Any], paths: list[str]) -> dict[str, Any]:
    if not paths:
        return _image_settings(kind, raw)
    return images.build_params(kind, raw, paths)


async def dry_run_unsaved(
    kind: str,
    params: dict[str, Any],
    images_in: list[MemoryImage],
    mask: MemoryImage | None,
    *,
    request_id: str | None = None,
) -> DryRunResult:
    """Validate an edit whose data URLs were checked in memory and not stored.

    Asset ids in the list are still looked up. Nothing here creates a job,
    an asset, or a mask file, and a remote kind does not need its worker.
    """
    _require_known(kind)
    normalized = normalize_request_id(request_id)
    if kind in tools.TOOLS:
        tool = tools.TOOLS[kind]
        clean = _validated(kind, params, {str(c["name"]): c for c in tool.controls})
        _expect_memory(kind, Inputs(1, 1, image_kind=tool.input), images_in, mask)
        stand_in = Asset(
            kind=tool.input, path="", filename="dry-run",
            width=images_in[0].width, height=images_in[0].height,
        )
        built = tool.build(stand_in, clean)
        for key in ("src_path", "src_meta", "source_asset_id"):
            built.pop(key, None)
        return _dry(kind, built, 1)
    if kind == "refine":
        from ..refine import refine_defaults

        clean = _validated("refine", params, _refine_known())
        _expect_memory("refine", Inputs(1, 1), images_in, mask)
        source = images_in[0].image
        if source is None:
            raise RequestError(
                400, "refine needs the image bytes to judge the region",
                code="invalid_request", field="images",
                suggestions=["Pass a data URL, or an asset id of an image already in the gallery."],
            )
        prompt = str(clean.get("prompt") or "")
        raw = refine_defaults(prompt, {**clean, "request_id": normalized})
        if images_in[0].asset_id is not None:
            raw["parent_asset_id"] = images_in[0].asset_id
        planned, built = _plan_refine(source.convert("RGB"), raw, [], dry_run=True)
        return _dry(planned, built, 1)

    from ..variant_sets.operations import registry_specs

    spec = registry_specs().get(kind)
    taken = inputs_for(kind, spec)
    clean = _validated(kind, params, _controls_of(spec or {}))
    _expect_memory(kind, taken, images_in, mask)
    raw = {**clean, "request_id": normalized}
    if kind in _LOCAL_GPU:
        images.require_local_gpu()
    if kind in _IMAGE_KINDS:
        built = _image_settings(kind, raw)
    elif kind == JobKind.i2v.value:
        built = videos.build_params(kind, raw, ["."])
        built.pop("image_path", None)
        built.pop("last_image_path", None)
    elif kind in _VIDEO_KINDS:
        built = videos.build_params(kind, raw)
    else:
        raise RequestError(
            400, f"{kind} cannot be checked from an image that is not in the gallery",
            code="invalid_request", field="kind",
            suggestions=["Import the file, then pass its asset id."],
        )
    if kind in _MODEL_ACCESS:
        images.require_model_access(built)
    return _dry(kind, built, _would_queue(kind, built))


async def _create_refine(body: JobCreate, info: JobKindInfo,
                         request_id: str | None) -> JobSubmission | DryRunResult:
    """Facade: one sentence becomes an ordinary inpaint_remote or image_edit job."""
    from PIL import Image as PILImage

    from ..refine import refine_defaults

    clean = _validated("refine", body.params, _refine_known())
    paths, _mask, _last = _resolve("refine", Inputs(1, 1), body.inputs)
    prompt = str(clean.get("prompt") or "")
    raw = refine_defaults(prompt, {**clean, "request_id": request_id})
    raw["parent_asset_id"] = body.inputs.images[0]
    with PILImage.open(paths[0]) as opened:
        opened.load()
        source = opened.convert("RGB")
    kind, params = _plan_refine(source, raw, paths, dry_run=body.dry_run)
    if body.dry_run:
        return _dry(kind, params, 1)
    await _require_runnable(info)
    handler = get_handler(kind)
    if handler is None:
        raise HTTPException(status_code=500, detail=f"no handler for {kind}")
    return _submission(await submit(kind, params, handler), duplicate=False)


# ---- waiting --------------------------------------------------------------------------------
def _view(job) -> JobWait:
    ids = db.asset_ids_for_job(int(job.id))
    found = {int(a.id): a for a in db.get_assets(ids) if a.id is not None}
    return JobWait(job=JobRead.of(job), settled=job.status in TERMINAL,
                   assets=[AssetRead.of(found[i]) for i in ids if i in found])


@router.get("/jobs/{job_id}/wait", responses=error_responses(404))
async def wait_job(
    job_id: int,
    timeout: float = Query(30.0, ge=0.0, le=MAX_WAIT_SECONDS,
                           description="Seconds to wait for the job to settle."),
) -> JobWait:
    """Long-poll: return once the job is done, failed or canceled, or at `timeout`.

    Always answers within `timeout` (60 s at most, so a proxy's idle limit is
    never the thing that ends the request). `settled: false` means wait again.
    """
    try:
        return await _wait_job(job_id, timeout)
    except HTTPException as exc:
        raise as_request_error(exc) from None


async def _wait_job(job_id: int, timeout: float) -> JobWait:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        job = db.get_job(job_id)
        if job is None:
            raise RequestError(
                404, "job not found", code="not_found", field="job_id",
                suggestions=["Check the job id, or submit the job again."],
            )
        if job.status in TERMINAL or loop.time() >= deadline:
            return _view(job)
        await asyncio.sleep(min(_POLL_SECONDS, max(0.0, deadline - loop.time())))
