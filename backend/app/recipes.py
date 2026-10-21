"""Saved generation recipes: compile a narrow schema, merge a run, submit a job.

A recipe freezes one job kind's settings and publishes only the fields the
author exposed. Running it validates those inputs against the schema stored on
the recipe, overlays them on the frozen params, and submits through
`POST /api/jobs`. Queueing, provenance, client tags, dry_run and structured
errors stay on that path.

The shape follows the idea of an InvokeAI workflow form: a saved setup called
by id, with a few fields pinned into a simple panel. No InvokeAI source is
copied. This is not a Variant Set recipe; those fan out across axes.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from . import controls
from .problems import RequestError, close_suggestions
from .routers.job_api import Inputs, kind_contract, require_input_asset

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_FIELDS = 32
_MAX_DESCRIPTION = 400
_MAX_PARAMS_CHARS = 100_000
_CONSTRAINT_KEYS = ("min", "max", "step", "options")
_INPUT_NAMES = ("images", "mask", "last_frame")


def clean_name(name: str) -> str:
    """Collapse whitespace. A slash would break lookup by path."""
    text = " ".join(str(name).split())
    if not text or len(text) > 80 or "/" in text:
        raise RequestError(
            400, "name must be 1-80 characters and cannot contain a slash",
            code="invalid_request", field="name",
            suggestions=["Use a short name without slashes, or pass slug."],
        )
    return text


def slugify(name: str) -> str:
    """A URL slug from `name`. Letters and digits only; accents are dropped."""
    text = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    text = text[:64].strip("-")
    if not text or not _SLUG.fullmatch(text):
        raise RequestError(
            400, "name needs a letter or digit so it can be called by slug",
            code="invalid_request", field="name",
            suggestions=["Use a name with letters or digits, or pass slug."],
        )
    return text


def check_slug(slug: str) -> str:
    text = slug.strip().lower()
    if not _SLUG.fullmatch(text) or len(text) > 64:
        raise RequestError(
            400, "slug must be 1-64 characters of lowercase letters, digits and hyphens",
            code="invalid_request", field="slug",
            suggestions=["Use a slug like ceramic-cup."],
        )
    return text


def offered_inputs(spec: Inputs) -> set[str]:
    names: set[str] = set()
    if spec.max_images > 0:
        names.add("images")
    if spec.mask != "none":
        names.add("mask")
    if spec.last_frame:
        names.add("last_frame")
    return names


def compile_recipe(
    kind: str,
    params: Mapping[str, Any],
    expose: list[str],
    inputs: Mapping[str, Any],
    descriptions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Frozen params, frozen inputs, and the exposed-field snapshot.

    Params are filled with the kind's defaults so a later default change does
    not silently retune a saved setup. Exposed names must be settings of the
    kind, or an input role it accepts (`images`, `mask`, `last_frame`).
    """
    if not isinstance(params, Mapping):
        raise RequestError(
            400, "params must be an object", code="invalid_type", field="params",
            suggestions=["Pass a JSON object of settings."],
        )
    known, spec = kind_contract(kind)
    try:
        clean = controls.check_params(dict(params), known, subject=kind)
    except controls.ControlError as error:
        raise _control_error(error) from None
    filled = _fill_defaults(known, clean)
    try:
        filled = controls.check_params(filled, known, subject=kind)
    except controls.ControlError as error:
        raise _control_error(error) from None
    _reject_huge(filled)

    frozen_inputs = _frozen_inputs(inputs)
    fields = _fields(kind, known, spec, filled, frozen_inputs, expose, descriptions or {})
    _check_frozen_shape(kind, spec, frozen_inputs, {item["name"] for item in fields})
    return {"kind": kind, "params": filled, "inputs": frozen_inputs, "fields": fields}


def validate_assets(kind: str, inputs: Mapping[str, Any]) -> None:
    """Frozen asset ids must exist and, when both are set, share a size.

    An exposed image with an empty default is checked later, on the run.
    """
    _known, spec = kind_contract(kind)
    images = [int(item) for item in inputs.get("images") or []]
    rows = [
        require_input_asset(asset_id, spec.image_kind, f"inputs.images[{index}]")
        for index, asset_id in enumerate(images)
    ]
    mask_id = inputs.get("mask")
    last_id = inputs.get("last_frame")
    mask = None if mask_id is None else require_input_asset(int(mask_id), "image", "inputs.mask")
    if last_id is not None:
        require_input_asset(int(last_id), "image", "inputs.last_frame")
    if rows and mask is not None and (mask.width, mask.height) != (rows[0].width, rows[0].height):
        raise RequestError(
            400,
            f"inputs.mask is {mask.width}x{mask.height} but the image is "
            f"{rows[0].width}x{rows[0].height}; a mask must match its image",
            code="invalid_request", field="inputs.mask",
            suggestions=["Use a mask the same width and height as the image."],
        )


def schema_for_fields(fields: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The JSON Schema a run's `inputs` must match. One schema for UI and agents."""
    properties = {str(field["name"]): _field_schema(field) for field in fields}
    required = [str(field["name"]) for field in fields if _required(field)]
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def merge_run(
    params: Mapping[str, Any],
    frozen_inputs: Mapping[str, Any],
    fields: Sequence[Mapping[str, Any]],
    provided: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Overlay `provided` onto the frozen setup. Unknown keys are refused."""
    if not isinstance(provided, Mapping):
        raise RequestError(
            400, "inputs must be an object", code="invalid_type", field="inputs",
            suggestions=["Pass a JSON object whose keys are the recipe's fields."],
        )
    by_name = {str(field["name"]): field for field in fields}
    clean: dict[str, Any] = {}
    for name in provided:
        field = by_name.get(str(name))
        if field is None:
            valid = list(by_name)
            shown = ", ".join(valid) or "none"
            raise RequestError(
                400, f"'{name}' is not an input of this recipe (valid: {shown})",
                code="unknown_setting", field=str(name), valid_options=valid,
                suggestions=close_suggestions(name, valid) or [
                    "Call get_recipe and send only the fields in input_schema.",
                ],
            )
        try:
            clean[str(name)] = _check_field(field, provided[name])
        except controls.ControlError as error:
            raise _control_error(error) from None
    missing = [name for name, field in by_name.items() if _required(field) and name not in clean]
    if missing:
        raise RequestError(
            400, f"'{missing[0]}' is required",
            code="invalid_request", field=missing[0],
            suggestions=[f"Pass {missing[0]}. The recipe schema lists required fields."],
        )

    merged = dict(params)
    job_inputs = {
        "images": [int(item) for item in frozen_inputs.get("images") or []],
        "mask": frozen_inputs.get("mask"),
        "last_frame": frozen_inputs.get("last_frame"),
    }
    for name, value in clean.items():
        if name in _INPUT_NAMES:
            job_inputs[name] = value
        else:
            merged[name] = value
    return merged, job_inputs


def public_recipe(row: Any, *, summary: bool = False) -> dict[str, Any]:
    """The document the HTTP API and the MCP tools both return."""
    fields = list(row.fields)
    data: dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "slug": row.slug,
        "description": row.description or "",
        "kind": row.kind,
        "fields": fields,
        "input_schema": schema_for_fields(fields),
        "client": row.client or "",
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if summary:
        return data
    stored = row.inputs if isinstance(row.inputs, Mapping) else {}
    data["params"] = dict(row.params)
    data["inputs"] = {
        "images": [int(item) for item in stored.get("images") or [] if isinstance(item, int)],
        "mask": stored.get("mask") if isinstance(stored.get("mask"), int) else None,
        "last_frame": stored.get("last_frame") if isinstance(stored.get("last_frame"), int) else None,
    }
    return data


def _fill_defaults(known: Mapping[str, Mapping[str, Any]], clean: Mapping[str, Any]) -> dict[str, Any]:
    filled: dict[str, Any] = {}
    for name, control in known.items():
        if name in clean:
            filled[name] = clean[name]
        elif "default" in control:
            filled[name] = control.get("default")
    for name, control in known.items():
        if name in clean:
            continue
        rule = control.get("defaults_by")
        if not isinstance(rule, Mapping):
            continue
        dep = str(rule.get("field") or "")
        dep_value = filled.get(dep, (known.get(dep) or {}).get("default"))
        mapped = (rule.get("map") or {}).get(str(dep_value))
        if mapped is not None:
            filled[name] = mapped
    return filled


def _reject_huge(params: Mapping[str, Any]) -> None:
    try:
        encoded = json.dumps(params)
    except (TypeError, ValueError) as error:
        raise RequestError(
            400, "params must be JSON values", code="invalid_type", field="params",
        ) from error
    if len(encoded) > _MAX_PARAMS_CHARS:
        raise RequestError(
            400, "params are too large to save as a recipe",
            code="invalid_request", field="params",
            suggestions=["Save fewer settings, or shorten the prompt."],
        )


def _frozen_inputs(inputs: Mapping[str, Any]) -> dict[str, Any]:
    images = inputs.get("images") or []
    bad_ids = (
        not isinstance(images, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in images)
    )
    if bad_ids:
        raise RequestError(
            400, "inputs.images must be a list of asset ids",
            code="invalid_type", field="inputs.images",
            suggestions=["Pass gallery asset ids, not paths or URLs."],
        )
    mask = inputs.get("mask")
    last = inputs.get("last_frame")
    for label, value in (("inputs.mask", mask), ("inputs.last_frame", last)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise RequestError(
                400, f"{label} must be an asset id",
                code="invalid_type", field=label,
                suggestions=["Pass a gallery asset id."],
            )
    return {"images": [int(item) for item in images], "mask": mask, "last_frame": last}


def _fields(
    kind: str,
    known: Mapping[str, Mapping[str, Any]],
    spec: Inputs,
    params: Mapping[str, Any],
    frozen_inputs: Mapping[str, Any],
    expose: list[str],
    descriptions: Mapping[str, str],
) -> list[dict[str, Any]]:
    if len(expose) > _MAX_FIELDS:
        raise RequestError(
            400, f"a recipe can expose at most {_MAX_FIELDS} fields",
            code="invalid_request", field="expose",
        )
    roles = offered_inputs(spec)
    legal = [*known.keys(), *sorted(roles)]
    extra = [key for key in descriptions if key not in expose]
    if extra:
        raise RequestError(
            400, f"field_descriptions.{extra[0]} is not an exposed field",
            code="unknown_setting", field=f"field_descriptions.{extra[0]}",
            valid_options=list(expose),
            suggestions=close_suggestions(extra[0], expose),
        )
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_name in expose:
        name = str(raw_name)
        if name in seen:
            raise RequestError(
                400, f"'{name}' is listed twice", code="invalid_request", field="expose",
            )
        seen.add(name)
        description = _description(name, descriptions.get(name))
        is_control = name in known
        is_input = name in roles
        if is_control and is_input:
            raise RequestError(
                400, f"'{name}' is both a setting and an input; it cannot be exposed",
                code="invalid_request", field=name,
            )
        if is_control:
            fields.append(_snapshot_control(known[name], params, known, description))
        elif is_input:
            fields.append(_snapshot_input(name, spec, frozen_inputs, description))
        else:
            shown = ", ".join(str(item) for item in legal) or "none"
            raise RequestError(
                400, f"'{name}' is not a setting or input of {kind} (valid: {shown})",
                code="unknown_setting", field=name, valid_options=[str(item) for item in legal],
                suggestions=close_suggestions(name, [str(item) for item in legal]),
            )
    return fields


def _description(name: str, raw: str | None) -> str:
    if raw is None:
        return ""
    text = str(raw).strip()
    if len(text) > _MAX_DESCRIPTION:
        raise RequestError(
            400, "a field description must be at most 400 characters",
            code="invalid_request", field=f"field_descriptions.{name}",
        )
    return text


def _snapshot_control(
    control: Mapping[str, Any],
    params: Mapping[str, Any],
    known: Mapping[str, Mapping[str, Any]],
    description: str,
) -> dict[str, Any]:
    effective = controls.effective(control, params, known)
    constraints: dict[str, Any] = {}
    for key in _CONSTRAINT_KEYS:
        if effective.get(key) is not None:
            value = effective[key]
            constraints[key] = list(value) if key == "options" else value
    return {
        "name": str(control["name"]),
        "type": str(effective.get("type") or control.get("type") or ""),
        "label": str(effective.get("label") or control.get("label") or control["name"]),
        "description": description,
        "default": params.get(str(control["name"])),
        "constraints": constraints,
    }


def _snapshot_input(
    name: str, spec: Inputs, frozen: Mapping[str, Any], description: str,
) -> dict[str, Any]:
    if name == "images":
        return {
            "name": "images",
            "type": "images",
            "label": "Source images",
            "description": description or f"Gallery {spec.image_kind} asset ids, in order.",
            "default": list(frozen.get("images") or []),
            "constraints": {
                "min_items": spec.min_images,
                "max_items": spec.max_images,
                "asset_kind": spec.image_kind,
            },
        }
    if name == "mask":
        return {
            "name": "mask",
            "type": "asset",
            "label": "Mask",
            "description": description or "White marks what should change. Same size as the image.",
            "default": frozen.get("mask"),
            "constraints": {"asset_kind": "image", "required": spec.mask == "required"},
        }
    return {
        "name": "last_frame",
        "type": "asset",
        "label": "Last frame",
        "description": description or "Optional last-frame image.",
        "default": frozen.get("last_frame"),
        "constraints": {"asset_kind": "image", "required": False},
    }


def _check_frozen_shape(kind: str, spec: Inputs, frozen: Mapping[str, Any], exposed: set[str]) -> None:
    images = list(frozen.get("images") or [])
    if "images" in exposed:
        if images and not spec.min_images <= len(images) <= spec.max_images:
            _bad_count(kind, spec, len(images))
    elif not spec.min_images <= len(images) <= spec.max_images:
        hint = ("Expose images, or pass inputs.images." if spec.min_images
                else "This kind takes no images.")
        raise RequestError(
            400, f"{kind} takes {_want(spec)} input {spec.image_kind}(s); got {len(images)}",
            code="invalid_request", field="inputs.images", suggestions=[hint],
        )
    if spec.mask == "none" and frozen.get("mask") is not None:
        raise RequestError(
            400, f"{kind} does not take a mask",
            code="invalid_request", field="inputs.mask",
            suggestions=["Omit inputs.mask for this kind."],
        )
    if spec.mask == "required" and "mask" not in exposed and frozen.get("mask") is None:
        raise RequestError(
            400, f"{kind} needs a mask. Expose mask, or pass inputs.mask.",
            code="invalid_request", field="inputs.mask",
            suggestions=["White marks what should change, same size as the image."],
        )
    if not spec.last_frame and frozen.get("last_frame") is not None:
        raise RequestError(
            400, f"{kind} does not take a last frame",
            code="invalid_request", field="inputs.last_frame",
            suggestions=["Omit inputs.last_frame for this kind."],
        )


def _bad_count(kind: str, spec: Inputs, count: int) -> None:
    raise RequestError(
        400, f"{kind} takes {_want(spec)} input {spec.image_kind}(s); got {count}",
        code="invalid_request", field="inputs.images",
        suggestions=[f"Pass {_want(spec)} {spec.image_kind} asset id(s), or leave the default empty."],
    )


def _want(spec: Inputs) -> str:
    if spec.min_images == spec.max_images:
        return str(spec.min_images)
    return f"{spec.min_images}-{spec.max_images}"


def _required(field: Mapping[str, Any]) -> bool:
    kind = field.get("type")
    constraints = field.get("constraints") or {}
    if kind == "images":
        default = field.get("default") or []
        count = len(default) if isinstance(default, list) else 0
        return count < int(constraints.get("min_items") or 0)
    if kind == "asset":
        return bool(constraints.get("required")) and field.get("default") is None
    return False


def _field_schema(field: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(field.get("type") or "")
    constraints = field.get("constraints") or {}
    title = str(field.get("label") or field.get("name") or "")
    description = str(field.get("description") or "")
    if kind == "images":
        schema: dict[str, Any] = {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": int(constraints.get("min_items") or 0),
            "maxItems": int(constraints.get("max_items") or 0),
            "title": title,
            "default": list(field.get("default") or []),
        }
    elif kind == "asset":
        schema = {
            "title": title,
            "default": field.get("default"),
        }
        if constraints.get("required") and field.get("default") is None:
            schema["type"] = "integer"
        else:
            schema["type"] = ["integer", "null"]
    else:
        schema = controls.schema_for(_control_view(field))
        if title and "title" not in schema:
            schema["title"] = title
    if description:
        schema["description"] = description
    return schema


def _control_view(field: Mapping[str, Any]) -> dict[str, Any]:
    constraints = field.get("constraints") or {}
    control: dict[str, Any] = {
        "name": str(field.get("name") or ""),
        "type": str(field.get("type") or ""),
        "label": str(field.get("label") or field.get("name") or ""),
    }
    if "default" in field:
        control["default"] = field.get("default")
    for key in _CONSTRAINT_KEYS:
        if key in constraints:
            control[key] = constraints[key]
    return control


def _check_field(field: Mapping[str, Any], value: Any) -> Any:
    kind = str(field.get("type") or "")
    if kind == "images":
        return _check_images(field, value)
    if kind == "asset":
        return _check_asset(field, value)
    return controls.check_value(_control_view(field), value)


def _check_images(field: Mapping[str, Any], value: Any) -> list[int]:
    name = str(field.get("name") or "images")
    constraints = field.get("constraints") or {}
    bad_ids = (
        not isinstance(value, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    )
    if bad_ids:
        raise RequestError(
            400, f"{name} must be a list of asset ids",
            code="invalid_type", field=name,
            suggestions=["Pass gallery asset ids, not paths or URLs."],
        )
    low = int(constraints.get("min_items") or 0)
    high = int(constraints.get("max_items") or 0)
    if not low <= len(value) <= high:
        raise RequestError(
            400, f"{name} must contain {low if low == high else f'{low}-{high}'} asset id(s)",
            code="invalid_request", field=name,
            suggestions=[f"Pass {low if low == high else f'{low}-{high}'} gallery asset id(s)."],
        )
    if any(item < 1 for item in value):
        raise RequestError(
            400, f"{name} asset ids must be positive",
            code="invalid_request", field=name,
        )
    return [int(item) for item in value]


def _check_asset(field: Mapping[str, Any], value: Any) -> int | None:
    name = str(field.get("name") or "asset")
    required = bool((field.get("constraints") or {}).get("required"))
    if value is None:
        if required:
            raise RequestError(
                400, f"{name} is required", code="invalid_request", field=name,
                suggestions=["Pass a gallery asset id."],
            )
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RequestError(
            400, f"{name} must be an asset id", code="invalid_type", field=name,
            suggestions=["Pass a positive gallery asset id."],
        )
    return int(value)


def _control_error(error: controls.ControlError) -> RequestError:
    return RequestError(
        400, str(error), code=error.code, field=error.field,
        valid_options=error.valid_options, suggestions=error.suggestions,
    )
