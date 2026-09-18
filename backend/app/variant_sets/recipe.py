"""Variant recipes: the schema, compiling one against the live registry, and
materializing it into planned items.

A recipe is data a person wrote, so everything in it is checked here, before a
set, an item or a job exists: axis names and values, every template, content
maps, per-value parameter overrides, the operation of each stage, masks,
finishing steps, validation and naming. A failure is a :class:`RecipeError`
whose message says what to change.

Parameters are checked against the operation's *registry controls* — the same
descriptors the UI renders — so a recipe can set exactly what the form can,
with the same options and types, and nothing else. They are then sanitized a
second time by the route's own builder when each child is built, so a recipe
can never persist a value an ordinary request could not.

Stages are an ordered list, not a graph. Stage 1 runs on the recipe's sources;
each later stage runs once per combination of its own axes for every item the
stage before it produced, using that item's output as its source. A variant's
key and values accumulate across stages.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..finishing import MAX_STEPS, FinishingError, sanitize_steps
from ..prompt_engine import MAX_PROMPT_CHARS
from ..validators import ValidationSpec
from . import expansion, naming, templates
from .operations import OPERATIONS, Operation, controls_for

MASK_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
MAX_OVERRIDES_PER_VALUE = 16
SEED_MAX = 2**32 - 1


class RecipeError(ValueError):
    """A recipe that cannot run as written. The message is user-facing."""


# ---- the schema ---------------------------------------------------------------------
class AxisSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=32)
    values: list[str | int | float] = Field(max_length=expansion.MAX_VALUES_PER_AXIS)


class NamingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = Field(default="", max_length=naming.MAX_TEMPLATE_CHARS)
    prefix: str = Field(default="", max_length=naming.MAX_PREFIX_CHARS)


class StageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=80)
    operation: str = Field(max_length=40)
    axes: list[AxisSpec] = Field(default_factory=list, max_length=expansion.MAX_AXES_PER_STAGE)
    prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    negative_prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    # Registry control values shared by every variant of this stage.
    params: dict[str, Any] = Field(default_factory=dict)
    # axis -> value -> {control: value}: what one axis value changes.
    value_params: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    mask: str | None = Field(default=None, max_length=40)
    # Extra reference images for a multi-image operation in a later stage; the
    # previous stage's output is always the first input.
    references: list[int] = Field(default_factory=list, max_length=2)
    finishing: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_STEPS)
    validation: ValidationSpec = Field(default_factory=ValidationSpec)
    naming: NamingSpec = Field(default_factory=NamingSpec)


class SeedSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "fixed": every variant shares one seed, so the only difference between two
    # variants is what their axes change. "per_variant": each variant gets its
    # own seed, derived from the base seed and its key — stable across reruns.
    mode: Literal["fixed", "per_variant"] = "fixed"
    value: int = Field(default=-1, ge=-1, le=SEED_MAX)   # -1: resolved once at creation


class MaskRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: int = Field(ge=1)


class RecipeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    sources: list[int] = Field(default_factory=list, max_length=3)
    masks: dict[str, MaskRef] = Field(default_factory=dict, max_length=8)
    # axis -> value -> text: what {{axis}} renders as in prompts. A value with no
    # entry renders as itself.
    content: dict[str, dict[str, str]] = Field(default_factory=dict)
    seed: SeedSpec = Field(default_factory=SeedSpec)
    stages: list[StageSpec] = Field(min_length=1, max_length=expansion.MAX_STAGES)


# ---- compiled form ---------------------------------------------------------------------
@dataclass(frozen=True)
class CompiledStage:
    index: int
    name: str
    operation: Operation
    axes: tuple[expansion.Axis, ...]
    available: tuple[str, ...]          # axes known here: every earlier stage's, then its own
    prompt: str
    negative_prompt: str
    params: dict[str, Any]
    value_params: dict[str, dict[str, dict[str, Any]]]
    mask: str | None
    references: tuple[int, ...]
    finishing: list[dict[str, Any]]
    validation: ValidationSpec
    naming_template: str
    naming_prefix: str
    count: int                          # combinations per parent
    total: int                          # items at this stage


@dataclass(frozen=True)
class CompiledRecipe:
    spec: RecipeSpec
    stages: tuple[CompiledStage, ...]
    content: dict[str, dict[str, str]]
    total: int

    def snapshot(self) -> dict[str, Any]:
        """The normalized recipe, as persisted on a set and in a manifest."""
        data = self.spec.model_dump(mode="json")
        for stage_data, stage in zip(data["stages"], self.stages, strict=True):
            stage_data["axes"] = [{"name": a.name, "values": list(a.values)} for a in stage.axes]
            stage_data["finishing"] = stage.finishing
            stage_data["params"] = stage.params
            stage_data["value_params"] = stage.value_params
        data["content"] = self.content
        return data

    @property
    def axis_names(self) -> list[str]:
        return [a.name for stage in self.stages for a in stage.axes]


@dataclass
class PlannedItem:
    stage: int
    ordinal: int
    key: str
    values: dict[str, str]
    parent: int | None                  # index of the parent in the planned list
    request: dict[str, Any]             # rendered params, before inputs are attached
    output_name: str


def _stage_label(index: int, stage: StageSpec) -> str:
    return f"stage {index + 1}" + (f" ({stage.name})" if stage.name else "")


def check_param(control: Mapping[str, Any], value: Any, where: str) -> Any:
    """Validate one value against its registry control; returns it canonicalised.

    The route's own sanitization still runs when the child is built; this is
    what makes a wrong value an error at submission rather than a silent clamp.
    """
    name, kind = control.get("name"), control.get("type")
    if kind in ("select", "segmented", "aspect"):
        options = [str(o) for o in control.get("options") or []]
        if str(value) not in options:
            raise RecipeError(f"{where}: {name} must be one of {', '.join(options)}")
        return value
    if kind in ("slider", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RecipeError(f"{where}: {name} must be a number")
        low, high = control.get("min"), control.get("max")
        if (low is not None and value < low) or (high is not None and value > high):
            raise RecipeError(f"{where}: {name} must be between {low} and {high}")
        return value
    if kind == "toggle":
        if not isinstance(value, bool):
            raise RecipeError(f"{where}: {name} must be true or false")
        return value
    if kind == "textarea":
        if not isinstance(value, str) or len(value) > MAX_PROMPT_CHARS:
            raise RecipeError(f"{where}: {name} must be text of at most {MAX_PROMPT_CHARS} characters")
        return value
    if kind == "lora":
        if not isinstance(value, list):
            raise RecipeError(f"{where}: {name} must be a list")
        return value
    raise RecipeError(f"{where}: {name} cannot be set by a recipe")


def _check_params(params: Mapping[str, Any], controls: Mapping[str, Mapping[str, Any]],
                  where: str) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for name, value in params.items():
        control = controls.get(name)
        if control is None:
            raise RecipeError(
                f"{where}: '{name}' is not a setting of this operation"
                + (" (the set supplies it)" if name in ("prompt", "negative_prompt", "batch",
                                                        "combinatorial", "seed", "seed_mode")
                   else "")
            )
        clean[name] = check_param(control, value, where)
    return clean


def compile_recipe(
    spec: RecipeSpec, *, specs: Mapping[str, Mapping[str, Any]], cap: int | None = None,
) -> CompiledRecipe:
    """Validate `spec` against the live registry. Raises RecipeError."""
    try:
        stage_axes = [
            tuple(expansion.make_axis(axis.name, axis.values) for axis in stage.axes)
            for stage in spec.stages
        ]
        expansion.check_unique_names(a for axes in stage_axes for a in axes)
        counts = [expansion.count(axes) for axes in stage_axes]
        total = expansion.check_cap(counts, cap if cap is not None else expansion.configured_cap())
    except expansion.ExpansionError as error:
        raise RecipeError(str(error)) from None

    values_of = {a.name: set(a.values) for axes in stage_axes for a in axes}
    content = _check_content(spec.content, values_of)
    for name in spec.masks:
        if not MASK_NAME.fullmatch(name):
            raise RecipeError(f"mask name {name!r} must start with a letter (letters, digits, - and _)")

    totals = expansion.stage_totals(counts)
    compiled: list[CompiledStage] = []
    known: list[str] = []
    for index, (stage, axes) in enumerate(zip(spec.stages, stage_axes, strict=True)):
        where = _stage_label(index, stage)
        op = OPERATIONS.get(stage.operation)
        if op is None or stage.operation not in specs:
            offered = ", ".join(k for k in OPERATIONS if k in specs)
            raise RecipeError(f"{where}: operation '{stage.operation}' is not available here "
                              f"(available: {offered})")
        if index == 0:
            if stage.references:
                raise RecipeError(f"{where}: the first stage takes its images from `sources`")
            if not op.min_sources <= len(spec.sources) <= op.max_sources:
                need = (f"{op.min_sources}" if op.min_sources == op.max_sources
                        else f"{op.min_sources}-{op.max_sources}")
                raise RecipeError(f"{where}: {op.kind} takes {need} source image(s); "
                                  f"the recipe has {len(spec.sources)}")
        else:
            if not op.takes_source:
                raise RecipeError(f"{where}: {op.kind} cannot take the previous stage's output; "
                                  "a later stage needs an operation with an input image")
            if 1 + len(stage.references) > op.max_sources:
                raise RecipeError(f"{where}: {op.kind} takes at most {op.max_sources} image(s), "
                                  "counting the previous stage's output")
        if op.mask == "required":
            if not stage.mask:
                raise RecipeError(f"{where}: {op.kind} needs a mask")
            if stage.mask not in spec.masks:
                raise RecipeError(f"{where}: mask '{stage.mask}' is not defined in `masks`")
        elif stage.mask:
            raise RecipeError(f"{where}: {op.kind} does not use a mask")

        own = [a.name for a in axes]
        available = (*known, *own)
        later = {a.name for axes_ in stage_axes[index + 1:] for a in axes_}
        try:
            templates.check(stage.prompt, field=f"{where} prompt", available=available, later=later)
            templates.check(stage.negative_prompt, field=f"{where} negative prompt",
                            available=available, later=later)
            naming_template = stage.naming.template.strip() or naming.default_template(list(available))
            templates.check(naming_template, field=f"{where} naming", available=available,
                            later=later, builtins=True)
            templates.check(stage.naming.prefix, field=f"{where} naming prefix", available=(),
                            builtins=False)
        except templates.TemplateError as error:
            raise RecipeError(str(error)) from None

        controls = controls_for(specs[stage.operation])
        params = _check_params(stage.params, controls, where)
        value_params = _check_value_params(stage.value_params, controls, values_of,
                                           set(available), where)
        try:
            finishing = sanitize_steps(stage.finishing)
        except FinishingError as error:
            raise RecipeError(f"{where}: {error}") from None
        compiled.append(CompiledStage(
            index=index, name=stage.name, operation=op, axes=axes, available=tuple(available),
            prompt=stage.prompt, negative_prompt=stage.negative_prompt, params=params,
            value_params=value_params, mask=stage.mask, references=tuple(stage.references),
            finishing=finishing, validation=stage.validation,
            naming_template=naming_template, naming_prefix=stage.naming.prefix,
            count=counts[index], total=totals[index],
        ))
        known.extend(own)
    return CompiledRecipe(spec=spec, stages=tuple(compiled), content=content, total=total)


def _check_content(content: Mapping[str, Mapping[str, str]],
                   values_of: Mapping[str, set[str]]) -> dict[str, dict[str, str]]:
    clean: dict[str, dict[str, str]] = {}
    for axis, mapping in content.items():
        if axis not in values_of:
            raise RecipeError(f"content is given for '{axis}', which is not an axis")
        clean[axis] = {}
        for value, text in mapping.items():
            if value not in values_of[axis]:
                raise RecipeError(f"content for '{axis}': {value!r} is not one of its values")
            if not isinstance(text, str) or len(text) > MAX_PROMPT_CHARS:
                raise RecipeError(f"content for {axis}={value} must be text of at most "
                                  f"{MAX_PROMPT_CHARS} characters")
            if "{{" in text or "}}" in text:
                raise RecipeError(
                    f"content for {axis}={value} contains '{{{{' or '}}}}'; content is inserted "
                    "as written and never expanded again"
                )
            clean[axis][value] = text
    return clean


def _check_value_params(
    value_params: Mapping[str, Mapping[str, Mapping[str, Any]]],
    controls: Mapping[str, Mapping[str, Any]], values_of: Mapping[str, set[str]],
    available: set[str], where: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    clean: dict[str, dict[str, dict[str, Any]]] = {}
    owner: dict[str, str] = {}
    for axis, per_value in value_params.items():
        if axis not in available:
            raise RecipeError(f"{where}: settings are given for '{axis}', which is not an axis "
                              "known at this stage")
        clean[axis] = {}
        for value, overrides in per_value.items():
            if value not in values_of[axis]:
                raise RecipeError(f"{where}: '{axis}' has no value {value!r}")
            if len(overrides) > MAX_OVERRIDES_PER_VALUE:
                raise RecipeError(f"{where}: at most {MAX_OVERRIDES_PER_VALUE} settings per value")
            for name in overrides:
                # Two axes both setting one control would make the result depend
                # on which is applied last. Refuse rather than pick silently.
                if owner.setdefault(name, axis) != axis:
                    raise RecipeError(f"{where}: '{name}' is set by both '{owner[name]}' and "
                                      f"'{axis}'; only one axis may set a setting")
            clean[axis][value] = _check_params(overrides, controls, f"{where} {axis}={value}")
    return clean


# ---- materialization -------------------------------------------------------------------------
def derive_seed(base: int, stage: int, key: str) -> int:
    """A per-variant seed that depends only on the base seed and the variant's
    identity, so adding or reordering values never changes an existing one."""
    digest = hashlib.blake2b(f"{base}:{stage}:{key}".encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big")


def seed_for(recipe: CompiledRecipe, base_seed: int | None, stage: int, key: str) -> int | None:
    if base_seed is None:
        return None
    return base_seed if recipe.spec.seed.mode == "fixed" else derive_seed(base_seed, stage, key)


def render_request(content: Mapping[str, Mapping[str, str]], stage: CompiledStage | StageSpec,
                   values: Mapping[str, str], seed: int | None) -> dict[str, Any]:
    """The params one variant asks for, before its input images are attached.

    Takes a compiled stage or a stored snapshot's stage alike: rendering reads
    only the templates, params, per-value settings and finishing, all of which
    a snapshot records exactly as they were validated.
    """
    slots = {axis: templates.Slot(value, content.get(axis, {}).get(value))
             for axis, value in values.items()}
    request: dict[str, Any] = dict(stage.params)
    for axis, value in values.items():
        request.update(stage.value_params.get(axis, {}).get(value, {}))
    request.update({
        "prompt": templates.render(stage.prompt, slots),
        "negative_prompt": templates.render(stage.negative_prompt, slots),
        "batch": 1,
        "combinatorial": False,
        "seed_mode": "fixed",
    })
    if seed is not None:
        request["seed"] = seed
    if stage.finishing:
        request["finish_steps"] = stage.finishing
    return request


def _join(parent: str, own: str) -> str:
    return ",".join(part for part in (parent, own) if part)


def materialize(recipe: CompiledRecipe, *, base_seed: int | None) -> tuple[
        list[PlannedItem], dict[int, dict[str, list[str]]]]:
    """Every item of every stage, in order, plus naming collisions per stage.

    Raises RecipeError when a rendered prompt exceeds the prompt limit: cutting
    it short would silently change what that variant asks for.
    """
    planned: list[PlannedItem] = []
    collisions: dict[int, dict[str, list[str]]] = {}
    parents: list[int | None] = [None]
    for stage in recipe.stages:
        width = len(str(stage.total))
        combos = list(expansion.combinations(stage.axes))
        stage_items: list[int] = []
        names: dict[str, str] = {}
        for parent_index in parents:
            parent = planned[parent_index] if parent_index is not None else None
            for combo in combos:
                values = {**(parent.values if parent else {}), **combo.as_dict()}
                key = _join(parent.key if parent else "", combo.key)
                ordinal = (parent.ordinal * stage.count if parent else 0) + combo.index
                seed = seed_for(recipe, base_seed, stage.index, key)
                request = render_request(recipe.content, stage, values, seed)
                for field in ("prompt", "negative_prompt"):
                    if len(request[field]) > MAX_PROMPT_CHARS:
                        raise RecipeError(
                            f"{_stage_label(stage.index, recipe.spec.stages[stage.index])}: the "
                            f"{field.replace('_', ' ')} for {key or 'the variant'} is "
                            f"{len(request[field])} characters; the limit is {MAX_PROMPT_CHARS}"
                        )
                try:
                    name = naming.render_name(
                        stage.naming_template, values, prefix=stage.naming_prefix,
                        builtins={"_index": str(ordinal + 1).zfill(width),
                                  "_key": key or "variant", "_stage": str(stage.index + 1)},
                    )
                except naming.NamingError as error:
                    raise RecipeError(f"{key or 'variant'}: {error}") from None
                names[key] = name
                stage_items.append(len(planned))
                planned.append(PlannedItem(stage.index, ordinal, key, values, parent_index,
                                           request, name))
        found = naming.find_collisions(names)
        if found:
            collisions[stage.index] = found
        parents = list(stage_items)
    return planned, collisions


def describe_collisions(collisions: Mapping[int, Mapping[str, Sequence[str]]]) -> str:
    stage, found = next(iter(collisions.items()))
    name, keys = next(iter(found.items()))
    more = sum(len(f) for f in collisions.values()) - 1
    return (f"stage {stage + 1}: {len(keys)} variants would share the file name {name!r} "
            f"({'; '.join(keys[:3])}{'…' if len(keys) > 3 else ''})"
            + (f", and {more} more name(s) collide" if more > 0 else "")
            + ". Include every axis in the naming template.")
