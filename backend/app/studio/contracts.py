"""The schema vocabulary for presets, resources and execution plans.

Names here are contracts shared by storage, the resolver, HTTP and MCP, so they
are decided once (docs/studio-contracts.md) and every later module imports them
rather than spelling its own. Pure: pydantic and the standard library only.

Three document families look alike and mean different things, and
:func:`classify` keeps them apart:

* a **saved generator** (schema v1, `recipe` rows with a `kind`): one frozen job
  kind and a narrow exposed-input schema. Unchanged by this module.
* a **preset** (schema v2): a named, versioned creative setup that points at
  resources and is resolved into a plan.
* a **Variant Set recipe** (`variantrecipe` rows): staged expansion across axes.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PRESET_SCHEMA = "wizards-brush/preset"
PRESET_SCHEMA_VERSION = 2
PLAN_SCHEMA = "wizards-brush/plan"
PLAN_SCHEMA_VERSION = 1
# Bumped whenever the resolver's output for an unchanged input could change.
RESOLVER_VERSION = "1"

# Every persisted job kind. Rows on disk carry these strings forever, so the
# set only grows; tests/test_studio_contracts.py fails if one disappears.
PERSISTED_KINDS: frozenset[str] = frozenset({
    "image_local", "image_colab", "image_edit", "img2img", "inpaint", "inpaint_remote",
    "outpaint", "control_local", "t2v", "i2v", "long_video", "extend_video", "upscale",
    "face_restore", "interpolate", "detail", "matte", "region_mask",
})

# Logical operations: what the caller wants done, independent of where. A plan
# maps (operation, resolved target) onto one existing kind; see KIND_FOR.
Operation = Literal[
    "text_to_image", "image_to_image", "inpaint", "outpaint", "edit", "control",
    "text_to_video", "image_to_video", "long_video",
]
OPERATIONS: tuple[str, ...] = (
    "text_to_image", "image_to_image", "inpaint", "outpaint", "edit", "control",
    "text_to_video", "image_to_video", "long_video",
)
Target = Literal["local", "remote", "auto"]
ResolvedTarget = Literal["local", "remote"]

# (operation, resolved target) -> the persisted kind that executes it. A pair
# that is absent is not offered on that target today, and the resolver says so
# rather than borrowing the other target's kind.
KIND_FOR: dict[tuple[str, str], str] = {
    ("text_to_image", "local"): "image_local",
    ("text_to_image", "remote"): "image_colab",
    ("image_to_image", "local"): "img2img",
    ("inpaint", "local"): "inpaint",
    ("inpaint", "remote"): "inpaint_remote",
    ("outpaint", "local"): "outpaint",
    ("edit", "remote"): "image_edit",
    ("control", "local"): "control_local",
    ("text_to_video", "remote"): "t2v",
    ("image_to_video", "remote"): "i2v",
    ("long_video", "remote"): "long_video",
}

# Where a value in a plan came from. Kept per field so a model change can move
# only what was never chosen, and so "set equal to the default on purpose"
# stays distinguishable from "left alone".
Origin = Literal[
    "registry_default",   # the control's own default; nothing chose it
    "dependent_default",  # a defaults_by map chose it from another field
    "preset",             # the preset revision authored it
    "override",           # the user or calling AI set it explicitly
    "imported",           # read from a historical job or legacy record
    "runtime",            # resolved for execution only (seed, target, sizes)
]
DEFAULT_ORIGINS = frozenset({"registry_default", "dependent_default", "preset"})

# Resource lifecycle. Separate facts: a curated recommendation, structural
# compatibility, a test on this target and current readiness are not one flag.
ResourceState = Literal[
    "known", "access_required", "downloading", "installed", "verified", "runnable",
    "unavailable", "failed",
]
Verification = Literal["legacy_unverified", "unverified", "verified", "mismatch"]

# Placeholders a preset prompt template may use, in the Variant Set grammar
# ({{name}}); nothing else is substituted and nothing is evaluated.
TEMPLATE_PLACEHOLDERS: frozenset[str] = frozenset({"prompt", "styles", "triggers", "notes"})

_PRESET_ID = re.compile(r"^[a-z0-9]+(?:[-.][a-z0-9]+)*(?:/[a-z0-9]+(?:[-.][a-z0-9]+)*)?$")
# A resource id is "<provider>:<identity>"; see resource_id().
_RESOURCE_ID = re.compile(r"^(hf|civitai|local|builtin):[A-Za-z0-9._/@:+=-]{1,300}$")
_TAG = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,31}$")
_FIELD_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
MAX_ADAPTERS = 4
MAX_FINISH_STEPS = 8
MAX_TEMPLATE_CHARS = 2000


class ContractError(ValueError):
    """A document that does not match its declared schema.

    `field` is a dotted path ("adapters.1.strength") so a caller can point at
    the exact value, and `code` is stable for programs.
    """

    def __init__(self, message: str, *, field: str | None = None,
                 code: str = "invalid_request") -> None:
        super().__init__(message)
        self.field = field
        self.code = code


# ---- canonical form and hashing --------------------------------------------------------
def _canonical(value: Any) -> Any:
    """JSON-ready value whose encoding cannot vary for an equal meaning.

    4 and 4.0 are the same guidance and must hash the same; key order is
    sorted at encoding time; tuples become lists. NaN and infinity have no JSON
    spelling and are refused rather than silently encoded as strings.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("a hashed value must be a finite number", code="invalid_type")
        return int(value) if value.is_integer() else value
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="json"))
    raise ContractError(f"cannot hash a {type(value).__name__}", code="invalid_type")


def canonical_json(value: Any) -> str:
    return json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def digest(value: Any, *, domain: str) -> str:
    """sha256 over a domain tag and the canonical JSON. The tag keeps a
    creative hash from ever colliding with an execution hash of equal bytes."""
    payload = f"{domain}\n{canonical_json(value)}".encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


# ---- resource identity -----------------------------------------------------------------
def resource_id(provider: str, identity: str, revision: str | None = None) -> str:
    """The application id for a resource: provider identity plus exact version.

    hf:Tongyi-MAI/Z-Image-Turbo@f332072...  a Hub repo at a commit
    civitai:model/123/version/456/file/789  one exact Civitai file
    local:sha256:<64 hex>                   an imported file, by content
    builtin:<name>                          code that ships with the app

    An unpinned Hub repo gets "@unpinned": it follows whatever the operator's
    configured identifier serves, and its plans say so instead of claiming an
    exact revision.
    """
    if provider == "hf":
        return f"hf:{identity}@{revision or 'unpinned'}"
    if provider == "civitai" or provider == "builtin":
        return f"{provider}:{identity}"
    if provider == "local":
        return f"local:sha256:{identity}"
    raise ContractError(f"unknown resource provider {provider!r}", code="invalid_request")


def check_resource_id(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not _RESOURCE_ID.fullmatch(text) or ".." in text:
        raise ContractError(
            f"{field} must be a resource id such as hf:<repo>@<revision> or local:sha256:<hash>",
            field=field, code="invalid_identifier",
        )
    return text


# ---- preset definition (schema v2) -----------------------------------------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelRef(_Strict):
    resource: str = Field(description="Resource id of the base model.")

    @field_validator("resource")
    @classmethod
    def _rid(cls, v: str) -> str:
        return check_resource_id(v, "model.resource")


class AdapterRef(_Strict):
    resource: str = Field(description="Resource id of the adapter (LoRA).")
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    text_encoder_strength: float | None = Field(default=None, ge=-2.0, le=2.0)
    # Trigger guidance an author recommends; only reaches the prompt through an
    # explicit {{triggers}} placeholder or enabled enhancement.
    trigger: str = Field(default="", max_length=200)

    @field_validator("resource")
    @classmethod
    def _rid(cls, v: str) -> str:
        return check_resource_id(v, "adapters.resource")


class StyleFragment(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")
    label: str = Field(default="", max_length=80)
    text: str = Field(max_length=600)


class PromptSpec(_Strict):
    """Declared fragments composed in a fixed order; see docs/studio-contracts.md."""

    template: str = Field(default="{{prompt}}", max_length=MAX_TEMPLATE_CHARS)
    negative: str = Field(default="", max_length=MAX_TEMPLATE_CHARS)
    styles: list[StyleFragment] = Field(default_factory=list, max_length=32)
    default_styles: list[str] = Field(default_factory=list, max_length=32)
    # Model-specific enhancement: declared modifier text, never an LLM. Off
    # unless the preset turns it on; the caller can always turn it off.
    enhancement: list[str] = Field(default_factory=list, max_length=8)
    enhance_by_default: bool = False

    @field_validator("template", "negative")
    @classmethod
    def _grammar(cls, v: str) -> str:
        check_template(v, field="prompt.template")
        return v

    @model_validator(mode="after")
    def _styles_known(self) -> PromptSpec:
        ids = [s.id for s in self.styles]
        if len(ids) != len(set(ids)):
            raise ContractError("prompt.styles has a duplicate id", field="prompt.styles")
        for index, sid in enumerate(self.default_styles):
            if sid not in ids:
                raise ContractError(
                    f"prompt.default_styles.{index} names style {sid!r}, which is not declared",
                    field=f"prompt.default_styles.{index}",
                )
        return self


class FinishStep(BaseModel):
    """One processor in an ordered finishing chain. Options are processor
    specific and checked against app/finishing.py at resolve time."""

    model_config = ConfigDict(extra="allow")
    processor: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")


class ReferenceRole(_Strict):
    role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    label: str = Field(default="", max_length=80)
    accepts: list[Literal["image", "mask"]] = Field(
        default_factory=lambda: list[Literal["image", "mask"]](["image"]))
    min: int = Field(default=0, ge=0, le=8)
    max: int = Field(default=1, ge=0, le=8)
    reference_set: int | None = Field(default=None, ge=1)
    notes_in_prompt: bool = False

    @model_validator(mode="after")
    def _bounds(self) -> ReferenceRole:
        if self.max < self.min:
            raise ContractError(f"references.{self.role}: max is below min",
                                field="references.max")
        return self


class ValidatorPolicy(_Strict):
    validator: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    required: bool = False
    options: dict[str, Any] = Field(default_factory=dict)


class FieldNote(_Strict):
    """Help an author attached to an editable field. Description, not schema:
    types and bounds always come from the registry control."""

    name: str
    label: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=400)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _FIELD_NAME.fullmatch(v):
            raise ContractError("fields.name must be a setting name", field="fields.name")
        return v


class Evidence(_Strict):
    kind: Literal["evaluation", "example", "upstream", "note"]
    ref: str = Field(default="", max_length=400)
    note: str = Field(default="", max_length=1000)


class TestedTarget(_Strict):
    target: ResolvedTarget
    hardware: str = Field(max_length=120)
    runtime: str = Field(default="", max_length=200)
    tested_on: str = Field(default="", max_length=32)


class Curation(_Strict):
    status: Literal["unreviewed", "reviewed", "tested"] = "unreviewed"
    evidence: list[Evidence] = Field(default_factory=list, max_length=32)
    tested_targets: list[TestedTarget] = Field(default_factory=list, max_length=16)
    limitations: list[str] = Field(default_factory=list, max_length=32)
    examples: list[int] = Field(default_factory=list, max_length=16)


class PresetDefinition(_Strict):
    """A named, versioned creative setup. Curated and custom share this schema;
    `origin` records who owns it, never how good it is."""

    schema_: Literal["wizards-brush/preset"] = Field(default="wizards-brush/preset", alias="schema")
    schema_version: int = PRESET_SCHEMA_VERSION
    id: str = Field(max_length=96)
    revision: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    origin: Literal["curated", "custom"] = "custom"
    tags: list[str] = Field(default_factory=list, max_length=16)
    operation: Operation
    model: ModelRef
    # Ordered: a stack's order changes how adapters merge, so it is never sorted.
    adapters: list[AdapterRef] = Field(default_factory=list, max_length=MAX_ADAPTERS)
    prompt: PromptSpec = Field(default_factory=PromptSpec)
    settings: dict[str, Any] = Field(default_factory=dict)
    finish: list[FinishStep] = Field(default_factory=list, max_length=MAX_FINISH_STEPS)
    references: list[ReferenceRole] = Field(default_factory=list, max_length=8)
    validation: list[ValidatorPolicy] = Field(default_factory=list, max_length=16)
    fields: list[FieldNote] = Field(default_factory=list, max_length=32)
    curation: Curation = Field(default_factory=Curation)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("schema_version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != PRESET_SCHEMA_VERSION:
            raise ContractError(
                f"unsupported preset schema_version {v}; this build reads {PRESET_SCHEMA_VERSION}",
                field="schema_version", code="unsupported_schema",
            )
        return v

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _PRESET_ID.fullmatch(v):
            raise ContractError(
                "id must be lowercase letters, digits, '-' or '.', optionally one '/' namespace",
                field="id", code="invalid_identifier",
            )
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        for index, tag in enumerate(v):
            if not _TAG.fullmatch(tag):
                raise ContractError(f"tags.{index} must be a short lowercase tag",
                                    field=f"tags.{index}")
        return list(dict.fromkeys(v))

    @field_validator("settings")
    @classmethod
    def _settings(cls, v: dict[str, Any]) -> dict[str, Any]:
        for name, value in v.items():
            if not _FIELD_NAME.fullmatch(name):
                raise ContractError(f"settings.{name} is not a setting name",
                                    field=f"settings.{name}", code="unknown_setting")
            if isinstance(value, (dict, list)) and name != "style_ids":
                raise ContractError(
                    f"settings.{name} must be a scalar; adapters, finishing and references "
                    "have their own fields",
                    field=f"settings.{name}", code="invalid_type",
                )
        return v

    @model_validator(mode="after")
    def _curated_evidence(self) -> PresetDefinition:
        roles = [r.role for r in self.references]
        if len(roles) != len(set(roles)):
            raise ContractError("references has a duplicate role", field="references")
        if self.origin == "curated":
            missing = [name for name, value in (
                ("curation.evidence", self.curation.evidence),
                ("curation.tested_targets", self.curation.tested_targets),
                ("curation.limitations", self.curation.limitations),
            ) if not value]
            if missing:
                raise ContractError(
                    f"a curated preset needs {missing[0]}", field=missing[0],
                    code="curation_incomplete",
                )
        return self

    def authored(self) -> dict[str, Any]:
        """The definition as stored: the author's values, nothing compiled."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=False)

    def definition_hash(self) -> str:
        """Identity of this revision's content. Stored with the revision so a
        snapshot that changed under the same number is detectable."""
        return digest(self.authored(), domain="wizards-brush/preset-definition")


def check_template(text: str, *, field: str) -> list[str]:
    """Placeholders in a preset template; anything but the fixed set is refused."""
    from ..variant_sets import templates

    try:
        names = templates.check(text, field=field, available=TEMPLATE_PLACEHOLDERS)
    except templates.TemplateError as error:
        raise ContractError(str(error), field=field, code="invalid_template") from None
    if "{{" in text and any(".value" in m.group(0) for m in templates.PLACEHOLDER.finditer(text)):
        raise ContractError(f"{field}: .value is a Variant Set form; write {{{{name}}}}",
                            field=field, code="invalid_template")
    return names


def parse_preset(raw: Mapping[str, Any]) -> PresetDefinition:
    """Validate a preset document, turning pydantic's errors into one ContractError
    whose `field` is the dotted path of the first problem."""
    from pydantic import ValidationError

    if not isinstance(raw, Mapping):
        raise ContractError("a preset must be a JSON object", code="invalid_type")
    version = raw.get("schema_version", PRESET_SCHEMA_VERSION)
    if version != PRESET_SCHEMA_VERSION:
        raise ContractError(
            f"unsupported preset schema_version {version!r}; this build reads "
            f"{PRESET_SCHEMA_VERSION}",
            field="schema_version", code="unsupported_schema",
        )
    try:
        return PresetDefinition.model_validate(dict(raw))
    except ContractError:
        raise
    except ValidationError as error:
        first = error.errors()[0]
        path = ".".join(str(part) for part in first.get("loc", ()) if part != "__root__")
        inner = (first.get("ctx") or {}).get("error")
        if isinstance(inner, ContractError):
            raise ContractError(str(inner), field=inner.field or path or None,
                                code=inner.code) from None
        code = {"extra_forbidden": "unknown_field", "missing": "missing_field"}.get(
            str(first.get("type")), "invalid_request")
        message = str(first.get("msg") or "invalid value")
        raise ContractError(f"{path or 'preset'}: {message}", field=path or None,
                            code=code) from None


# ---- document families -----------------------------------------------------------------
DocumentKind = Literal["saved_generator_v1", "preset_v2", "variant_recipe"]


def classify(doc: Mapping[str, Any]) -> DocumentKind:
    """Which family a stored or submitted document belongs to.

    Decided by the fields each family must have, never by guessing from
    optional ones: a v2 preset carries schema_version 2 and an operation; a v1
    saved generator has a job `kind` and no schema_version above 1; a Variant
    Set recipe has `stages` or `axes`. Anything else is refused.
    """
    version = doc.get("schema_version")
    if version == PRESET_SCHEMA_VERSION or doc.get("schema") == PRESET_SCHEMA:
        if "kind" in doc:
            raise ContractError(
                "a preset names an operation, not a job kind; send one or the other",
                field="kind", code="ambiguous_request",
            )
        return "preset_v2"
    if "stages" in doc or "axes" in doc:
        return "variant_recipe"
    if isinstance(doc.get("kind"), str) and version in (None, 1):
        return "saved_generator_v1"
    if isinstance(version, int) and version > PRESET_SCHEMA_VERSION:
        raise ContractError(f"unsupported schema_version {version}", field="schema_version",
                            code="unsupported_schema")
    raise ContractError("not a saved generator, preset or Variant Set recipe",
                        code="invalid_request")
