"""Registry controls as a validation contract and a JSON Schema.

A registry control describes a setting for the UI (generators/registry.py) and
for the tool catalogue (routers/tools.py). This module reads the same entries
as a contract, so every programmatic entry point — the unified job API and
Variant Set recipes — refuses a wrong value with a reason instead of letting
the route's sanitization clamp it silently, and so `GET /api/jobs/kinds` can
publish a JSON Schema that cannot drift from what is enforced.

Pure: no torch, no database, no request objects.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from .problems import close_suggestions
from .prompt_engine import MAX_PROMPT_CHARS


class ControlError(ValueError):
    """A value a control does not accept; the message names the control.

    `code`, `field`, `valid_options` and `suggestions` are the structured form
    the jobs API and MCP tools return. `str(error)` stays the detail text.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        code: str = "invalid_request",
        valid_options: list[Any] | None = None,
        suggestions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.code = code
        self.valid_options = list(valid_options) if valid_options else None
        self.suggestions = [item for item in (suggestions or []) if item][:3]


def _reject(
    control: Mapping[str, Any],
    message: str,
    *,
    code: str,
    valid_options: list[Any] | None = None,
    suggestions: list[str] | None = None,
) -> NoReturn:
    name = str(control.get("name") or "")
    raise ControlError(
        message, field=name or None, code=code,
        valid_options=valid_options, suggestions=suggestions,
    )


def effective(control: Mapping[str, Any], params: Mapping[str, Any],
              controls: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    """`control` with any `overrides_by` for the value its field will have.

    Video frame limits depend on the engine: Wan takes 25-121 and HunyuanVideo
    9-129. The field's own default applies when the request leaves it out,
    exactly as the form would.
    """
    rule = control.get("overrides_by")
    if not isinstance(rule, Mapping):
        return control
    field = str(rule.get("field"))
    value = params.get(field, (controls.get(field) or {}).get("default"))
    patch = (rule.get("map") or {}).get(str(value))
    return {**control, **patch} if isinstance(patch, Mapping) else control


def check_value(control: Mapping[str, Any], value: Any) -> Any:
    """Validate one value against its control; returns it. Raises ControlError."""
    name, kind = control.get("name"), control.get("type")
    if kind in ("select", "segmented", "aspect"):
        options = list(control.get("options") or [])
        labels = [str(option) for option in options]
        if str(value) not in labels or isinstance(value, (dict, list)):
            _reject(
                control, f"{name} must be one of {', '.join(labels)}",
                code="invalid_option", valid_options=options,
                suggestions=close_suggestions(value, options),
            )
        return value
    if kind in ("slider", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _reject(control, f"{name} must be a number", code="invalid_type",
                    suggestions=["Pass a number."])
        low, high = control.get("min"), control.get("max")
        if (low is not None and value < low) or (high is not None and value > high):
            _reject(
                control, f"{name} must be between {low} and {high}",
                code="out_of_range",
                suggestions=[f"Use a value between {low} and {high}."],
            )
        return value
    if kind == "toggle":
        if not isinstance(value, bool):
            _reject(control, f"{name} must be true or false", code="invalid_type",
                    suggestions=["Pass true or false."])
        return value
    if kind == "textarea":
        if not isinstance(value, str) or len(value) > MAX_PROMPT_CHARS:
            _reject(
                control, f"{name} must be text of at most {MAX_PROMPT_CHARS} characters",
                code="invalid_type",
                suggestions=[f"Pass text of at most {MAX_PROMPT_CHARS} characters."],
            )
        return value
    if kind == "lora":
        # A path relative to LORA_DIR, or {"path", "weight"}; loras.sanitize()
        # resolves and bounds them at the route, as it does for the form.
        if not isinstance(value, list) or not all(isinstance(v, (str, Mapping)) for v in value):
            _reject(
                control,
                f"{name} must be a list of LoRA paths or {{\"path\", \"weight\"}} objects",
                code="invalid_type",
                suggestions=["Pass a list of LoRA paths or {\"path\", \"weight\"} objects."],
            )
        return value
    if kind == "multiselect":
        options = [str(o) for o in control.get("options") or []]
        if (not isinstance(value, list) or not value
                or any(str(v) not in options for v in value)):
            _reject(
                control, f"{name} must be a non-empty list of: {', '.join(options)}",
                code="invalid_option", valid_options=options,
            )
        return list(dict.fromkeys(value))
    if kind == "finishing":
        from .finishing import FinishingError, sanitize_steps

        try:
            return sanitize_steps(value)
        except FinishingError as error:
            _reject(control, f"{name}: {error}", code="invalid_option")
    _reject(control, f"{name} cannot be set here", code="invalid_request")


def check_params(params: Mapping[str, Any], controls: Mapping[str, Mapping[str, Any]], *,
                 subject: str) -> dict[str, Any]:
    """Validate a whole params object against `controls`, strictly.

    An unknown key is an error rather than being ignored: to a program a typo
    that silently falls back to a default is the worst possible outcome.
    """
    clean: dict[str, Any] = {}
    for name, value in params.items():
        control = controls.get(name)
        if control is None:
            valid = sorted(controls)
            shown = ", ".join(valid) or "none"
            raise ControlError(
                f"'{name}' is not a setting of {subject} (valid: {shown})",
                field=str(name), code="unknown_setting", valid_options=valid,
                suggestions=close_suggestions(name, valid),
            )
        clean[name] = check_value(effective(control, params, controls), value)
    return clean


# ---- JSON Schema --------------------------------------------------------------------------------
_LORA_ITEM = {
    "anyOf": [
        {"type": "string"},
        {"type": "object",
         "properties": {"path": {"type": "string"}, "weight": {"type": "number"}},
         "required": ["path"]},
    ],
    "description": "A LoRA path relative to the LoRA library (GET /api/loras).",
}
_FINISHING_ITEM = {
    "type": "object",
    "properties": {"processor": {"type": "string"}},
    "required": ["processor"],
    "description": "A finishing step; see GET /api/variant-sets/capabilities for the "
                   "processors and their options.",
}


def schema_for(control: Mapping[str, Any]) -> dict[str, Any]:
    """One control as a JSON Schema property."""
    kind = control.get("type")
    out: dict[str, Any]
    if kind in ("select", "segmented", "aspect"):
        out = {"enum": list(control.get("options") or [])}
    elif kind in ("slider", "number"):
        out = {"type": "number"}
        if control.get("min") is not None:
            out["minimum"] = control["min"]
        if control.get("max") is not None:
            out["maximum"] = control["max"]
    elif kind == "toggle":
        out = {"type": "boolean"}
    elif kind == "textarea":
        out = {"type": "string", "maxLength": MAX_PROMPT_CHARS}
    elif kind == "lora":
        out = {"type": "array", "items": _LORA_ITEM}
    elif kind == "multiselect":
        out = {"type": "array", "items": {"enum": list(control.get("options") or [])},
               "minItems": 1, "uniqueItems": True}
    elif kind == "finishing":
        names = [str(p.get("name")) for p in control.get("processors") or []]
        item = ({**_FINISHING_ITEM, "properties": {"processor": {"enum": names}}}
                if names else _FINISHING_ITEM)
        out = {"type": "array", "items": item}
    else:
        out = {}
    if control.get("label"):
        out["title"] = str(control["label"])
    if "default" in control:
        out["default"] = control["default"]
    if isinstance(control.get("overrides_by"), Mapping):
        field = control["overrides_by"].get("field")
        out["description"] = f"Limits depend on `{field}`; the tighter ones are shown."
    return out


def params_schema(controls: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """A params object for these controls; unknown keys are refused."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {name: schema_for(control) for name, control in controls.items()},
    }
