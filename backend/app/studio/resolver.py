"""Resolve a preset request into an immutable execution plan. No side effects.

One function, `resolve(request, context)`, serves the UI, HTTP, MCP, saved
generators and Variant Set compilation. It reads (presets, resources, the live
registry, cached target state, gallery assets) and never writes: no job, no
download, no upload, no provider call, no change to a saved preset.

Precedence, lowest to highest, each value carrying its origin:

1. installed registry constraints and defaults (`registry_default`), with
   `defaults_by` maps applied (`dependent_default`);
2. the preset revision's settings (`preset`);
3. explicit caller overrides (`override`), validated strictly, never clamped;
4. execution-only values (`runtime`): the resolved target, model slot, seed,
   and whatever the job builder normalizes for the chosen device.

The builder step matters: the image and video builders (`common_params`) turn
quality into steps, aspect into a size bucket, cap batch by a pixel budget and
apply speed-mode recipes. The plan runs those same builders and records every
value they changed in `normalized`, so the preview is the request that runs.

Creative validity (a malformed request, an invalid explicit value, an unknown
style) raises `ResolveError`. Execution readiness (weights not downloaded, the
worker offline, an adapter that does not fit this model) is reported in
`readiness` on a complete plan, never as invalidity.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..problems import RequestError
from . import contracts

CREATIVE_DOMAIN = "wizards-brush/creative-request"
EXECUTION_DOMAIN = "wizards-brush/execution-plan"
MATERIAL_DOMAIN = "wizards-brush/material-plan"

# Settings the resolver owns: each has its own request field, or follows from
# the chosen model and target, so they cannot be set as plain settings.
MANAGED = frozenset({"prompt", "negative_prompt", "loras", "finish_steps", "model_variant",
                     "engine", "seed"})
# Keys a builder adds that are inputs (paths) or transport, never plan content.
_PATH_KEYS = frozenset({"image_path", "image_paths", "mask_path", "last_image_path",
                        "request_id"})
_IMAGE_KINDS = frozenset({"image_local", "image_colab", "img2img", "inpaint", "inpaint_remote",
                          "outpaint", "image_edit", "control_local"})
_VIDEO_ENGINE = {"video": "wan", "video_hunyuan": "hunyuan", "video_ltx": "ltx"}
_NORMALIZED_WHY = {
    "steps": "the quality tier sets the step count unless quality is Custom",
    "width": "the aspect sets the size, snapped to this model's size bucket",
    "height": "the aspect sets the size, snapped to this model's size bucket",
    "batch": "the batch is capped by this device's pixel budget",
    "guidance": "a fixed recipe (speed mode) sets guidance",
    "negative_prompt": "the builder's negative-prompt rules apply (auto negative, finish, "
                       "speed mode)",
    "quality": "a fixed recipe (speed mode) sets quality",
    "num_frames": "this video engine needs a frame count of the form 4k+1",
}
_SEP = re.compile(r"(?:\s*,\s*){2,}")


class ResolveError(RequestError):
    """A request that is not creatively valid. Always names a field."""


def _bad(detail: str, field: str, code: str = "invalid_request", **extra: Any) -> ResolveError:
    return ResolveError(400, detail, code=code, field=field, **extra)


# ---- the request ---------------------------------------------------------------------------
class AdapterOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    text_encoder_strength: float | None = Field(default=None, ge=-2.0, le=2.0)
    trigger: str = Field(default="", max_length=200)


class StyleText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=48)
    text: str = Field(min_length=1, max_length=600)


class ResolveInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images: list[int] = Field(default_factory=list, max_length=8)
    mask: int | None = None
    last_frame: int | None = None
    # role -> asset ids, for the roles the preset declares.
    references: dict[str, list[int]] = Field(default_factory=dict)


class ResolveRequest(BaseModel):
    """The resolve input. Omitted fields take the preset's (or the registry's)
    value; a field that is present is an explicit choice, even when it equals
    the default, and keeps that provenance."""

    model_config = ConfigDict(extra="forbid")

    preset_id: str | None = Field(default=None, max_length=96)
    revision: int | None = Field(default=None, ge=1)
    operation: contracts.Operation | None = None
    model: str | None = Field(default=None, max_length=320)
    adapters: list[AdapterOverride] | None = Field(default=None, max_length=contracts.MAX_ADAPTERS)
    prompt: str = Field(default="", max_length=8000)
    negative_prompt: str | None = Field(default=None, max_length=8000)
    styles: list[str] | None = Field(default=None, max_length=32)
    style_fragments: list[StyleText] | None = Field(default=None, max_length=32)
    enhance: bool | None = None
    notes: str = Field(default="", max_length=2000)
    settings: dict[str, Any] = Field(default_factory=dict)
    finish: list[dict[str, Any]] | None = Field(default=None, max_length=contracts.MAX_FINISH_STEPS)
    inputs: ResolveInputs = Field(default_factory=ResolveInputs)
    target: contracts.Target = "auto"
    allow_remote: bool = Field(
        default=False,
        description="Let auto routing choose a connected remote worker, and send the inputs "
                    "this request names to it. Never starts or rents hardware.")
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    capability_revision: str | None = Field(default=None, max_length=80)


# ---- the context ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Slot:
    target: str          # "local" | "remote"
    role: str            # "image" | "edit" | "video"
    variant: str
    repo: str
    gated: bool = False


@dataclass
class Context:
    """Everything resolve reads. Tests pass fakes; `live()` reads the app."""

    preset: Callable[[str, int | None], dict[str, Any]]
    resource: Callable[[str], Any]
    adapter_path: Callable[[str], str | None]
    specs: Callable[[], dict[str, dict[str, Any]]]
    slots: Callable[[], list[Slot]]
    resource_for_repo: Callable[[str], str]
    asset: Callable[[int], Any]
    asset_hash: Callable[[Any], str]
    asset_file_exists: Callable[[Any], bool]
    local_unavailable: Callable[[], str]
    local_model_status: Callable[[str], str]
    remote_state: Callable[[], dict[str, Any]]
    finishing: Callable[[], list[dict[str, Any]]]
    hf_token: Callable[[], bool]
    extra: dict[str, Any] = field(default_factory=dict)
    # The user's routing policy (studio/targets.py); the default is the
    # shipped one: remote allowed, never chosen by Auto, inputs never sent
    # without the request saying so.
    policy: Callable[[], dict[str, bool]] = field(default=lambda: {
        "remote_enabled": True, "auto_may_use_remote": False, "send_inputs_to_remote": False})
    # This machine's measured hardware: {"vram_gb": float | None, ...}.
    hardware: Callable[[], dict[str, Any]] = field(default=lambda: {"vram_gb": None})
    # (needed, available) bytes when host RAM cannot back loading a repo.
    memory_shortfall: Callable[[str], tuple[int, int] | None] = field(default=lambda _repo: None)
    # The repo resident on this machine's GPU now, if any.
    resident: Callable[[], str | None] = field(default=lambda: None)
    # The app-wide capability revision, when the caller knows it.
    capability_revision: Callable[[], str] | None = None


def live() -> Context:
    """The running app, read without network access: the Remote GPU's state is
    the last health answer the app already holds."""
    from .. import db
    from ..backends.local import _cache_status, unavailable_reason
    from ..config import settings
    from ..finishing import describe
    from ..remote_gpu_client import last_health
    from ..resources import catalogue as resources
    from ..resources import store
    from ..variant_sets.operations import registry_specs
    from . import catalogue

    def slots() -> list[Slot]:
        return [Slot(s.lane, s.role, s.variant, s.repo, s.gated)
                for s in resources.configured_slots()]

    def asset_hash(row: Any) -> str:
        content = db.content_of(int(row.id)) if row is not None and row.id else None
        return f"blake2b:{content.hash}" if content is not None and content.hash else ""

    def file_exists(row: Any) -> bool:
        from pathlib import Path

        return bool(row is not None and row.path and Path(row.path).exists())

    from .. import hardware
    from ..generators.vram import host_ram_shortfall
    from . import targets

    return Context(
        preset=catalogue.get, resource=store.get, adapter_path=resources.library_path,
        specs=registry_specs, slots=slots, resource_for_repo=resources.model_resource_id,
        asset=db.get_asset, asset_hash=asset_hash, asset_file_exists=file_exists,
        local_unavailable=unavailable_reason,
        local_model_status=lambda repo: _cache_status(repo)[0],
        remote_state=last_health, finishing=describe,
        hf_token=lambda: bool(settings.hf_token),
        policy=targets.policy, hardware=hardware.describe,
        memory_shortfall=lambda repo: host_ram_shortfall(repo, settings.local_quant),
        resident=_resident_model,
    )


def _resident_model() -> str | None:
    try:
        from ..generators.local_image import resident_model

        return resident_model() or None
    except Exception:  # noqa: BLE001 — a probe of the GPU state is advisory here
        return None


# ---- small helpers -------------------------------------------------------------------------
def _issue(code: str, message: str, *, field: str | None = None, action: str | None = None,
           blocking: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {"code": code, "message": message, "blocking": blocking}
    if field:
        out["field"] = field
    if action:
        out["action"] = action
    return out


def _check(control: Mapping[str, Any], value: Any, values: Mapping[str, Any],
           known: Mapping[str, Mapping[str, Any]], field: str) -> Any:
    from ..controls import ControlError, check_value, effective

    try:
        return check_value(effective(control, {**values, control["name"]: value}, known), value)
    except ControlError as error:
        raise ResolveError(400, str(error), code=error.code, field=field,
                           valid_options=error.valid_options,
                           suggestions=error.suggestions) from None


def _visible(control: Mapping[str, Any], values: Mapping[str, Any]) -> bool:
    rule = control.get("show_if")
    if not isinstance(rule, Mapping):
        return True
    want = rule.get("equals")
    got = values.get(str(rule.get("field")))
    return got in want if isinstance(want, list) else got == want


def _render(template: str, slots: Mapping[str, str]) -> str:
    """The fixed grammar, then separator clean-up.

    An empty placeholder takes its adjacent ", " with it: "{{triggers}},
    {{prompt}}" with no triggers is "a fox", not ", a fox". Commas inside the
    caller's own text are left alone.
    """
    from ..variant_sets.templates import Slot as TemplateSlot
    from ..variant_sets.templates import render

    text = render(template, {k: TemplateSlot(value=v) for k, v in slots.items()})
    text = _SEP.sub(", ", text)
    return text.strip().strip(",").strip()


# ---- resolve -------------------------------------------------------------------------------
def resolve(request: ResolveRequest, context: Context | None = None, *,
            seed_origin: Literal["override", "runtime"] = "override") -> dict[str, Any]:
    """The complete candidate plan for `request`. Raises ResolveError when the
    request is not creatively valid; reports readiness otherwise."""
    try:
        return _resolve(request, context or live(), seed_origin)
    except contracts.ContractError as error:
        raise _bad(str(error), error.field or "request", code=error.code) from None


def _resolve(request: ResolveRequest, ctx: Context,
             seed_origin: Literal["override", "runtime"]) -> dict[str, Any]:
    if request.seed is None:
        raise _bad("resolve needs a concrete seed; prepare the request first so a random seed "
                   "is chosen once and kept", "seed", code="seed_required")
    preset, preset_ref = _load_preset(request, ctx)
    operation = _operation(request, preset)
    model_id, model_origin = _model(request, preset)
    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    model_row = ctx.resource(model_id)
    model_facts = _model_facts(model_id, model_row)
    candidates = _candidates(operation, model_id, ctx)
    rules = ctx.policy()
    may_route_remote = bool(rules.get("remote_enabled", True)) and (
        request.allow_remote or bool(rules.get("auto_may_use_remote", False)))
    target, why = _choose_target(request, candidates, may_route_remote)
    if target == "remote" and not rules.get("remote_enabled", True):
        blockers.append(_issue(
            "remote_disabled", "remote use is turned off in the routing policy",
            field="target", action="Run locally, or enable remote use in Settings."))
    elif request.target == "auto" and target == "remote" and not may_route_remote:
        blockers.append(_issue(
            "remote_not_allowed",
            "only the Remote GPU runs this, and neither this request nor the routing policy "
            "lets Auto send work there", field="allow_remote",
            action="Set allow_remote, allow Auto to use remote in Settings, or choose the "
                   "Remote target explicitly."))
    chosen = next((c for c in candidates if c["target"] == target and c["slot"]), None)
    # No slot serves this model on the chosen target: there is no kind to run.
    # Building one anyway would run the lane's default model in its place.
    kind = (contracts.KIND_FOR.get((operation, target))
            if chosen is not None and target is not None else None)
    if chosen is None:
        offered = [c for c in candidates if c["slot"]]
        blockers.append(_issue(
            "target_unavailable",
            f"no {target or 'configured'} target here runs {model_facts['repo'] or model_id} "
            f"for {operation.replace('_', ' ')}" + (
                f"; it is configured for {', '.join(c['target'] for c in offered)}"
                if offered else ""),
            field="target",
            action=("Configure this model in a slot on the chosen target, or choose a target "
                    "that has it."),
        ))
    else:
        blockers.extend(i for i in chosen["issues"] if i["blocking"])
        warnings.extend(i for i in chosen["issues"] if not i["blocking"])
    slot: Slot | None = chosen["slot"] if chosen else None
    specs = ctx.specs()
    spec = specs.get(kind or "") if kind else None
    if kind and spec is None:
        blockers.append(_issue("kind_unavailable", f"{kind} is not offered here right now",
                               field="target"))
    known: dict[str, dict[str, Any]] = {str(c["name"]): c for c in (spec or {}).get("controls", [])}

    values, origins, changed = _settings(request, preset, known, slot, model_origin, warnings)
    prompt = _prompt(request, preset, values, origins)
    adapters, loras = _adapters(request, preset, ctx, model_facts, slot, target, known, blockers,
                                warnings)
    finish = _finish(request, preset, ctx, warnings)
    inputs, paths, mask_path, last_path = _inputs(request, preset, kind, specs, ctx)
    origins["seed"] = seed_origin
    sent = len(inputs["images"]) + sum(len(v) for v in inputs["references"].values()) + \
        (1 if inputs["mask"] else 0) + (1 if inputs["last_frame"] else 0)
    if target == "remote" and sent and not (request.allow_remote
                                            or rules.get("send_inputs_to_remote", False)):
        blockers.append(_issue(
            "input_transfer_not_allowed",
            f"this request would send {sent} image(s) to the Remote GPU, and neither the request "
            "nor the routing policy allows that", field="allow_remote",
            action="Set allow_remote for this request, or allow sending inputs in Settings."))

    raw: dict[str, Any] = {
        **values, "prompt": prompt["positive"], "negative_prompt": prompt["negative"],
        "raw_prompt": request.prompt, "style_ids": prompt["style_ids"], "seed": request.seed,
    }
    if loras:
        raw["loras"] = loras
    if finish:
        raw["finish_steps"] = finish
    effective, normalized = _effective(kind if spec is not None else None, raw, paths, mask_path,
                                       last_path, origins, warnings)
    if loras and contracts.canonical_json(effective.get("loras")) != contracts.canonical_json(loras):
        # The builder could not read an adapter the catalogue listed (moved or
        # replaced since the last scan). Running would silently drop it.
        for entry in adapters:
            if entry["status"] == "ok":
                entry["status"] = "blocked"
        blockers.append(_issue(
            "resource_not_ready", "an adapter file could not be read from the LoRA library",
            field="adapters", action="Rescan the LoRA library, or re-import the adapter."))
    seeds = _seed_mapping(request.seed, effective or raw)

    remote = ctx.remote_state()
    capability = ctx.capability_revision() if ctx.capability_revision else contracts.digest({
        "kind": kind, "controls": sorted(known),
        "options": {n: c.get("options") for n, c in known.items() if c.get("options")},
        "slot": slot.__dict__ if slot else None,
        "local": ctx.local_unavailable(), "remote_features": sorted(remote.get("features") or []),
        "remote_build": remote.get("build") or "",
    }, domain="wizards-brush/capability")[7:23]
    if request.capability_revision and request.capability_revision != capability:
        warnings.append(_issue("capabilities_changed",
                               "what this install can run changed since the last preview",
                               blocking=False))

    creative = {
        "operation": operation,
        "model": {"resource": model_id, "verification": model_facts["verification"]},
        "adapters": [{k: a[k] for k in ("resource", "strength", "text_encoder_strength")}
                     for a in adapters],
        "prompt": {"positive": prompt["positive"], "negative": prompt["negative"]},
        "settings": {k: v for k, v in values.items() if k not in {"model_variant", "engine"}},
        "inputs": inputs,
        "seed": request.seed,
        "finish": finish,
    }
    creative_hash = contracts.digest(creative, domain=CREATIVE_DOMAIN)
    # What would actually run: the creative request on a concrete target, kind
    # and slot, with the builder's effective params. The execution hash adds
    # the capability revision; the material hash leaves it out, so a change in
    # what the target offers that does not change this plan is not staleness.
    material = {
        "creative": creative_hash, "target": target, "kind": kind,
        "slot": {"variant": slot.variant, "repo": slot.repo} if slot else None,
        "effective": effective,
        "adapter_installs": [a.get("library_path") for a in adapters],
    }
    material_hash = contracts.digest(material, domain=MATERIAL_DOMAIN)
    execution_hash = contracts.digest({**material, "capability": capability},
                                      domain=EXECUTION_DOMAIN)

    return {
        "schema": contracts.PLAN_SCHEMA,
        "schema_version": contracts.PLAN_SCHEMA_VERSION,
        "resolver_version": contracts.RESOLVER_VERSION,
        "preset": preset_ref,
        "operation": operation,
        "target": {"requested": request.target, "resolved": target, "reason": why,
                   "allow_remote": request.allow_remote,
                   "candidates": [{"target": c["target"], "kind": c["kind"],
                                   "available": bool(c["slot"]) and not any(
                                       i["blocking"] for i in c["issues"]),
                                   "issues": c["issues"]} for c in candidates]},
        "kind": kind,
        "model": {**model_facts, "variant": slot.variant if slot else None, "origin": model_origin},
        "adapters": adapters,
        "prompt": prompt,
        "settings": values,
        "provenance": origins,
        "changed_dependencies": changed,
        "effective_params": effective,
        "normalized": normalized,
        "inputs": inputs,
        "seed": {"base": request.seed, "origin": seed_origin,
                 "mode": (effective or raw).get("seed_mode", "increment"), "items": seeds},
        "finish": finish,
        "validation": [v.model_dump() for v in (preset.validation if preset else [])],
        "warnings": warnings,
        "readiness": _readiness(blockers, warnings),
        "capability_revision": capability,
        "fingerprint": {"model": model_facts["verification"],
                        "adapters": [a["verification"] for a in adapters],
                        "runtime": "recorded at execution"},
        "hashes": {"creative_request_hash": creative_hash,
                   "execution_plan_hash": execution_hash,
                   "material_hash": material_hash},
    }


# ---- steps ---------------------------------------------------------------------------------
def _load_preset(request: ResolveRequest, ctx: Context
                 ) -> tuple[contracts.PresetDefinition | None, dict[str, Any] | None]:
    if not request.preset_id:
        if request.revision is not None:
            raise _bad("revision needs preset_id", "revision")
        if request.styles:
            raise _bad("styles name a preset's fragments; without a preset send style_fragments",
                       "styles")
        return None, None
    stored = ctx.preset(request.preset_id, request.revision)
    try:
        preset = contracts.parse_preset(stored["definition"])
    except contracts.ContractError as error:
        raise _bad(f"the stored preset no longer validates: {error}", "preset_id",
                   code="unsupported_schema") from None
    return preset, {"id": preset.id, "revision": int(stored["revision"]),
                    "definition_hash": stored.get("definition_hash") or preset.definition_hash(),
                    "origin": preset.origin}


def _operation(request: ResolveRequest, preset: contracts.PresetDefinition | None) -> str:
    if preset is not None:
        if request.operation and request.operation != preset.operation:
            raise _bad(f"this preset runs {preset.operation}, not {request.operation}",
                       "operation", code="operation_mismatch")
        return preset.operation
    if not request.operation:
        raise _bad("name an operation, or a preset that has one", "operation",
                   code="missing_input", valid_options=list(contracts.OPERATIONS))
    return request.operation


def _model(request: ResolveRequest, preset: contracts.PresetDefinition | None) -> tuple[str, str]:
    if request.model:
        rid = contracts.check_resource_id(request.model, "model")
        origin = "override" if preset is None or rid != preset.model.resource else "preset"
        return rid, origin
    if preset is None:
        raise _bad("name a model resource, or a preset that has one", "model",
                   code="missing_input")
    return preset.model.resource, "preset"


def _model_facts(model_id: str, row: Any) -> dict[str, Any]:
    repo, revision = "", ""
    if model_id.startswith("hf:"):
        repo, _, revision = model_id[3:].rpartition("@")
    from ..modelprobe import family_of_model

    if row is not None:
        verification = row.verification
        family = row.family or family_of_model(repo)
    else:
        verification = "unverified"
        family = family_of_model(repo) if repo else "unknown"
    if revision == "unpinned":
        verification = "legacy_unverified"
    return {"resource": model_id, "repo": repo, "revision": "" if revision == "unpinned" else revision,
            "family": family, "verification": verification, "known": row is not None}


def _serves(slot: Slot, operation: str, target: str, model_id: str, ctx: Context) -> bool:
    if slot.target != target or ctx.resource_for_repo(slot.repo) != model_id:
        return False
    if operation == "edit" or (operation == "inpaint" and target == "remote"):
        return slot.role == "edit"
    if operation in {"text_to_video", "image_to_video", "long_video"}:
        return slot.role == "video" and (operation != "long_video" or slot.variant == "video")
    if operation == "control":
        from ..config import settings

        return slot.role == "image" and slot.repo == settings.local_image_model
    return slot.role == "image"


def _candidates(operation: str, model_id: str, ctx: Context) -> list[dict[str, Any]]:
    out = []
    slots = ctx.slots()
    for target in ("local", "remote"):
        kind = contracts.KIND_FOR.get((operation, target))
        if kind is None:
            continue
        slot = next((s for s in slots if _serves(s, operation, target, model_id, ctx)), None)
        issues: list[dict[str, Any]] = []
        if slot is None:
            near = [s for s in slots if s.target == target and model_id.startswith(
                f"hf:{s.repo}@")]
            if near:
                issues.append(_issue(
                    "revision_mismatch",
                    f"{near[0].repo} is configured here at a different revision than the "
                    f"plan names ({model_id.rsplit('@', 1)[1]})",
                    field="model", action="Pin the slot to this revision, or upgrade the plan."))
        elif target == "local":
            if reason := ctx.local_unavailable():
                issues.append(_issue("target_unavailable", reason, field="target"))
            hw = ctx.hardware()
            if hw.get("vram_gb") is None:
                issues.append(_issue(
                    "needs_check", "this machine's GPU memory has not been measured yet, so "
                    "whether this fits is unknown", field="target", blocking=False,
                    action="Wait for the startup device probe, or run it to find out."))
            # Advisory, as the loader treats it: it waits for memory before it
            # gives up, and a resident model needs no load at all. An estimate
            # is never a refusal; a measured failure is the job's to report.
            if ctx.resident() != slot.repo and (shortfall := ctx.memory_shortfall(slot.repo)):
                need, free = shortfall
                issues.append(_issue(
                    "insufficient_memory",
                    f"loading {slot.repo} is estimated to need about {need / 1024 ** 3:.1f} GB of "
                    f"free RAM and {free / 1024 ** 3:.1f} GB is available now; the load waits "
                    "for memory and fails if it never frees", field="target", blocking=False,
                    action="Close other programs, choose a smaller model, or run on Remote."))
            status = ctx.local_model_status(slot.repo)
            if status != "ready":
                if slot.gated and not ctx.hf_token():
                    issues.append(_issue(
                        "access_required",
                        f"{slot.repo} is gated: accept its licence on Hugging Face and set "
                        "HF_TOKEN before the first download", field="model",
                        action="Accept the model's licence and set HF_TOKEN."))
                else:
                    issues.append(_issue(
                        "resource_not_ready",
                        f"{slot.repo} is not downloaded on this machine yet ({status}); the "
                        "first local run downloads it", field="model", blocking=False,
                        action="Download the model first to avoid a long first run."))
        else:
            remote = ctx.remote_state()
            if not remote.get("connected"):
                issues.append(_issue(
                    "target_unavailable", "the Remote GPU is not connected", field="target",
                    action="Start or connect the Remote GPU worker (this request never "
                           "starts paid hardware)."))
        out.append({"target": target, "kind": kind, "slot": slot, "issues": issues})
    return out


def _choose_target(request: ResolveRequest, candidates: list[dict[str, Any]],
                   may_route_remote: bool = False) -> tuple[str | None, str]:
    by_target = {c["target"]: c for c in candidates}
    if request.target in ("local", "remote"):
        if request.target not in by_target:
            return request.target, f"{request.target} was requested; it does not offer this operation"
        return request.target, f"{request.target} was requested"

    def ready(c: dict[str, Any]) -> bool:
        return bool(c["slot"]) and not any(i["blocking"] for i in c["issues"])

    local = by_target.get("local")
    remote = by_target.get("remote")
    if local and ready(local):
        return "local", "auto: this machine can run it"
    if remote and ready(remote):
        if may_route_remote:
            return "remote", "auto: this machine cannot run it and remote use is allowed"
        if local and local["slot"]:
            return "local", ("auto: the local slot is not ready; remote is ready but not "
                             "allowed for this request")
        return "remote", "auto: only the remote target runs it, and remote use is not allowed"
    if local and local["slot"]:
        return "local", "auto: no target is ready; local is configured for this model"
    if remote and remote["slot"]:
        return "remote", "auto: no target is ready; remote is configured for this model"
    first = candidates[0]["target"] if candidates else None
    return first, "auto: no configured target runs this model"


def _settings(request: ResolveRequest, preset: contracts.PresetDefinition | None,
              known: dict[str, dict[str, Any]], slot: Slot | None, model_origin: str,
              warnings: list[dict[str, Any]]
              ) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    values: dict[str, Any] = {}
    origins: dict[str, str] = {}
    changed: list[dict[str, Any]] = []
    for name, control in known.items():
        if name in MANAGED or "default" not in control:
            continue
        values[name] = control["default"]
        origins[name] = "registry_default"
    if slot is not None and "model_variant" in known:
        values["model_variant"] = slot.variant
        origins["model_variant"] = "runtime"
    if slot is not None and "engine" in known and slot.variant in _VIDEO_ENGINE:
        values["engine"] = _VIDEO_ENGINE[slot.variant]
        origins["engine"] = "runtime"

    def dependent() -> None:
        for name, control in known.items():
            rule = control.get("defaults_by")
            if name in MANAGED or not isinstance(rule, Mapping):
                continue
            if origins.get(name) not in {"registry_default", "dependent_default"}:
                continue
            mapped = (rule.get("map") or {}).get(str(values.get(str(rule.get("field")))))
            if mapped is not None:
                values[name] = mapped
                origins[name] = "dependent_default"

    dependent()
    model_changed = preset is not None and model_origin == "override"
    for name, value in (preset.settings if preset else {}).items():
        authored = known.get(name)
        if name in MANAGED:
            raise _bad(f"the preset sets {name}, which is chosen another way",
                       f"preset.settings.{name}")
        if authored is None:
            if known:
                warnings.append(_issue("setting_not_applicable",
                                       f"the preset's {name} does not apply here", blocking=False,
                                       field=f"settings.{name}"))
            continue
        rule = authored.get("defaults_by")
        if model_changed and isinstance(rule, Mapping) and rule.get("field") == "model_variant":
            changed.append({"field": name, "preset": value, "now": values.get(name),
                            "because": "the model differs from the preset's, so its "
                                       "model-tuned value no longer applies"})
            continue
        values[name] = _check(authored, value, values, known, f"preset.settings.{name}")
        origins[name] = "preset"
    for name, value in request.settings.items():
        if name in MANAGED:
            field_for = {"prompt": "prompt", "negative_prompt": "negative_prompt",
                         "loras": "adapters", "finish_steps": "finish", "model_variant": "model",
                         "engine": "model", "seed": "seed"}[name]
            raise _bad(f"{name} is not a setting here; send it as `{field_for}`",
                       f"settings.{name}", code="unknown_setting")
        chosen = known.get(name)
        if chosen is None:
            if not known:
                continue  # no kind to judge against; readiness already says why
            valid = sorted(n for n in known if n not in MANAGED)
            raise _bad(f"'{name}' is not a setting of this operation here (valid: "
                       f"{', '.join(valid)})", f"settings.{name}", code="unknown_setting",
                       valid_options=valid)
        values[name] = _check(chosen, value, values, known, f"settings.{name}")
        origins[name] = "override"
    dependent()
    for name, control in known.items():
        rule = control.get("constrained_by")
        if not isinstance(rule, Mapping) or name in MANAGED:
            continue
        want = rule.get("equals")
        got = values.get(str(rule.get("field")))
        if not (got in want if isinstance(want, list) else got == want):
            continue
        forced = rule.get("value")
        if origins.get(name) == "override" and values.get(name) != forced:
            raise _bad(f"{name} is fixed to {forced!r} while {rule.get('field')} is "
                       f"{got!r}: {rule.get('reason') or ''}".strip(), f"settings.{name}",
                       code="constrained", suggestions=[f"Remove {name}, or change {rule.get('field')}."])
        values[name] = forced
        origins[name] = "runtime"
    for name, control in known.items():
        if origins.get(name) == "override" and not _visible(control, values):
            rule = control.get("show_if") or {}
            warnings.append(_issue(
                "ignored_override",
                f"{name} has no effect while {rule.get('field')} is {values.get(str(rule.get('field')))!r}",
                field=f"settings.{name}", blocking=False))
    return values, origins, changed


def _prompt(request: ResolveRequest, preset: contracts.PresetDefinition | None,
            values: dict[str, Any], origins: dict[str, str]) -> dict[str, Any]:
    from ..recipes import compose_prompt

    if preset is None:
        fragments = [f.model_dump() for f in request.style_fragments or []]
        positive = compose_prompt(request.prompt, fragments)
        negative = request.negative_prompt or ""
        origins["prompt"] = "override"
        origins["negative_prompt"] = "override" if request.negative_prompt is not None \
            else "registry_default"
        return {"raw": request.prompt, "template": "{{prompt}}", "styles": fragments,
                "style_ids": [f["id"] for f in fragments], "triggers": "", "notes": request.notes,
                "enhancement": {"enabled": False, "modifications": []},
                "positive": positive, "negative": negative}
    declared = {s.id: s for s in preset.prompt.styles}
    chosen = request.styles if request.styles is not None else preset.prompt.default_styles
    for index, sid in enumerate(chosen):
        if sid not in declared:
            raise _bad(f"style {sid!r} is not one of this preset's styles", f"styles.{index}",
                       code="invalid_option", valid_options=list(declared))
    if request.style_fragments:
        raise _bad("a preset declares its own styles; select them by id in `styles`",
                   "style_fragments")
    ordered = [s for s in preset.prompt.styles if s.id in set(chosen)]
    styles_text = ", ".join(s.text for s in ordered)
    adapters = request.adapters if request.adapters is not None else preset.adapters
    triggers = ", ".join(a.trigger for a in adapters if a.trigger)
    slots = {"prompt": request.prompt, "styles": styles_text, "triggers": triggers,
             "notes": request.notes}
    positive = _render(preset.prompt.template, slots)
    enabled = request.enhance if request.enhance is not None else preset.prompt.enhance_by_default
    modifications = list(preset.prompt.enhancement) if enabled else []
    if modifications:
        positive = ", ".join(part for part in (positive, *modifications) if part)
    if request.negative_prompt is not None:
        negative = request.negative_prompt
        origins["negative_prompt"] = "override"
    else:
        negative = _render(preset.prompt.negative, slots) if preset.prompt.negative else ""
        origins["negative_prompt"] = "preset"
    origins["prompt"] = "runtime"
    origins["styles"] = "override" if request.styles is not None else "preset"
    origins["enhancement"] = "override" if request.enhance is not None else "preset"
    return {"raw": request.prompt, "template": preset.prompt.template,
            "styles": [{"id": s.id, "text": s.text} for s in ordered],
            "style_ids": [s.id for s in ordered], "triggers": triggers, "notes": request.notes,
            "enhancement": {"enabled": bool(enabled), "modifications": modifications},
            "positive": positive, "negative": negative}


def _adapters(request: ResolveRequest, preset: contracts.PresetDefinition | None, ctx: Context,
              model: dict[str, Any], slot: Slot | None, target: str | None,
              known: dict[str, dict[str, Any]], blockers: list[dict[str, Any]],
              warnings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from ..modelprobe import compatible

    chosen: Sequence[Any] = (request.adapters if request.adapters is not None
                             else (preset.adapters if preset else []))
    origin = "override" if request.adapters is not None else "preset"
    out: list[dict[str, Any]] = []
    loras: list[dict[str, Any]] = []
    for index, adapter in enumerate(chosen):
        rid = contracts.check_resource_id(adapter.resource, f"adapters.{index}.resource")
        row = ctx.resource(rid)
        entry: dict[str, Any] = {
            "resource": rid, "strength": adapter.strength,
            "text_encoder_strength": adapter.text_encoder_strength, "trigger": adapter.trigger,
            "origin": origin, "status": "ok", "family": row.family if row else "unknown",
            "verification": row.verification if row else "unverified", "library_path": None,
        }
        field = f"adapters.{index}"

        def block(code: str, message: str, action: str | None = None,
                  _entry: dict[str, Any] = entry, _field: str = field) -> None:
            _entry["status"] = "blocked"
            blockers.append(_issue(code, message, field=_field, action=action))

        if row is None:
            block("resource_not_ready", f"adapter {rid} is not installed here",
                  "Import the adapter file, or remove it from the setup.")
        elif row.kind != "adapter":
            raise _bad(f"{rid} is a {row.kind}, not an adapter", f"{field}.resource",
                       code="incompatible_resource")
        else:
            model_family = model["family"]
            if row.family == "unknown" or model_family == "unknown":
                warnings.append(_issue(
                    "needs_check", f"adapter {index + 1}'s architecture could not be matched "
                    "to the model's; it may not apply", field=field, blocking=False))
            elif not compatible(row.family, model_family):
                block("incompatible_resource",
                      f"adapter {index + 1} is for {row.family} and this model is "
                      f"{model_family}", "Remove the adapter or choose a matching model.")
            pinned = [str(v) for v in (row.constraints or {}).get("variants") or []]
            if pinned and slot is not None and slot.variant not in pinned:
                block("incompatible_resource",
                      f"adapter {index + 1} declares it is for {', '.join(pinned)}, not "
                      f"{slot.variant}", "Choose a model it is made for, or remove it.")
            if target == "remote":
                block("adapters_unsupported_on_target",
                      "the Remote GPU worker does not apply your adapters yet; they would be "
                      "dropped", "Run on Local, or remove the adapters.")
            elif target == "local":
                path = ctx.adapter_path(rid)
                entry["library_path"] = path
                if path is None:
                    block("resource_not_ready", f"adapter {index + 1} has no copy in the LoRA "
                          "library", "Import the adapter, or rescan the library.")
                elif known and "loras" not in known:
                    block("adapters_unsupported_for_model",
                          "this model's loaded quantization cannot merge adapters",
                          "Change LOCAL_QUANT, choose another model, or remove the adapters.")
                elif entry["status"] == "ok":
                    lora: dict[str, Any] = {"path": path, "weight": adapter.strength}
                    if adapter.text_encoder_strength is not None:
                        lora["te_weight"] = adapter.text_encoder_strength
                    loras.append(lora)
        out.append(entry)
    return out, loras


def _finish(request: ResolveRequest, preset: contracts.PresetDefinition | None, ctx: Context,
            warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from ..finishing import FinishingError, sanitize_steps

    raw = (request.finish if request.finish is not None
           else [s.model_dump() for s in (preset.finish if preset else [])])
    try:
        steps = sanitize_steps(raw)
    except FinishingError as error:
        raise _bad(str(error), "finish", code="invalid_option") from None
    available = {p["name"]: p for p in ctx.finishing()}
    for index, step in enumerate(steps):
        info = available.get(step["processor"]) or {}
        if info and not info.get("available", True):
            warnings.append(_issue(
                "processor_unavailable",
                f"finishing step {index + 1} ({step['processor']}) cannot run here: "
                f"{info.get('unavailable_reason') or 'unavailable'}; it would be skipped",
                field=f"finish.{index}", blocking=False))
    return steps


def _inputs(request: ResolveRequest, preset: contracts.PresetDefinition | None,
            kind: str | None, specs: dict[str, dict[str, Any]], ctx: Context
            ) -> tuple[dict[str, Any], list[str], str | None, str | None]:
    from ..routers.job_api import inputs_for

    given = request.inputs
    roles = {r.role: r for r in (preset.references if preset else [])}
    for role in given.references:
        if role not in roles:
            raise _bad(f"this setup has no {role!r} reference role", f"inputs.references.{role}",
                       valid_options=list(roles))
    images = list(given.images)
    references: dict[str, list[dict[str, Any]]] = {}
    for role, declared in roles.items():
        ids = list(given.references.get(role, []))
        if not declared.min <= len(ids) <= declared.max:
            want = (str(declared.min) if declared.min == declared.max
                    else f"{declared.min}-{declared.max}")
            raise _bad(f"{role} takes {want} reference(s); got {len(ids)}",
                       f"inputs.references.{role}", code="missing_input" if not ids else
                       "invalid_request")
        references[role] = [_asset_entry(i, "image", f"inputs.references.{role}.{n}", ctx)
                            for n, i in enumerate(ids)]
        images.extend(ids)
    entries = [_asset_entry(i, "image", f"inputs.images.{n}", ctx)
               for n, i in enumerate(given.images)]
    mask = _asset_entry(given.mask, "image", "inputs.mask", ctx) if given.mask else None
    last = (_asset_entry(given.last_frame, "image", "inputs.last_frame", ctx)
            if given.last_frame else None)
    if kind is not None:
        spec = inputs_for(kind, specs.get(kind))
        if not spec.min_images <= len(images) <= spec.max_images:
            want = (str(spec.min_images) if spec.min_images == spec.max_images
                    else f"{spec.min_images}-{spec.max_images}")
            raise _bad(f"{kind} takes {want} input image(s); this request has {len(images)}",
                       "inputs.images", code="missing_input" if len(images) < spec.min_images
                       else "invalid_request")
        if spec.mask == "required" and mask is None:
            raise _bad(f"{kind} needs a mask: white marks what changes", "inputs.mask",
                       code="missing_input")
        if spec.mask == "none" and mask is not None:
            raise _bad(f"{kind} does not take a mask", "inputs.mask")
        if mask is not None:
            first = ctx.asset(images[0])
            if (mask["width"], mask["height"]) != (first.width, first.height):
                raise _bad(f"the mask is {mask['width']}x{mask['height']} but the image is "
                           f"{first.width}x{first.height}; a mask must match its image",
                           "inputs.mask", code="invalid_mask")
        if last is not None and not spec.last_frame:
            raise _bad(f"{kind} does not take a last frame here", "inputs.last_frame")
    paths = [ctx.asset(i).path for i in images]
    record = {"images": entries, "references": references, "mask": mask, "last_frame": last}
    return (record, paths, ctx.asset(given.mask).path if given.mask else None,
            ctx.asset(given.last_frame).path if given.last_frame else None)


def _asset_entry(asset_id: int, kind: str, field: str, ctx: Context) -> dict[str, Any]:
    row = ctx.asset(asset_id)
    if row is None:
        raise ResolveError(404, f"{field}: asset {asset_id} does not exist or is in the trash",
                           code="not_found", field=field)
    if row.kind != kind:
        raise _bad(f"{field}: asset {asset_id} is a {row.kind}, not an {kind}", field)
    if not ctx.asset_file_exists(row):
        raise ResolveError(409, f"{field}: asset {asset_id}'s file is missing from disk",
                           code="conflict", field=field)
    return {"asset_id": int(asset_id), "hash": ctx.asset_hash(row),
            "width": row.width, "height": row.height}


def _effective(kind: str | None, raw: dict[str, Any], paths: list[str], mask_path: str | None,
               last_path: str | None, origins: dict[str, str],
               warnings: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the job builder exactly as submission will, and say what it changed."""
    if kind is None:
        return {}, []
    from fastapi import HTTPException

    from ..routers import images, videos

    try:
        if kind in _IMAGE_KINDS:
            built = images.build_params(kind, dict(raw), paths, mask_path)
        else:
            built = videos.build_params(kind, dict(raw), paths, last_path)
    except HTTPException as error:
        raise _bad(str(error.detail), "settings") from None
    effective = {k: v for k, v in built.items() if k not in _PATH_KEYS}
    normalized = []
    for key, requested in raw.items():
        if key not in effective or key in {"raw_prompt", "style_ids", "loras"}:
            continue
        got = effective[key]
        if contracts.canonical_json(got) != contracts.canonical_json(requested):
            normalized.append({"field": key, "requested": requested, "effective": got,
                               "origin": origins.get(key, "runtime"),
                               "reason": _NORMALIZED_WHY.get(key, "the job builder normalizes "
                                                             "this value")})
            if origins.get(key) == "override":
                warnings.append(_issue(
                    "normalized_override",
                    f"{key} will run as {got!r}, not {requested!r}: "
                    f"{_NORMALIZED_WHY.get(key, 'normalized by the job builder')}",
                    field=f"settings.{key}", blocking=False))
    return effective, normalized


_NEEDS_RESOURCES = frozenset({"resource_not_ready", "access_required"})


def _readiness(blockers: list[dict[str, Any]],
               warnings: list[dict[str, Any]]) -> dict[str, Any]:
    """blocked > needs_resources > needs_check > ready.

    Only `ready` is a claim that the plan will run as is. `needs_resources`
    (a first download) and `needs_check` (unmeasured hardware, an adapter we
    could not match) can still be submitted; `blocked` cannot.
    """
    codes = {w["code"] for w in warnings}
    if blockers:
        state = "blocked"
    elif codes & _NEEDS_RESOURCES:
        state = "needs_resources"
    elif codes & {"needs_check", "insufficient_memory"}:
        state = "needs_check"
    else:
        state = "ready"
    actions = [i["action"] for i in [*blockers, *warnings] if i.get("action")]
    return {"state": state, "ready": state == "ready", "submittable": not blockers,
            "blockers": blockers, "actions": list(dict.fromkeys(actions))}


def _seed_mapping(base: int, params: Mapping[str, Any]) -> list[int]:
    from ..prompt_engine import seed_for

    mode = str(params.get("seed_mode", "increment"))
    count = max(1, int(params.get("batch", 1) or 1))
    if mode == "random":
        return []  # each item draws its own; recorded on the outputs, not predictable here
    return [seed_for(base, index, mode) for index in range(count)]
