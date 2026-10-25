"""Preset stages: a studio preset revision expanded into an ordinary stage.

A stage may name a preset instead of spelling out an operation and settings.
The set resolves it once, at preview and creation, with the same resolver a
single run uses, and writes the result into the stage: the kind it runs, the
preset's effective settings and adapters as params, its composed prompt (the
stage prompt is inserted as written, so ``{{axis}}`` placeholders survive for
each variant), its finishing and its validation policy. The pinned revision
and definition hash stay on the stage.

Explicit stage values win over the preset's: `params` and per-value settings
override, a non-empty `finishing` replaces, and a non-empty `validation`
replaces. Everything after expansion is the existing compiler, scheduler,
snapshot and retry path; there is no second batch queue.
"""
from __future__ import annotations

from typing import Any

from ..validators import ValidationSpec
from .operations import OPERATIONS, controls_for
from .recipe import RecipeError, RecipeSpec, StageSpec


def _policy() -> dict[str, bool]:
    from ..studio import targets

    return targets.policy()


def _label(index: int, stage: StageSpec) -> str:
    return f"stage {index + 1}" + (f" ({stage.name})" if stage.name else "")


def expand(spec: RecipeSpec, *, specs: dict[str, dict[str, Any]],
           context: Any = None) -> tuple[RecipeSpec, list[dict[str, Any]]]:
    """The spec with every preset stage expanded and pinned, and a report per
    preset stage (kind, target, pinned revision, readiness).

    Raises RecipeError for a preset that cannot run as a set stage here.
    """
    from ..problems import RequestError
    from ..studio.resolver import ResolveRequest, resolve_stage

    stages: list[StageSpec] = []
    reports: list[dict[str, Any]] = []
    for index, stage in enumerate(spec.stages):
        preset = stage.preset
        if preset is None or preset.definition_hash:
            stages.append(stage)
            continue
        where = _label(index, stage)
        try:
            template = resolve_stage(ResolveRequest(
                preset_id=preset.id, revision=preset.revision, prompt=stage.prompt,
                negative_prompt=stage.negative_prompt or None, target=preset.target,
                allow_remote=preset.allow_remote), context)
        except RequestError as error:
            raise RecipeError(f"{where}: preset {preset.id}: {error.detail}") from None
        kind = template["kind"]
        readiness = template["readiness"]
        if not readiness["submittable"] or kind is None:
            reason = "; ".join(b["message"] for b in readiness["blockers"]) or "it cannot run here"
            actions = " ".join(readiness["actions"])
            raise RecipeError(f"{where}: preset {preset.id} cannot run here: {reason}"
                              + (f". {actions}" if actions else ""))
        if kind not in OPERATIONS:
            raise RecipeError(f"{where}: preset {preset.id} runs {kind}, which a Variant Set "
                              f"stage cannot drive (supported: {', '.join(OPERATIONS)})")
        if stage.operation and stage.operation != kind:
            raise RecipeError(f"{where}: the preset runs {kind}, not {stage.operation}; leave "
                              "the operation empty for a preset stage")
        sends_images = index > 0 or bool(spec.sources)
        if template["target"]["resolved"] == "remote" and sends_images \
                and not (preset.allow_remote or _policy().get("send_inputs_to_remote")):
            raise RecipeError(f"{where}: preset {preset.id} runs on the Remote GPU and this "
                              "stage would send it images; set allow_remote on the stage, or "
                              "allow sending inputs in Settings")
        controls = controls_for(specs.get(kind) or {})
        params = {k: v for k, v in template["settings"].items() if k in controls}
        if template["loras"] and "loras" in controls:
            params["loras"] = template["loras"]
        params.update(stage.params)
        validation = stage.validation
        if validation == ValidationSpec() and template["validation_spec"]:
            validation = ValidationSpec.model_validate(template["validation_spec"])
        pinned = template["preset"]
        stages.append(stage.model_copy(update={
            "operation": kind,
            "preset": preset.model_copy(update={"revision": pinned["revision"],
                                                "definition_hash": pinned["definition_hash"]}),
            "prompt": template["prompt"]["positive"],
            "negative_prompt": template["prompt"]["negative"],
            "params": params,
            "finishing": list(stage.finishing) or list(template["finish"]),
            "validation": validation,
        }))
        reports.append({
            "stage": index, "preset": pinned, "kind": kind,
            "target": template["target"], "model": template["model"].get("resource"),
            "adapters": [a["resource"] for a in template["adapters"]],
            "readiness": {k: readiness[k] for k in ("state", "ready", "submittable", "actions")},
            "warnings": [w["message"] for w in template["warnings"]],
        })
    return spec.model_copy(update={"stages": stages}), reports
