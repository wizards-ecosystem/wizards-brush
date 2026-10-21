"""OpenAI-compatible image API. A thin map onto the job API.

``GET /models``, ``POST /images/generations`` and ``POST /images/edits`` call
``core`` only. Settings are validated by ``POST /api/jobs``, and refusals use
the same ``code`` / ``field`` / ``valid_options`` / ``suggestions`` as every
other agent surface. This module only changes the envelope.

Model ids
---------
Friendly ids are what ``GET /models`` lists. Raw ids are also accepted.

| Catalogue row | Friendly id | Raw id |
| --- | --- | --- |
| local / turbo | brush-fast | local/turbo |
| local / <name> | brush-<name> | local/<name> |
| colab / quality | brush-remote | remote/quality |
| colab / <name> | brush-<name> | remote/<name> |

``local/turbo`` is ``brush-fast`` because that is the name a caller expects
for the distilled default. Remote ``quality`` is ``brush-remote`` so it does
not collide with local ``brush-quality``. ``colab/<name>`` is accepted as an
alias of ``remote/<name>`` (that is the catalogue's lane key) and is not listed.

Only variants whose repo is configured are offered. An unknown or unconfigured
id is a 400, not a silent fallback onto another model.

There is no bare ``/v1``. Host, token, Origin and Fetch-Metadata checks apply
to paths under ``/api``. A second prefix would be a second copy of that
boundary.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from .. import client_tag, log
from ..config import settings
from ..generators.variants import Variant, available
from ..problems import RequestError, as_request_error, close_suggestions
from ..redaction import redact
from ..routers import job_api
from ..routers.job_api import DryRunResult, JobCreate, JobInputs, JobSubmission, JobWait, MemoryImage
from . import core

logger = log.get("openai")

router = APIRouter(prefix="/openai/v1", tags=["openai"])

# The OpenAI SDK's ImagesResponse.size literal. Any other string makes the
# official client throw away a response that already holds the image. Delivered
# pixels that fall outside this set are reported on ``brush.size`` instead.
_SDK_SIZES = frozenset({"1024x1024", "1024x1536", "1536x1024"})
_RATIOS = frozenset({"1:1", "3:4", "4:3", "2:3", "3:2", "9:16", "16:9"})
_SIZE_RE = re.compile(r"^(\d+)\s*x\s*(\d+)$", re.IGNORECASE)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_WAIT_SLICE = 60.0

# Accepted and ignored. ``user`` is an end-user label, not a generation setting,
# so it is dropped without a warning. The others would change the image on
# OpenAI's API and must be visible as no-ops.
_WARN_FIELDS = frozenset({
    "quality", "style", "moderation", "input_fidelity", "output_compression",
})
_SILENT_FIELDS = frozenset({"user"})
_GENERATE_FIELDS = frozenset({
    "prompt", "model", "n", "size", "response_format", "output_format", "background",
    "stream", "partial_images",
}) | _WARN_FIELDS | _SILENT_FIELDS
_EDIT_FIELDS = _GENERATE_FIELDS | {"image", "images", "mask"}

_LANES = {"local": "local", "remote": "colab", "colab": "colab"}


def friendly_id(variant: Variant) -> str:
    """The id ``GET /models`` publishes for this catalogue row."""
    if variant.lane == "local" and variant.name == "turbo":
        return "brush-fast"
    if variant.lane == "colab" and variant.name == "quality":
        return "brush-remote"
    return f"brush-{variant.name}"


def raw_id(variant: Variant) -> str:
    """``local/<name>`` or ``remote/<name>``."""
    lane = "remote" if variant.lane == "colab" else variant.lane
    return f"{lane}/{variant.name}"


def _ids_unique() -> None:
    from ..generators import variants

    friendly = [friendly_id(v) for v in variants.VARIANTS]
    raw = [raw_id(v) for v in variants.VARIANTS]
    if len(friendly) != len(set(friendly)) or len(raw) != len(set(raw)):
        raise RuntimeError("OpenAI model ids collide")


_ids_unique()


def _configured() -> list[Variant]:
    return [*available("local"), *available("colab")]


def _lookup(lane: str, name: str) -> Variant | None:
    from ..generators import variants

    internal = _LANES.get(lane)
    if internal is None:
        return None
    for variant in variants.VARIANTS:
        if variant.lane == internal and (variant.name == name or name in variant.aliases):
            return variant
    return None


def resolve_model(model: str) -> Variant:
    """The configured variant for a friendly or raw id. Unknown ids are refused."""
    text = str(model or "").strip()
    found: Variant | None = None
    if text:
        found = next((v for v in _configured() if friendly_id(v) == text), None)
        if found is None and "/" in text:
            lane, _, name = text.partition("/")
            candidate = _lookup(lane, name)
            if candidate is not None and candidate in _configured():
                found = candidate
            elif candidate is not None:
                found = None  # known row, repo not configured
    if found is None:
        known = [friendly_id(v) for v in _configured()]
        known += [raw_id(v) for v in _configured()]
        hint = close_suggestions(text, known)
        detail = f"unknown model '{text}'"
        if known:
            detail += f" (available: {', '.join(known)})"
        raise RequestError(
            400, detail, code="invalid_option", field="model", valid_options=known,
            suggestions=hint or ["GET /api/openai/v1/models lists the ids this install can run."],
        )
    return found


def model_object(variant: Variant) -> dict[str, Any]:
    """One ``GET /models`` entry. ``description`` is the catalogue note."""
    return {
        "id": friendly_id(variant),
        "object": "model",
        "created": 0,
        "owned_by": "brush",
        "description": variant.note,
        "brush": {
            "lane": "remote" if variant.lane == "colab" else "local",
            "variant": variant.name,
            "raw_id": raw_id(variant),
        },
    }


def request_id_for_key(key: str | None) -> str | None:
    """An ``Idempotency-Key`` as a job ``request_id``.

    Keys that already match the 16–128 ``[A-Za-z0-9_-]`` rule are kept. Anything
    else becomes the 64-character sha256 hex of the stripped key, which fits
    the rule. Empty means no key.
    """
    value = str(key or "").strip()
    if not value:
        return None
    if _REQUEST_ID.fullmatch(value):
        return value
    return hashlib.sha256(value.encode()).hexdigest()


def client_label() -> str:
    """``openai``, or the API token's name when one is configured."""
    raw = settings.api_token_name.strip()
    named = client_tag.token_name()
    if raw and named == raw:
        return named
    return "openai"


@dataclass(frozen=True)
class _Parsed:
    model: str
    prompt: str
    n: int | None
    size: str | None
    response_format: str
    background: str | None
    warnings: tuple[str, ...]
    images: tuple[bytes | str, ...] = ()
    mask: bytes | str | None = None


def _error_type(status: int) -> str:
    if status == 504:
        return "timeout"
    if status >= 500:
        return "server_error"
    if status == 401:
        return "authentication_error"
    return "invalid_request_error"


def _openai_param(field: str | None) -> str | None:
    """OpenAI's names for the same inputs the job API calls ``inputs.images``."""
    if not field:
        return None
    if field == "inputs.mask" or field.endswith(".mask"):
        return "mask"
    if field == "images" or field.startswith("inputs.images"):
        return "image"
    return field


def error_response(exc: HTTPException) -> JSONResponse:
    """The OpenAI envelope, filled from the shared agent error."""
    structured = as_request_error(exc)
    body: dict[str, Any] = {
        "message": str(structured.detail),
        "type": _error_type(structured.status_code),
        "param": _openai_param(structured.field),
        "code": structured.code,
    }
    return JSONResponse({"error": body}, status_code=structured.status_code)


def _reject(
    status: int, detail: str, *, code: str, field: str | None = None,
    suggestions: list[str] | None = None, valid_options: list[Any] | None = None,
) -> NoReturn:
    raise RequestError(
        status, detail, code=code, field=field, suggestions=suggestions, valid_options=valid_options,
    )


def _unknown(body: dict[str, Any], allowed: frozenset[str]) -> None:
    extra = sorted(set(body) - allowed)
    if not extra:
        return
    _reject(
        400, f"unsupported parameter '{extra[0]}'",
        code="unsupported_parameter", field=extra[0],
        suggestions=["See docs/agents.md for the image fields this server accepts."],
    )


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _as_int(value: object, field: str) -> int:
    if isinstance(value, bool | float):
        _reject(400, f"{field} must be an integer", code="invalid_type", field=field)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    _reject(400, f"{field} must be an integer", code="invalid_type", field=field)


def _check_stream(body: dict[str, Any]) -> None:
    if "stream" in body and body["stream"] is not None and _as_bool(body["stream"]):
        _reject(
            400, "stream is not supported",
            code="unsupported_parameter", field="stream",
            suggestions=["Wait for the finished image. The job id is returned if that times out."],
        )
    partial = body.get("partial_images")
    if partial is None:
        return
    if _as_int(partial, "partial_images") > 0:
        _reject(
            400, "partial_images is not supported",
            code="unsupported_parameter", field="partial_images",
            suggestions=["Omit partial_images. This server returns the finished image."],
        )


def _warnings_for(body: dict[str, Any]) -> tuple[str, ...]:
    present = [name for name in sorted(_WARN_FIELDS) if name in body and body[name] not in (None, "")]
    if not present:
        return ()
    return (f"ignored {', '.join(present)}",)


def _require_text(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if isinstance(value, str) and value.strip():
        return value
    _reject(400, f"{field} is required", code="invalid_request", field=field)


def _model_text(body: dict[str, Any]) -> str:
    """``brush-fast`` when ``model`` is left out. A present non-string is refused."""
    if "model" not in body or body["model"] is None or body["model"] == "":
        return "brush-fast"
    value = body["model"]
    if isinstance(value, str) and value.strip():
        return value.strip()
    _reject(400, "model must be a string", code="invalid_type", field="model")


def _optional_text(body: dict[str, Any], field: str) -> str | None:
    if field not in body or body[field] is None:
        return None
    value = body[field]
    if isinstance(value, str):
        return value.strip()
    _reject(400, f"{field} must be a string", code="invalid_type", field=field)


def _parse_n(body: dict[str, Any]) -> int | None:
    if "n" not in body or body["n"] is None or body["n"] == "":
        return None
    n = _as_int(body["n"], "n")
    if not 1 <= n <= 8:
        _reject(
            400, "n must be between 1 and 8", code="out_of_range", field="n",
            suggestions=["Use a batch from 1 to 8."],
        )
    return n


def _parse_size(body: dict[str, Any]) -> str | None:
    raw = _optional_text(body, "size")
    if raw is None or raw.lower() == "auto":
        return None
    if raw in _RATIOS or _SIZE_RE.fullmatch(raw):
        return raw
    shown = ", ".join(sorted(_RATIOS))
    _reject(
        400, f"size must be auto, one of {shown}, or WxH such as 1536x1024",
        code="invalid_request", field="size",
        valid_options=["auto", *sorted(_RATIOS), "WxH"],
    )


def _parse_format(body: dict[str, Any]) -> str:
    raw = _optional_text(body, "response_format")
    if raw is None or raw == "b64_json":
        return "b64_json"
    if raw == "url":
        return "url"
    _reject(
        400, "response_format must be b64_json or url",
        code="unsupported_parameter", field="response_format",
        valid_options=["b64_json", "url"],
    )


def _parse_output_format(body: dict[str, Any]) -> None:
    raw = _optional_text(body, "output_format")
    if raw is None or raw.lower() == "png":
        return
    _reject(
        400, "only png output is supported",
        code="unsupported_parameter", field="output_format",
        valid_options=["png"],
        suggestions=["Omit output_format, or set it to png."],
    )


def _parse_background(body: dict[str, Any]) -> str | None:
    raw = _optional_text(body, "background")
    if raw is None or raw.lower() in {"auto", "opaque"}:
        return None
    if raw.lower() == "transparent":
        return "transparent"
    _reject(
        400, "background must be transparent, opaque, or auto",
        code="unsupported_parameter", field="background",
        valid_options=["transparent", "opaque", "auto"],
    )


def _parse_common(body: dict[str, Any], allowed: frozenset[str]) -> _Parsed:
    _unknown(body, allowed)
    _check_stream(body)
    _parse_output_format(body)
    return _Parsed(
        model=_model_text(body),
        prompt=_require_text(body, "prompt"),
        n=_parse_n(body),
        size=_parse_size(body),
        response_format=_parse_format(body),
        background=_parse_background(body),
        warnings=_warnings_for(body),
    )


def _json_image(value: object, field: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        url = value.get("image_url", value.get("url"))
        if isinstance(url, dict):
            url = url.get("url")
        if isinstance(url, str):
            return url
    _reject(
        400, f"{field} must be a data URL",
        code="invalid_request", field=field,
        suggestions=["Pass a data:image/...;base64 URL. http URLs are not accepted."],
    )


def _json_images(body: dict[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    if "images" in body and body["images"] is not None:
        images = body["images"]
        if not isinstance(images, list) or not images:
            _reject(400, "images must be a non-empty list of data URLs",
                    code="invalid_request", field="image")
        for item in images:
            found.append(_json_image(item, "image"))
    if "image" in body and body["image"] is not None:
        image = body["image"]
        if isinstance(image, list):
            if not image:
                _reject(400, "image must contain a data URL", code="invalid_request", field="image")
            for item in image:
                found.append(_json_image(item, "image"))
        else:
            found.append(_json_image(image, "image"))
    if not found:
        _reject(
            400, "image is required", code="invalid_request", field="image",
            suggestions=["Send multipart image files, or JSON data URLs. http URLs are refused."],
        )
    return tuple(found)


def _json_mask(body: dict[str, Any]) -> str | None:
    if "mask" not in body or body["mask"] is None:
        return None
    return _json_image(body["mask"], "mask")


async def _json_object(request: Request) -> dict[str, Any]:
    import json

    try:
        body = await request.json()
    except UnicodeDecodeError:
        _reject(400, "request body is not valid UTF-8", code="invalid_request")
    except json.JSONDecodeError:
        _reject(400, "request body is not valid JSON", code="invalid_request")
    if not isinstance(body, dict):
        _reject(400, "request body must be a JSON object", code="invalid_request")
    return body


def _form_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, UploadFile):
        _reject(400, f"{field} must be text", code="invalid_type", field=field)
    return str(value)


def _file_keys(form: Any, prefix: str) -> list[str]:
    return [key for key in form if key == prefix or key.startswith(prefix + "[")]


async def _form_files(form: Any, prefix: str, *, field: str) -> list[bytes]:
    blobs: list[bytes] = []
    for name in _file_keys(form, prefix):
        for item in form.getlist(name):
            if isinstance(item, str):
                try:
                    blobs.append(core.decode_data_url(item))
                except RequestError as exc:
                    _reject(
                        exc.status_code, str(exc.detail), code=exc.code, field=field,
                        suggestions=exc.suggestions, valid_options=exc.valid_options,
                    )
            elif isinstance(item, UploadFile):
                blobs.append(await item.read())
            else:
                _reject(400, f"{field} must be a file", code="invalid_type", field=field)
    return blobs


async def _edits_form(request: Request) -> _Parsed:
    try:
        form = await request.form()
    except Exception as exc:
        raise RequestError(
            400, "could not read the multipart body", code="invalid_request",
        ) from exc
    body: dict[str, Any] = {}
    for key in form:
        if key in _file_keys(form, "image") or key in _file_keys(form, "mask"):
            continue
        body[key] = _form_text(form.get(key), key)
    parsed = _parse_common(body, _EDIT_FIELDS)
    images = await _form_files(form, "image", field="image")
    if not images:
        _reject(
            400, "image is required", code="invalid_request", field="image",
            suggestions=["Send one or more image files. http URLs are refused."],
        )
    mask_blobs = await _form_files(form, "mask", field="mask")
    if len(mask_blobs) > 1:
        _reject(400, "send one mask", code="invalid_request", field="mask")
    return _Parsed(
        model=parsed.model, prompt=parsed.prompt, n=parsed.n, size=parsed.size,
        response_format=parsed.response_format, background=parsed.background,
        warnings=parsed.warnings, images=tuple(images),
        mask=mask_blobs[0] if mask_blobs else None,
    )


def _controls(kind: str) -> dict[str, dict[str, Any]] | None:
    from ..variant_sets.operations import registry_specs

    spec = registry_specs().get(kind)
    if spec is None:
        return None
    return {str(control["name"]): control for control in spec.get("controls") or []}


def _size_params(size: str | None, controls: dict[str, dict[str, Any]],
                 warnings: list[str]) -> dict[str, Any]:
    if size is None:
        return {}
    if size in _RATIOS:
        parsed: dict[str, Any] = {"aspect": size}
    else:
        match = _SIZE_RE.fullmatch(size)
        assert match is not None
        width, height = int(match.group(1)), int(match.group(2))
        if not 16 <= width <= 8192 or not 16 <= height <= 8192:
            _reject(400, "size must be between 16 and 8192 on each side",
                    code="out_of_range", field="size")
        parsed = {"aspect": "Custom", "width": width, "height": height}
    if "aspect" not in controls or ("width" in parsed and "width" not in controls):
        warnings.append("size is ignored; this edit follows the source image")
        return {}
    return parsed


def _variant_params(variant: Variant, controls: dict[str, dict[str, Any]],
                    warnings: list[str]) -> dict[str, Any]:
    if "model_variant" not in controls:
        from ..generators import variants

        if variant.name != variants.default(variant.lane).name:
            warnings.append(
                f"{friendly_id(variant)} selects the "
                f"{'remote' if variant.lane == 'colab' else 'local'} lane; "
                "this edit uses that lane's edit model",
            )
        return {}
    options = [str(option) for option in controls["model_variant"].get("options") or []]
    if variant.name not in options:
        _reject(
            400, f"model variant '{variant.name}' is not available",
            code="invalid_option", field="model", valid_options=options,
            suggestions=close_suggestions(variant.name, options),
        )
    return {"model_variant": variant.name}


def _count_params(n: int | None, controls: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if n is None:
        return {}
    if "batch" not in controls:
        if n != 1:
            _reject(
                400, "n must be 1 for this model",
                code="unsupported_parameter", field="n",
                suggestions=["This edit returns one image. Omit n or set it to 1."],
            )
        return {}
    return {"batch": n}


def _background_params(background: str | None, controls: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if background != "transparent":
        return {}
    if "background" in controls:
        return {"background": "transparent"}
    if "finish_steps" in controls:
        return {"finish_steps": [{"processor": "background_removal"}]}
    _reject(
        400, "this model cannot return a transparent background",
        code="unsupported_parameter", field="background",
    )


def _job_params(parsed: _Parsed, variant: Variant, kind: str) -> tuple[dict[str, Any], tuple[str, ...]]:
    controls = _controls(kind)
    if controls is None:
        return {"prompt": parsed.prompt}, parsed.warnings
    warnings = list(parsed.warnings)
    params: dict[str, Any] = {
        "prompt": parsed.prompt,
        **_variant_params(variant, controls, warnings),
        **_count_params(parsed.n, controls),
        **_size_params(parsed.size, controls, warnings),
        **_background_params(parsed.background, controls),
    }
    return params, tuple(warnings)


def _kind(variant: Variant, *, mask: bool, edit: bool) -> str:
    if not edit:
        return "image_local" if variant.lane == "local" else "image_colab"
    if variant.lane == "local":
        return "inpaint" if mask else "img2img"
    return "inpaint_remote" if mask else "image_edit"


class _TimedOut(Exception):
    def __init__(self, job_id: int, budget: float) -> None:
        self.job_id = job_id
        self.budget = budget


def _timeout_response(exc: _TimedOut) -> JSONResponse:
    seconds = int(exc.budget) if exc.budget == int(exc.budget) else exc.budget
    return JSONResponse(
        {"error": {
            "message": f"timed out after {seconds}s waiting for job {exc.job_id}",
            "type": "timeout",
            "param": None,
            "code": "timeout",
            "job_id": exc.job_id,
        }},
        status_code=504,
    )


async def _wait_one(job_id: int, deadline: float) -> JobWait:
    loop = asyncio.get_running_loop()
    view: JobWait | None = None
    while True:
        remaining = deadline - loop.time()
        if view is not None and remaining <= 0:
            return view
        view = await core.wait(job_id, min(_WAIT_SLICE, max(0.0, remaining)))
        if view.settled or loop.time() >= deadline:
            return view


async def _wait_all(submission: JobSubmission) -> list[JobWait]:
    budget = float(settings.openai_compat_timeout)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, budget)
    views: list[JobWait] = []
    for job_id in submission.job_ids:
        view = await _wait_one(job_id, deadline)
        if not view.settled:
            raise _TimedOut(job_id, budget)
        _ensure_done(view)
        views.append(view)
    return views


def _ensure_done(view: JobWait) -> None:
    status = view.job.status
    if status == "done":
        if view.assets:
            return
        _reject(500, "the job finished without an image", code="error")
    text = (view.job.tip or view.job.message or view.job.error or status).split("\n", 1)[0]
    if status == "canceled":
        _reject(409, text or "canceled", code="conflict")
    _reject(500, text or "the job failed", code="error")


def _prepare_value(value: bytes | str) -> str:
    """Bytes from a multipart upload, as the data URL ``prepare_image`` already accepts."""
    if isinstance(value, str):
        return value
    return "data:image/png;base64," + base64.b64encode(value).decode("ascii")


async def _prepare_one(
    value: bytes | str, *, role: Literal["source", "mask"], field: str,
) -> core.PreparedImage:
    """Decode an edit input in memory. Nothing is written and captioning does not start."""
    try:
        return await core.prepare_image(_prepare_value(value), role=role, persist=False)
    except RequestError as exc:
        raise RequestError(
            exc.status_code, str(exc.detail), code=exc.code, field=field,
            valid_options=exc.valid_options, suggestions=exc.suggestions,
        ) from None
    except HTTPException as exc:
        structured = as_request_error(exc)
        raise RequestError(
            structured.status_code, str(structured.detail), code=structured.code, field=field,
            valid_options=structured.valid_options, suggestions=structured.suggestions,
        ) from None


def _memory(item: core.PreparedImage) -> MemoryImage:
    return MemoryImage(item.asset_id, item.width, item.height, item.image)


async def _import_one(value: bytes | str, *, role: Literal["source", "mask"], field: str) -> int:
    try:
        if isinstance(value, bytes):
            filename = "openai-mask.png" if role == "mask" else "openai.png"
            imported = await core.import_image(value, role=role, filename=filename)
            return int(imported.id)
        return await core.image_id(value, role=role)
    except RequestError as exc:
        raise RequestError(
            exc.status_code, str(exc.detail), code=exc.code, field=field,
            valid_options=exc.valid_options, suggestions=exc.suggestions,
        ) from None
    except HTTPException as exc:
        structured = as_request_error(exc)
        raise RequestError(
            structured.status_code, str(structured.detail), code=structured.code, field=field,
            valid_options=structured.valid_options, suggestions=structured.suggestions,
        ) from None


async def _submit(kind: str, params: dict[str, Any], *, images: list[int], mask: int | None,
                  request_id: str | None) -> JobSubmission:
    body = JobCreate(
        kind=kind,
        params=params,
        inputs=JobInputs(images=images, mask=mask),
        request_id=request_id,
    )
    with client_tag.use(client_label()):
        result = await core.submit(body)
    if isinstance(result, DryRunResult):
        _reject(500, "dry run was not requested", code="error")
    assert isinstance(result, JobSubmission)
    logger.info("queued %s as %s job %s", kind, params.get("model_variant") or kind, result.job_id)
    return result


def _prior(request_id: str | None) -> JobSubmission | None:
    _, prior = job_api.duplicate_of(request_id, dry_run=False)
    if isinstance(prior, JobSubmission):
        return prior
    return None


async def _b64(asset_id: int) -> str:
    from .. import db

    row = db.get_asset(asset_id)
    if row is None:
        _reject(500, "output file is missing", code="error")
    path = row.path

    def read() -> bytes:
        from pathlib import Path

        file = Path(path)
        if not file.is_file():
            raise FileNotFoundError
        return file.read_bytes()

    try:
        data = await asyncio.to_thread(read)
    except FileNotFoundError:
        _reject(500, "output file is missing", code="error")
    return base64.b64encode(data).decode("ascii")


def _file_url(request: Request, asset_id: int) -> str:
    return f"{str(request.base_url).rstrip('/')}/api/assets/{asset_id}/file"


def _response_size(requested: str | None, delivered: str | None) -> str | None:
    if requested in _SDK_SIZES:
        return requested
    if delivered in _SDK_SIZES:
        return delivered
    return None


async def _image_response(
    request: Request,
    parsed: _Parsed,
    submission: JobSubmission,
    views: list[JobWait],
    warnings: tuple[str, ...],
) -> JSONResponse:
    assets = [asset for view in views for asset in view.assets]
    data: list[dict[str, str]] = []
    for asset in assets:
        if parsed.response_format == "url":
            data.append({"url": _file_url(request, int(asset.id))})
        else:
            data.append({"b64_json": await _b64(int(asset.id))})
    delivered = None
    if assets and assets[0].width and assets[0].height:
        delivered = f"{assets[0].width}x{assets[0].height}"
    body: dict[str, Any] = {
        "created": int(time.time()),
        "data": data,
        "output_format": "png",
        "brush": {
            "job_ids": list(submission.job_ids),
            "asset_ids": [int(asset.id) for asset in assets],
            "meta": [redact(asset.meta) for asset in assets],
            "size": delivered,
        },
    }
    size = _response_size(parsed.size, delivered)
    if size is not None:
        body["size"] = size
    return _with_warnings(JSONResponse(body), warnings)


def _with_warnings(response: JSONResponse, warnings: tuple[str, ...]) -> JSONResponse:
    if warnings:
        response.headers["X-Brush-Warning"] = "; ".join(warnings)
    return response


async def _run(request: Request, parsed: _Parsed, *, edit: bool) -> JSONResponse:
    variant = resolve_model(parsed.model)
    request_id = request_id_for_key(request.headers.get("idempotency-key"))
    prior = _prior(request_id)
    kind = _kind(variant, mask=parsed.mask is not None, edit=edit)
    warnings = parsed.warnings
    try:
        if prior is None:
            params, warnings = _job_params(parsed, variant, kind)
            images: list[int] = []
            mask: int | None = None
            if edit:
                # Judge the bytes before they become gallery assets. A mismatch,
                # an extra image, or an offline worker must not leave uploads
                # behind or start captioning. ``dry_run_unsaved`` does not ask
                # whether the worker is up, so that 503 is a separate check,
                # still before the import.
                prepared = [
                    await _prepare_one(value, role="source", field="image") for value in parsed.images
                ]
                mask_prepared = (
                    None if parsed.mask is None
                    else await _prepare_one(parsed.mask, role="mask", field="mask")
                )
                await job_api.dry_run_unsaved(
                    kind, params,
                    [_memory(item) for item in prepared],
                    None if mask_prepared is None else _memory(mask_prepared),
                    request_id=request_id,
                )
                await job_api.require_runnable(kind)
                # A repeated key that landed while we decoded is answered
                # without storing these bytes.
                prior = _prior(request_id)
                if prior is None:
                    images = [
                        await _import_one(value, role="source", field="image")
                        for value in parsed.images
                    ]
                    if parsed.mask is not None:
                        mask = await _import_one(parsed.mask, role="mask", field="mask")
                    prior = _prior(request_id)
            if prior is None:
                prior = await _submit(kind, params, images=images, mask=mask, request_id=request_id)
        assert prior is not None
        views = await _wait_all(prior)
    except _TimedOut as exc:
        return _with_warnings(_timeout_response(exc), warnings)
    except RequestError as exc:
        return _with_warnings(error_response(exc), warnings)
    except HTTPException as exc:
        return _with_warnings(error_response(exc), warnings)
    return await _image_response(request, parsed, prior, views, warnings)


async def _handled(request: Request, parsed: _Parsed, *, edit: bool) -> JSONResponse:
    try:
        return await _run(request, parsed, edit=edit)
    except RequestError as exc:
        return _with_warnings(error_response(exc), parsed.warnings)
    except HTTPException as exc:
        return _with_warnings(error_response(exc), parsed.warnings)


@router.get("/models")
def list_models() -> dict[str, Any]:
    """Friendly ids for every configured image variant, with its catalogue note."""
    return {"object": "list", "data": [model_object(variant) for variant in _configured()]}


@router.post("/images/generations")
async def images_generations(request: Request) -> JSONResponse:
    """Generate images. ``local/*`` is ``image_local``; ``remote/*`` is ``image_colab``."""
    if "multipart/form-data" in request.headers.get("content-type", "").lower():
        _reject(400, "generations takes a JSON body", code="invalid_request")
    parsed = _parse_common(await _json_object(request), _GENERATE_FIELDS)
    return await _handled(request, parsed, edit=False)


@router.post("/images/edits")
async def images_edits(request: Request) -> JSONResponse:
    """Edit images. A mask selects inpaint; without one, img2img or image_edit.

    Multipart ``image`` / ``image[]`` files, or JSON data URLs. An http URL is
    a 400 so this route cannot be pointed at another server.
    """
    if "multipart/form-data" in request.headers.get("content-type", "").lower():
        parsed = await _edits_form(request)
    else:
        body = await _json_object(request)
        common = _parse_common(body, _EDIT_FIELDS)
        parsed = _Parsed(
            model=common.model, prompt=common.prompt, n=common.n, size=common.size,
            response_format=common.response_format, background=common.background,
            warnings=common.warnings, images=_json_images(body), mask=_json_mask(body),
        )
    return await _handled(request, parsed, edit=True)
