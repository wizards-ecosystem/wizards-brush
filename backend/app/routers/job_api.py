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
from ..models import AssetKind, AssetRead, JobKind, JobRead, JobStatus
from ..queue import lane_for
from . import images, tools, videos
from .common import (
    _submission_response,
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
    JobKind.inpaint.value, JobKind.outpaint.value, JobKind.image_edit.value,
    JobKind.control_local.value,
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
def _asset(asset_id: int, kind: str, role: str):
    a = db.get_asset(asset_id)
    if a is None:
        raise HTTPException(status_code=404,
                            detail=f"{role}: asset {asset_id} does not exist or is in the trash")
    if a.kind != kind:
        raise HTTPException(status_code=400,
                            detail=f"{role}: asset {asset_id} is a {a.kind}, not a {kind}")
    if not Path(a.path).exists():
        raise HTTPException(status_code=409,
                            detail=f"{role}: asset {asset_id}'s file is missing from disk")
    return a


def _resolve(kind: str, spec: Inputs, given: JobInputs) -> tuple[list[str], str | None, str | None]:
    count = len(given.images)
    if not spec.min_images <= count <= spec.max_images:
        want = (f"{spec.min_images}" if spec.min_images == spec.max_images
                else f"{spec.min_images}-{spec.max_images}")
        raise HTTPException(status_code=400,
                            detail=f"{kind} takes {want} input {spec.image_kind}(s); got {count}")
    sources = [_asset(i, spec.image_kind, f"inputs.images[{n}]")
               for n, i in enumerate(given.images)]
    mask_path = None
    if given.mask is not None and spec.mask == "none":
        raise HTTPException(status_code=400, detail=f"{kind} does not take a mask")
    if spec.mask == "required" and given.mask is None:
        raise HTTPException(status_code=400,
                            detail=f"{kind} needs inputs.mask: white = change, black = keep")
    if given.mask is not None:
        mask = _asset(given.mask, AssetKind.image.value, "inputs.mask")
        src = sources[0]
        if (mask.width, mask.height) != (src.width, src.height):
            raise HTTPException(
                status_code=400,
                detail=f"inputs.mask is {mask.width}x{mask.height} but the image is "
                       f"{src.width}x{src.height}; a mask must match its image")
        mask_path = mask.path
    last_path = None
    if given.last_frame is not None:
        if not spec.last_frame:
            raise HTTPException(status_code=400,
                                detail=f"{kind} does not take a last frame here")
        last_path = _asset(given.last_frame, AssetKind.image.value, "inputs.last_frame").path
    return [a.path for a in sources], mask_path, last_path


def _submission(result: dict[str, Any], *, duplicate: bool) -> JobSubmission:
    ids = [int(i) for i in result.get("job_ids") or [result["job_id"]]]
    return JobSubmission(job_id=int(result["job_id"]), job_ids=ids,
                         group_id=result.get("group_id"), duplicate=duplicate)


@router.post("/jobs", responses=error_responses(400, 404, 409, 503))
async def create_job(body: JobCreate) -> JobSubmission:
    """Queue one generation or tool job from asset ids and JSON settings.

    Settings are validated strictly against the kind's schema: an unknown name or
    a value outside its control's range is refused with a 400 naming it, rather
    than clamped. A `combinatorial` prompt with `{a|b}` choices fans out into one
    job per combination, exactly as the form does.

    The request itself is judged before the server's state: a malformed request
    is a 400 even when the kind is unavailable here, because a 503 would tell the
    client to retry something that can never succeed.
    """
    catalogue = _catalogue()
    info = catalogue.get(body.kind)
    if info is None:
        known = ", ".join(sorted(catalogue))
        raise HTTPException(status_code=400,
                            detail=f"unknown or unavailable kind '{body.kind}' (available: {known})")
    request_id = normalize_request_id(body.request_id)
    if request_id and (existing := db.job_for_request(request_id)) is not None:
        return _submission(_submission_response(existing), duplicate=True)

    if body.kind in tools.TOOLS:
        tool = tools.TOOLS[body.kind]
        clean = _validated(body.kind, body.params, {str(c["name"]): c for c in tool.controls})
        _resolve(body.kind, inputs_for(body.kind), body.inputs)
        await _require_runnable(info)
        result = await tools.submit_tool(body.kind, body.inputs.images[0], clean,
                                         request_id=request_id)
        return _submission(result, duplicate=False)

    from ..variant_sets.operations import registry_specs

    spec = registry_specs()[body.kind]
    clean = _validated(body.kind, body.params, _controls_of(spec))
    paths, mask_path, last_path = _resolve(body.kind, inputs_for(body.kind, spec), body.inputs)
    await _require_runnable(info)
    if body.kind in _LOCAL_GPU:
        images.require_local_gpu()
    raw = {**clean, "request_id": request_id}
    if body.kind in _IMAGE_KINDS:
        params = images.build_params(body.kind, raw, paths, mask_path)
    else:
        params = videos.build_params(body.kind, raw, paths, last_path)
    if body.kind in _MODEL_ACCESS:
        images.require_model_access(params)
    handler = get_handler(body.kind)
    if handler is None:  # pragma: no cover — every catalogued kind registers one at import
        raise HTTPException(status_code=500, detail=f"no handler for {body.kind}")
    fanned = await images.maybe_fanout(body.kind, params, handler) if body.kind in _FANOUT else None
    return _submission(fanned or await submit(body.kind, params, handler), duplicate=False)


async def _require_runnable(info: JobKindInfo) -> None:
    """503 when the kind cannot run here now; remote kinds need a live worker."""
    if not info.available:
        raise HTTPException(status_code=503, detail=info.unavailable_reason)
    if info.lane == "remote" and (blocker := remote_blocker(await remote_state())):
        raise HTTPException(status_code=503, detail=blocker)


def _validated(kind: str, params: dict[str, Any],
               known: dict[str, dict[str, Any]]) -> dict[str, Any]:
    try:
        return controls.check_params(params, known, subject=kind)
    except controls.ControlError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


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
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.status in TERMINAL or loop.time() >= deadline:
            return _view(job)
        await asyncio.sleep(min(_POLL_SECONDS, max(0.0, deadline - loop.time())))
