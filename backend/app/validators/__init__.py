"""Deterministic output validation: small, pluggable checks over a finished file.

A validator looks at one output file and says pass, warn or fail, with
structured details. Validators are torch-free and model-free on purpose: the
questions they answer — is this readable, is it the size and format that was
asked for, is the transparency real — have exact answers, and an exact check
should never depend on a download or a GPU.

**Extension point.** A validator is a function ``(ValidationContext) -> Result |
None`` registered under a stable name with :func:`register`. Returning ``None``
means "not applicable to this spec" and records nothing. The context carries the
asset id and its source asset ids as well as the file, so a later check that
needs more than pixels — similarity to the source, say, via the CLIP vector the
enrichment ladder already stores on ``Asset.embedding`` — can be added as one
more registered function, with one more optional field on
:class:`ValidationSpec`, and no change to how results are stored.

The overall verdict is the worst result: any ``fail`` fails the output, else any
``warn`` warns, else it passes. ``file`` always runs first; if the file cannot
be read, nothing else can be judged and the rest are skipped.

**Required and optional.** A check that cannot judge (no source image, no face
found, no alpha to measure margins on) says so with ``available=False`` and a
``warn``. A spec may name checks as ``required`` or ``optional``. A required
check that is unavailable, or that did not run at all, fails: a warning is never
allowed to stand in for a condition that had to hold. An optional check's
failure is recorded as a warning, so it can advance. Checks named in neither
list keep the plain behaviour, which is what every stored spec without a
policy means.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

Status = Literal["pass", "warn", "fail"]
# Judged by the Variant Set service rather than a registered check.
POLICY_ONLY = "finishing"
Verdict = Literal["passed", "warned", "failed", "skipped"]


class ValidationSpec(BaseModel):
    """What a finished output must satisfy. Every field is optional; an empty
    spec still checks that the file exists and decodes."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["PNG", "JPEG", "WEBP"] | None = None
    width: int | None = Field(default=None, ge=1, le=16384)
    height: int | None = Field(default=None, ge=1, le=16384)
    # "required": the file must carry an alpha channel. "forbidden": every pixel
    # must be fully opaque. "any": not checked.
    alpha: Literal["any", "required", "forbidden"] = "any"
    # Transparency checks read the alpha channel; they fail on a file without one.
    corners_transparent: bool = False
    min_transparent_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    max_transparent_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    # Pixels of clear space required between visible content and every edge.
    safe_margin: int | None = Field(default=None, ge=0, le=4096)
    # Alpha at or below this counts as transparent (0-254).
    transparent_threshold: int = Field(default=8, ge=0, le=254)
    # Identity lock: unmasked source pixels must match the output (feather
    # excluded). Needs the source image and the original hard mask in `meta`.
    unmasked_match: bool = False
    # Minimum OpenCV SFace cosine vs the first source face, or None to skip.
    face_identity_min: float | None = Field(default=None, ge=0.0, le=1.0)
    # Check names (see `registered()`, plus "finishing") whose condition must
    # hold, and ones whose failure only warns. Stored with the spec, so a set's
    # snapshot keeps the policy it was created with.
    required: list[str] = Field(default_factory=list, max_length=16)
    optional: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _policy(self) -> ValidationSpec:
        both = set(self.required) & set(self.optional)
        if both:
            raise ValueError(f"{sorted(both)} cannot be both required and optional")
        known = set(_REGISTRY) | {POLICY_ONLY}
        unknown = [n for n in [*self.required, *self.optional] if n not in known]
        if unknown:
            raise ValueError(f"unknown validator(s) {unknown}; known: {sorted(known)}")
        return self

    def is_empty(self) -> bool:
        return self == ValidationSpec()


def cutout_spec(*, coverage: float = 0.20, safe_margin: int = 16,
                **extra: Any) -> ValidationSpec:
    """Named cutout profile: real alpha, coverage band, edge margin.

    Coverage is the expected transparent fraction; the allowed band is
    ``coverage ± 0.35``, clamped to (0, 1). Four transparent corners are not
    required — a product that fills a corner is still a valid cutout.
    """
    lo = max(0.02, min(coverage - 0.35, 0.95))
    hi = min(0.98, max(coverage + 0.35, lo + 0.05))
    return ValidationSpec(alpha="required", min_transparent_fraction=lo,
                          max_transparent_fraction=hi, safe_margin=safe_margin, **extra)


@dataclass(frozen=True)
class Result:
    validator: str
    status: Status
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    # False when the check could not judge at all (a missing source, no face,
    # no alpha to measure). Its status is then a `warn` that means "unknown".
    available: bool = True

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"validator": self.validator, "status": self.status,
                               "message": self.message, "details": self.details}
        if not self.available:
            out["available"] = False
        return out


@dataclass
class ValidationContext:
    path: Path
    spec: ValidationSpec
    image: Image.Image | None = None      # loaded once by the `file` check
    image_format: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    asset_id: int | None = None
    source_asset_ids: tuple[int, ...] = ()


Validator = Callable[[ValidationContext], Result | None]

# Ordered: registration order is run order, and `file` must come first.
_REGISTRY: dict[str, Validator] = {}


def register(name: str, validator: Validator) -> None:
    """Add (or replace) a check. The name is persisted in results — never
    rename one that has shipped."""
    _REGISTRY[name] = validator


def registered() -> list[str]:
    return list(_REGISTRY)


def verdict(results: list[Result]) -> Verdict:
    statuses = {r.status for r in results}
    if "fail" in statuses:
        return "failed"
    if "warn" in statuses:
        return "warned"
    return "passed" if results else "skipped"


def apply_policy(results: list[Result], spec: ValidationSpec) -> list[Result]:
    """Apply `required`/`optional` to finished results. Idempotent.

    Required: unavailable becomes a fail, and a required check with no result
    is added as a fail. Optional: a fail becomes a warn that remembers it was
    one. Everything else is returned unchanged.
    """
    required, optional = set(spec.required), set(spec.optional)
    out: list[Result] = []
    for r in results:
        if r.details.get("required") or r.details.get("optional"):
            out.append(r)  # already judged by this policy
        elif r.validator in required and not r.available:
            out.append(Result(r.validator, "fail", f"required check could not run: {r.message}",
                              {**r.details, "required": True}, available=False))
        elif r.validator in optional and r.status == "fail":
            out.append(Result(r.validator, "warn", f"optional check failed: {r.message}",
                              {**r.details, "optional": True, "original_status": "fail"},
                              r.available))
        else:
            out.append(r)
    seen = {r.validator for r in out}
    for name in spec.required:
        if name not in seen and name != POLICY_ONLY:
            out.append(Result(name, "fail", "required check did not run: the spec does not "
                                            "configure it, or the file could not be read",
                              {"required": True}, available=False))
    return out


def spec_from_policies(policies: list[dict[str, Any]]) -> ValidationSpec:
    """A preset's ordered validator policy as one spec.

    Each policy names a check, whether it is required, and the spec options
    that configure it (``{"validator": "alpha", "required": true, "options":
    {"alpha": "required"}}``). Options for the same field must agree.
    """
    fields: dict[str, Any] = {}
    required: list[str] = []
    optional: list[str] = []
    for index, policy in enumerate(policies):
        name = str(policy.get("validator") or "")
        for key, value in (policy.get("options") or {}).items():
            if key in {"required", "optional"} or key not in ValidationSpec.model_fields:
                raise ValueError(f"validation.{index}: {key!r} is not a validator option")
            if key in fields and fields[key] != value:
                raise ValueError(f"validation.{index}: {key} conflicts with an earlier policy")
            fields[key] = value
        (required if policy.get("required") else optional).append(name)
    return ValidationSpec.model_validate({**fields, "required": required, "optional": optional})


def run(
    path: Path | str, spec: ValidationSpec | dict[str, Any] | None = None, *,
    meta: dict[str, Any] | None = None, asset_id: int | None = None,
    source_asset_ids: tuple[int, ...] = (),
) -> tuple[Verdict, list[Result]]:
    """Validate one file. Never raises for a bad file — that is a `fail`."""
    model = spec if isinstance(spec, ValidationSpec) else ValidationSpec.model_validate(spec or {})
    ctx = ValidationContext(Path(path), model, meta=dict(meta or {}), asset_id=asset_id,
                            source_asset_ids=tuple(source_asset_ids))
    results: list[Result] = []
    # `file` loads the image every other check reads, so it always goes first
    # whatever order checks were registered in.
    checks = sorted(_REGISTRY.items(), key=lambda item: item[0] != "file")
    try:
        for name, check in checks:
            try:
                outcome = check(ctx)
            except Exception as error:  # noqa: BLE001 — a buggy check reports; it never crashes a set
                outcome = Result(name, "fail", f"the {name} check could not run: {error}")
            if outcome is None:
                continue
            results.append(outcome)
            if name == "file" and outcome.status == "fail":
                break
    finally:
        if ctx.image is not None:
            ctx.image.close()
    results = apply_policy(results, model)
    return verdict(results), results


# The built-in checks register themselves; imported last because they import
# `register` and the types above from this package.
from . import identity as _identity  # noqa: F401
from . import image as _builtin  # noqa: F401
