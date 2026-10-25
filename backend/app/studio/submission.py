"""Submit a resolved plan as an ordinary job, and replay one exactly.

There is no second queue: a plan becomes a job of the kind the resolver chose,
through `routers.common._create_and_enqueue`, with the builder's own params.
What this adds is the contract around that one call:

* **Idempotency by request.** A request id already used by the same canonical
  request returns its job, even if capabilities changed since; the same id
  with a different request is `request_conflict`.
* **No setup displayed and another queued.** A submission may name the
  execution plan hash it previewed. If the fresh plan differs in anything that
  would run differently (`material_hash`), it is refused as `plan_stale`, with
  the changes when the preview is still known here.
* **Readiness is checked at enqueue and again at dispatch.** A plan that is
  not ready creates no job. A queued job whose model slot, adapter files or
  remote worker build changed before it started fails as stale instead of
  running a different setup.
* **Exact replay** reuses the stored plan and refuses, as
  `replay_resource_unavailable`, when a resource it names is gone. Running the
  latest preset revision is a different, explicit action.
"""
from __future__ import annotations

import json
import secrets
from collections import OrderedDict
from typing import Any

from .. import db, log
from ..problems import RequestError
from . import contracts

logger = log.get("plans")

REQUEST_DOMAIN = "wizards-brush/resolve-request"
_RECENT: OrderedDict[str, dict[str, Any]] = OrderedDict()
_RECENT_LIMIT = 256
_MODEL_KEYED = frozenset({"image_local", "img2img", "inpaint", "outpaint", "control_local",
                          "image_colab"})


class PlanStale(RuntimeError):
    """A queued plan that no longer runs as it was resolved."""

    code = "plan_stale"

    def __init__(self, message: str, tip: str) -> None:
        super().__init__(f"plan_stale: {message}")
        self.tip = tip


def remember(plan: dict[str, Any]) -> None:
    """Keep a preview so a later stale submission can say what changed.

    In memory and bounded: a preview creates nothing durable.
    """
    key = plan["hashes"]["execution_plan_hash"]
    _RECENT[key] = plan
    _RECENT.move_to_end(key)
    while len(_RECENT) > _RECENT_LIMIT:
        _RECENT.popitem(last=False)


def request_digest(request: Any) -> str:
    """The canonical resolve input, seed included: what idempotency compares."""
    return contracts.digest(request.model_dump(mode="json"), domain=REQUEST_DOMAIN)


def diff(before: dict[str, Any] | None, after: dict[str, Any]) -> list[dict[str, Any]]:
    """What runs differently between two plans, for a person to read."""
    if before is None:
        return []
    out = []
    for key in ("kind", "target", "model", "adapters", "prompt", "effective_params", "inputs",
                "finish"):
        old, new = before.get(key), after.get(key)
        if key == "target":
            old, new = (old or {}).get("resolved"), (new or {}).get("resolved")
        if key == "effective_params" and isinstance(old, dict) and isinstance(new, dict):
            for name in sorted(set(old) | set(new)):
                if contracts.canonical_json(old.get(name)) != contracts.canonical_json(new.get(name)):
                    out.append({"field": f"effective_params.{name}", "before": old.get(name),
                                "after": new.get(name)})
            continue
        if contracts.canonical_json(old) != contracts.canonical_json(new):
            out.append({"field": key, "before": old, "after": new})
    return out


def _summary(plan: dict[str, Any]) -> dict[str, Any]:
    return {"kind": plan["kind"], "target": plan["target"]["resolved"],
            "hashes": plan["hashes"], "readiness": plan["readiness"]}


def _refuse_unready(plan: dict[str, Any]) -> RequestError:
    first = plan["readiness"]["blockers"][0]
    retryable = first["code"] in {"target_unavailable", "resource_not_ready", "kind_unavailable"}
    return RequestError(
        503 if retryable else 409, first["message"], code=first["code"],
        field=first.get("field"), retryable=retryable,
        state={"readiness": plan["readiness"], "plan": _summary(plan)},
        suggestions=plan["readiness"]["actions"][:3],
    )


def job_params(plan: dict[str, Any]) -> dict[str, Any]:
    """The params the job runs: the builder's effective params plus inputs.

    Inputs are attached from the asset ids the plan recorded, the way the
    routes attach them, so the job is indistinguishable from a form job.
    """
    from ..routers import images

    kind = str(plan["kind"])
    params = json.loads(json.dumps(plan["effective_params"]))
    inputs = plan["inputs"]
    ids = [e["asset_id"] for e in inputs["images"]]
    for entries in inputs["references"].values():
        ids.extend(e["asset_id"] for e in entries)
    paths = [_path(i) for i in ids]
    mask = _path(inputs["mask"]["asset_id"]) if inputs.get("mask") else None
    last = _path(inputs["last_frame"]["asset_id"]) if inputs.get("last_frame") else None
    if kind in {"t2v", "long_video"}:
        return params
    if kind == "i2v":
        params["image_path"] = paths[0]
        if last:
            params["last_image_path"] = last
        return params
    return images.attach_inputs(kind, params, paths, mask)


def _path(asset_id: int) -> str:
    row = db.get_asset(int(asset_id))
    if row is None:
        raise RequestError(409, f"asset {asset_id} was removed after the plan was made",
                           code="plan_stale", field="inputs")
    return row.path


def _backend_id(plan: dict[str, Any]) -> str | None:
    target = plan["target"]["resolved"]
    if target == "local":
        return "local"
    if target == "remote":
        from ..remote_gpu_client import last_health

        build = str(last_health().get("build") or "")
        return f"remote:{build}" if build else "remote"
    return None


async def submit_plan(body: Any, plan: dict[str, Any], request: Any) -> Any:
    from ..routers.common import _create_and_enqueue, _submission_response, get_handler, normalize_request_id
    from ..routers.job_api import JobSubmission

    request_id = normalize_request_id(body.request_id)
    digest = request_digest(request)
    if request_id:
        existing = db.job_for_request(request_id)
        if existing is not None:
            if existing.request_digest == digest:
                response = _submission_response(existing)
                return JobSubmission(job_id=int(response["job_id"]),
                                     job_ids=list(response.get("job_ids") or [response["job_id"]]),
                                     group_id=response.get("group_id"), duplicate=True)
            raise RequestError(
                409, "this request_id was already used for a different request",
                code="request_conflict", field="request_id", retryable=False,
                suggestions=["Use a new request_id for a new request."],
            )
    expected = getattr(body, "expected_plan_hash", None)
    if expected and expected != plan["hashes"]["execution_plan_hash"]:
        before = _RECENT.get(expected)
        if before is None or before["hashes"]["material_hash"] != plan["hashes"]["material_hash"]:
            raise RequestError(
                409, "the setup changed since it was previewed; review it and submit again",
                code="plan_stale", field="expected_plan_hash", retryable=False,
                state={"plan": _summary(plan), "changes": diff(before, plan),
                       "previewed_known": before is not None},
                suggestions=["Preview again and confirm the new plan."],
            )
    if not plan["readiness"]["ready"]:
        raise _refuse_unready(plan)
    if plan["effective_params"].get("combinatorial"):
        raise RequestError(
            400, "a combinatorial prompt fans out into several jobs; use a Variant Set, or the "
                 "kind form of this request", code="invalid_request",
            field="settings.combinatorial")
    kind = str(plan["kind"])
    handler = get_handler(kind)
    if handler is None:  # pragma: no cover — every planned kind registers one at import
        raise RequestError(500, f"no handler for {kind}", code="internal")
    params = job_params(plan)
    row = db.insert_plan(
        request_id=request_id, request_digest=digest,
        creative_hash=plan["hashes"]["creative_request_hash"],
        execution_hash=plan["hashes"]["execution_plan_hash"],
        material_hash=plan["hashes"]["material_hash"],
        schema_version=plan["schema_version"], resolver_version=plan["resolver_version"],
        preset_id=(plan["preset"] or {}).get("id"),
        preset_revision=(plan["preset"] or {}).get("revision"),
        kind=kind, target=str(plan["target"]["resolved"]),
        plan_json=json.dumps(plan), request_json=json.dumps(request.model_dump(mode="json")),
    )
    job, created = await _create_and_enqueue(
        kind, params, handler, group_id=None, request_id=request_id, plan_id=row.id,
        request_digest=digest, backend_id=_backend_id(plan),
    )
    if not created:
        # A concurrent retry won the request id. Its digest decides.
        if job.request_digest != digest:
            raise RequestError(409, "this request_id was already used for a different request",
                               code="request_conflict", field="request_id", retryable=False)
        return JobSubmission(job_id=int(job.id or 0), job_ids=[int(job.id or 0)], duplicate=True)
    logger.info("job %s runs plan %s (%s)", job.id, row.id, plan["hashes"]["creative_request_hash"])
    return JobSubmission(job_id=int(job.id or 0), job_ids=[int(job.id or 0)])


# ---- dispatch ------------------------------------------------------------------------------
def runtime_snapshot(target: str | None) -> dict[str, Any]:
    """What is about to execute: app and library versions, the worker build.

    Read from package metadata, never by importing the heavy stack.
    """
    from importlib import metadata

    from ..version import get_version

    versions: dict[str, str] = {}
    for package in ("torch", "diffusers", "transformers", "peft", "accelerate"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = ""
    snapshot: dict[str, Any] = {"app": get_version(), "target": target}
    if target == "remote":
        from ..remote_gpu_client import last_health

        health = last_health()
        snapshot["worker_build"] = str(health.get("build") or "")
        snapshot["worker_features"] = sorted(health.get("features") or [])
    else:
        snapshot["libraries"] = versions
    return snapshot


def admit_at_dispatch(job: Any) -> None:
    """Recheck a planned job as it starts, and record what runs it.

    Raises PlanStale when the bound setup changed after enqueue: the model
    slot now serves a different model, an adapter file changed or vanished,
    or the remote worker was replaced by a different build.
    """
    row = db.get_plan(int(job.plan_id))
    if row is None:
        raise PlanStale("its execution plan is missing", "Submit the preset request again.")
    plan = row.plan
    target = plan["target"]["resolved"]
    _check_model(plan)
    _check_adapters(plan)
    if target == "remote":
        from ..remote_gpu_client import last_health

        build = str(last_health().get("build") or "")
        planned = (job.backend_id or "").partition(":")[2]
        if planned and build and build != planned:
            raise PlanStale(
                f"the Remote GPU worker changed from build {planned} to {build} after this "
                "job was queued", "Preview the request again on the new worker and resubmit.")
    db.set_execution(int(job.id), runtime_snapshot(target))


def _check_model(plan: dict[str, Any]) -> None:
    from ..generators import variants
    from ..resources.catalogue import model_resource_id

    variant = plan["model"].get("variant")
    kind = plan["kind"]
    if kind not in _MODEL_KEYED or not variant:
        return
    lane = "local" if plan["target"]["resolved"] == "local" else "colab"
    known = variants.exact(str(variant), lane)
    repo = variants.repo_of(known) if known is not None else ""
    if not repo or model_resource_id(repo) != plan["model"]["resource"]:
        raise PlanStale(
            f"the {variant} slot no longer serves {plan['model']['resource']}",
            "Reconfigure the slot, or run the latest preset with the current model.")


def _check_adapters(plan: dict[str, Any]) -> None:
    from ..resources import catalogue, store

    for entry in plan["adapters"]:
        path = entry.get("library_path")
        if not path:
            continue
        install = store.install_at("local", f"loras:{path}")
        file = catalogue.path_for(f"loras:{path}")
        if install is None or file is None or install.resource_id != entry["resource"]:
            raise PlanStale(
                f"adapter {entry['resource']} is no longer at {path}",
                "Rescan the LoRA library or re-import the adapter, then submit again.")
        if install.mtime_ns is not None and file.stat().st_mtime_ns != install.mtime_ns:
            raise PlanStale(f"the adapter file {path} changed after this job was queued",
                            "Rescan the LoRA library, then submit again.")


# ---- replay --------------------------------------------------------------------------------
def replay_problems(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Why a stored plan cannot run exactly as recorded here, if anything."""
    from ..resources import catalogue, store
    from ..resources.catalogue import configured_slots, model_resource_id

    problems: list[dict[str, Any]] = []
    target = plan["target"]["resolved"]
    variant = plan["model"].get("variant")
    serving = [s for s in configured_slots()
               if s.lane == target and model_resource_id(s.repo) == plan["model"]["resource"]
               and (variant is None or s.variant == variant)]
    if not serving:
        problems.append({"field": "model", "resource": plan["model"]["resource"],
                         "message": "no slot on the recorded target serves the recorded model"})
    for index, entry in enumerate(plan["adapters"]):
        if store.get(entry["resource"]) is None or (
                target == "local" and catalogue.local_file(entry["resource"]) is None):
            problems.append({"field": f"adapters.{index}", "resource": entry["resource"],
                             "message": "the recorded adapter is not installed"})
    inputs = plan["inputs"]
    entries = [*inputs["images"], *(e for v in inputs["references"].values() for e in v)]
    for key in ("mask", "last_frame"):
        if inputs.get(key):
            entries.append(inputs[key])
    for entry in entries:
        row = db.get_asset(int(entry["asset_id"]))
        if row is None:
            problems.append({"field": "inputs", "asset_id": entry["asset_id"],
                             "message": "an input asset is gone"})
            continue
        content = db.content_of(int(row.id or 0))
        current = f"blake2b:{content.hash}" if content is not None else ""
        if entry.get("hash") and current and current != entry["hash"]:
            problems.append({"field": "inputs", "asset_id": entry["asset_id"],
                             "message": "an input asset's content changed"})
    return problems


async def replay_job(job_id: int, *, request_id: str | None = None) -> Any:
    """Run a planned job again exactly: same plan, same resources, same seed."""
    from ..routers.common import _create_and_enqueue, get_handler, normalize_request_id
    from ..routers.job_api import JobSubmission

    job = db.get_job(job_id)
    if job is None:
        raise RequestError(404, "job not found", code="not_found", field="job_id")
    if job.plan_id is None:
        raise RequestError(
            409, "this job was made without an execution plan, so an exact replay cannot be "
                 "promised (legacy_unverified)", code="legacy_unverified", field="job_id",
            suggestions=["Use rerun to queue its stored params again, without a strict claim."])
    row = db.get_plan(int(job.plan_id))
    if row is None:
        raise RequestError(409, "the job's plan is missing", code="replay_resource_unavailable",
                           field="job_id")
    plan = row.plan
    problems = replay_problems(plan)
    if problems:
        raise RequestError(
            409, "the recorded setup cannot run exactly here: " + problems[0]["message"],
            code="replay_resource_unavailable", field=problems[0]["field"], retryable=False,
            state={"problems": problems, "preset": plan.get("preset")},
            suggestions=["Clone the setup with the current resources (run the latest preset).",
                         "Restore the missing resource and replay again."])
    kind = str(plan["kind"])
    handler = get_handler(kind)
    if handler is None:  # pragma: no cover
        raise RequestError(500, f"no handler for {kind}", code="internal")
    rid = normalize_request_id(request_id) or f"replay-{secrets.token_hex(12)}"
    copy = db.insert_plan(
        request_id=rid, request_digest=row.request_digest, creative_hash=row.creative_hash,
        execution_hash=row.execution_hash, material_hash=row.material_hash,
        schema_version=row.schema_version, resolver_version=row.resolver_version,
        preset_id=row.preset_id, preset_revision=row.preset_revision, kind=row.kind,
        target=row.target, plan_json=row.plan_json, request_json=row.request_json,
        replay_of=row.id,
    )
    replayed, _created = await _create_and_enqueue(
        kind, job_params(plan), handler, group_id=None, request_id=rid, plan_id=copy.id,
        request_digest=row.request_digest, backend_id=_backend_id(plan),
    )
    return JobSubmission(job_id=int(replayed.id or 0), job_ids=[int(replayed.id or 0)])
