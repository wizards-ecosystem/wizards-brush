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
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

Status = Literal["pass", "warn", "fail"]
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

    def is_empty(self) -> bool:
        return self == ValidationSpec()


@dataclass(frozen=True)
class Result:
    validator: str
    status: Status
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"validator": self.validator, "status": self.status,
                "message": self.message, "details": self.details}


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
    return verdict(results), results


# The built-in checks register themselves; imported last because they import
# `register` and the types above from this package.
from . import image as _builtin  # noqa: F401
