"""Variant Set orchestration: create, submit, complete, retry, cancel, recover.

The rules, each of which exists for a reason:

* **A child is an ordinary job.** It is created by the shared fan-out primitive
  (``routers.common.submit_group``) with an existing kind, the set's
  ``group_id`` and params built by the route's own builder. Nothing about lanes,
  model affinity, cancel, rerun or asset replay needs to know sets exist.
* **An item has its own state; a job exists only when the item can run.** A
  later stage's item waits in ``pending`` until its parent succeeds, and moves
  to ``blocked`` if the parent does not — without inventing a job status, and
  without a job a restart would try to resume.
* **Submission is idempotent.** Each attempt of each item has a deterministic
  request id, so a submission interrupted between creating the job and
  recording it resolves to the same job the next time, never to a second one.
* **Completion is processed once.** The item moves from ``queued`` only through
  a compare-and-set naming the job that finished, so the queue listener and
  restart reconciliation can both see the same completion safely.
* **The listener is an optimisation.** Everything it does is recoverable from
  the job rows, which is what :func:`reconcile_set` does at startup and when a
  set is read.
* **Nothing successful is redone unless asked.** Retry takes only failed and
  invalid items (and canceled ones on request); rerunning a succeeded item is a
  separate, explicit action.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .. import db, log, validators
from ..models import Job, JobStatus, VariantItem, VariantSet
from ..prompt_engine import MAX_PROMPT_CHARS
from ..utils.seeds import resolve_seed
from ..version import get_version
from . import expansion, operations, store, templates
from .recipe import (
    CompiledRecipe,
    RecipeError,
    RecipeSpec,
    compile_recipe,
    describe_collisions,
    materialize,
    render_request,
)
from .store import BLOCKED, CANCELED, FAILED, INVALID, PENDING, QUEUED, SUCCEEDED

logger = log.get("variant-sets")

GROUP_PREFIX = "vset-"
LISTENER = "variant_sets"
MAX_NAME_CHARS = 120


class VariantSetError(Exception):
    """A request the service refuses. `status` maps to an HTTP status code."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def is_variant_group(group_id: str | None) -> bool:
    return bool(group_id) and str(group_id).startswith(GROUP_PREFIX)


def _request_id(group_id: str, item_id: int, attempt: int) -> str:
    """One attempt of one item, globally unique: built on the set's random
    group id, never on a row id alone, so no other set can ever claim it."""
    return f"{group_id}-item-{item_id}-attempt-{attempt}"


def _now() -> datetime:
    return datetime.now(UTC)


def emit(set_row: VariantSet | None) -> None:
    """Tell clients a set changed, on the job hub every page already listens to."""
    if set_row is None or set_row.id is None:
        return
    try:
        from ..queue import hub

        hub.emit({"type": "variant_set", "id": set_row.id, "status": set_row.status,
                  "counts": set_row.counts})
    except Exception:  # a notification must never fail the state change
        logger.debug("could not announce variant set %s", set_row.id, exc_info=True)


def _refresh(set_id: int) -> VariantSet | None:
    row = store.refresh_set(set_id)
    emit(row)
    return row


# ---- compiling --------------------------------------------------------------------------
def compile_spec(spec: RecipeSpec) -> CompiledRecipe:
    try:
        return compile_recipe(spec, specs=operations.registry_specs())
    except RecipeError as error:
        raise VariantSetError(400, str(error)) from None


def snapshot_spec(row: VariantSet) -> RecipeSpec:
    """The recipe a set ran, as it was when the set was created."""
    return RecipeSpec.model_validate(row.recipe)


def _referenced_axes(recipe: CompiledRecipe) -> set[str]:
    used: set[str] = set()
    for stage in recipe.stages:
        used |= set(templates.placeholders(stage.prompt))
        used |= set(templates.placeholders(stage.negative_prompt))
        used |= set(stage.value_params)
    return used


def preview(spec: RecipeSpec, *, limit: int = 100) -> dict[str, Any]:
    """Everything a set would do, without creating anything."""
    recipe = compile_spec(spec)
    base = spec.seed.value if spec.seed.value >= 0 else None
    try:
        planned, collisions = materialize(recipe, base_seed=base)
    except RecipeError as error:
        raise VariantSetError(400, str(error)) from None
    warnings: list[str] = []
    if spec.seed.mode == "fixed":
        idle = [name for name in recipe.axis_names if name not in _referenced_axes(recipe)]
        for name in idle:
            warnings.append(
                f"'{name}' is not used by any prompt or setting, so its variants would be "
                "identical; use it in a template, give it settings, or use per-variant seeds"
            )
    return {
        "total": recipe.total,
        "cap": expansion.configured_cap(),
        "stages": [_stage_view(stage) for stage in recipe.stages],
        "items": [{"stage": p.stage, "key": p.key, "values": p.values,
                   "prompt": p.request.get("prompt", ""),
                   "negative_prompt": p.request.get("negative_prompt", ""),
                   "seed": p.request.get("seed"), "output_name": p.output_name}
                  for p in planned[:max(0, limit)]],
        "collisions": [{"stage": stage, "name": name, "keys": keys}
                       for stage, found in collisions.items() for name, keys in found.items()],
        "warnings": warnings,
        "recipe": recipe.snapshot(),
    }


def _stage_view(stage: Any) -> dict[str, Any]:
    return {"index": stage.index, "name": stage.name, "operation": stage.operation.kind,
            "axes": [{"name": a.name, "values": list(a.values)} for a in stage.axes],
            "count": stage.count, "total": stage.total, "naming": stage.naming_template}


# ---- assets a recipe depends on ------------------------------------------------------------
def _live_images(ids: Iterable[int]) -> dict[int, Any]:
    wanted = list(dict.fromkeys(int(i) for i in ids))
    found = {int(a.id): a for a in db.get_assets(wanted) if a.id is not None}
    for asset_id in wanted:
        asset = found.get(asset_id)
        if asset is None:
            raise VariantSetError(400, f"asset {asset_id} does not exist or is in the trash")
        if asset.kind != "image":
            raise VariantSetError(400, f"asset {asset_id} is a {asset.kind}, not an image")
    return found


def check_assets(spec: RecipeSpec) -> None:
    """Every asset a recipe names exists, is live, and is an image."""
    ids = [*spec.sources, *(m.asset_id for m in spec.masks.values()),
           *(ref for stage in spec.stages for ref in stage.references)]
    _live_images(ids)


# ---- creation -------------------------------------------------------------------------------
@dataclass
class Created:
    set: VariantSet
    created: bool


async def create_set(
    spec: RecipeSpec, *, name: str, recipe_id: int | None = None,
    request_id: str | None = None, collection_id: int | None = None,
    new_collection: bool = False,
) -> Created:
    """Persist a set and queue its first stage. Idempotent on `request_id`."""
    if request_id and (existing := store.set_for_request(request_id)) is not None:
        return Created(existing, False)
    recipe = compile_spec(spec)
    check_assets(spec)
    base_seed = spec.seed.value if spec.seed.value >= 0 else resolve_seed(None)
    try:
        planned, collisions = materialize(recipe, base_seed=base_seed)
    except RecipeError as error:
        raise VariantSetError(400, str(error)) from None
    if collisions:
        raise VariantSetError(400, describe_collisions(collisions))

    label = (name or "").strip()[:MAX_NAME_CHARS] or f"Variant set · {recipe.total} variants"
    if new_collection:
        collection_id = db.create_collection(label).id
    elif collection_id is not None and not any(c.id == collection_id
                                               for c, _n in db.list_collections()):
        raise VariantSetError(400, f"collection {collection_id} does not exist")

    snapshot = recipe.snapshot()
    snapshot["seed"]["value"] = base_seed   # the snapshot records what actually ran
    stages: list[list[dict[str, Any]]] = [[] for _ in recipe.stages]
    index_in_stage: dict[int, int] = {}
    for position, item in enumerate(planned):
        index_in_stage[position] = len(stages[item.stage])
        stages[item.stage].append({
            "stage": item.stage, "ordinal": item.ordinal, "key": item.key,
            "values_json": store.dumps(item.values),
            "params_json": store.params_json(item.request),
            "output_name": item.output_name,
            "parent": index_in_stage[item.parent] if item.parent is not None else None,
        })
    row = VariantSet(
        name=label, recipe_id=recipe_id, recipe_json=store.dumps(snapshot),
        operation=recipe.stages[0].operation.kind,
        source_asset_ids_json=store.dumps(list(spec.sources)),
        group_id=f"{GROUP_PREFIX}{uuid.uuid4().hex}", request_id=request_id,
        expected=len(planned), collection_id=collection_id,
        replay_json=store.dumps({
            "base_seed": base_seed, "seed_mode": spec.seed.mode,
            "app_version": get_version(), "recipe_version": spec.version,
        }),
    )
    row, created = store.insert_set(row, stages)
    if not created:
        return Created(row, False)
    assert row.id is not None
    await submit_items(row, store.items_for_set(row.id, stage=0))
    refreshed = _refresh(row.id)
    return Created(refreshed or row, True)


# ---- submission ----------------------------------------------------------------------------------
def _fail(item: VariantItem, reason: str, *, state: str = FAILED) -> None:
    assert item.id is not None
    store.transition(item.id, when_state=(PENDING,), state=state, state_reason=reason)


def _inputs(row: VariantSet, spec: RecipeSpec, item: VariantItem,
            parents: dict[int, VariantItem]) -> tuple[list[int], list[str], str | None]:
    """(source asset ids, their file paths, mask path) for one item's next attempt."""
    stage = spec.stages[item.stage]
    if item.stage == 0:
        source_ids = list(row.source_asset_ids)
    else:
        parent = parents.get(int(item.parent_item_id or 0))
        output = parent.asset_ids[:1] if parent is not None else []
        if not output:
            raise VariantSetError(409, "the stage it derives from has no output")
        source_ids = [*output, *stage.references]
    try:
        assets = _live_images([*source_ids, *(
            [spec.masks[stage.mask].asset_id] if stage.mask else [])])
    except VariantSetError as error:
        raise VariantSetError(409, f"an input is no longer available: {error.detail}") from None
    mask_path = assets[spec.masks[stage.mask].asset_id].path if stage.mask else None
    return source_ids, [assets[i].path for i in source_ids], mask_path


def _effective_params(row: VariantSet, recipe: RecipeSpec, item: VariantItem,
                      source_ids: list[int], paths: list[str],
                      mask_path: str | None) -> dict[str, Any]:
    """The child job's params: rebuilt from the snapshot only when the item has
    never been built (or was reset by a cascade); otherwise the stored effective
    params, so a retry reproduces exactly what failed."""
    stored = item.params
    if "variant" in stored:
        return stored
    op = operations.OPERATIONS[recipe.stages[item.stage].operation]
    params = op.build(stored, paths, mask_path)
    params["variant"] = {
        "set_id": row.id, "group_id": row.group_id, "item_id": item.id, "stage": item.stage,
        "key": item.key, "values": item.values, "source_asset_ids": source_ids,
    }
    return params


async def submit_items(row: VariantSet, items: Iterable[VariantItem]) -> int:
    """Create (or recover) the child job for each pending item. Returns how many
    items were handed to a lane."""
    from ..queue import cancel as cancel_job
    from ..routers.common import submit_group

    if row.id is None or row.canceled_at is not None:
        return 0
    recipe = snapshot_spec(row)
    wanted = [i for i in items if i.state == PENDING and i.id is not None]
    if not wanted:
        return 0
    parents = {int(p.id): p for p in store.get_items(
        {int(i.parent_item_id) for i in wanted if i.parent_item_id is not None}) if p.id is not None}
    by_kind: dict[str, list[tuple[VariantItem, dict[str, Any], list[int]]]] = {}
    for item in wanted:
        try:
            source_ids, paths, mask_path = _inputs(row, recipe, item, parents)
            params = _effective_params(row, recipe, item, source_ids, paths, mask_path)
        except VariantSetError as error:
            _fail(item, error.detail)
            continue
        except Exception as error:  # noqa: BLE001 — one bad item must not stop its siblings
            logger.warning("variant item %s could not be built: %s", item.id, error)
            _fail(item, f"could not build this variant: {getattr(error, 'detail', error)}")
            continue
        kind = recipe.stages[item.stage].operation
        by_kind.setdefault(kind, []).append((item, params, source_ids))

    submitted = 0
    for kind, batch in by_kind.items():
        op = operations.OPERATIONS[kind]
        results = await submit_group(
            kind, op.handler(), [params for _i, params, _s in batch], group_id=row.group_id,
            request_ids=[_request_id(row.group_id, int(i.id or 0), i.attempts + 1)
                         for i, _p, _s in batch],
            record_history=False,
        )
        for (item, params, source_ids), (job, created) in zip(batch, results, strict=False):
            assert item.id is not None and job.id is not None
            if job.group_id != row.group_id:   # impossible with group-scoped ids; never adopt
                logger.error("variant item %s resolved to job %s of another run", item.id, job.id)
                _fail(item, "its job identity collided with another run")
                continue
            won = store.transition(
                item.id, when_state=(PENDING,), state=QUEUED, state_reason="",
                job_id=job.id, attempts=item.attempts + 1,
                params_json=store.params_json(params),
                source_asset_ids_json=store.dumps(source_ids),
                validation_state="pending", validation_json="[]",
            )
            if won:
                submitted += 1
                continue
            # Lost the transition. A concurrent submission (the listener and a
            # read-time reconcile can both find a child runnable) may have linked
            # this very job, recovered through its request id: that is success,
            # not something to undo. Only a job this call created and no item
            # owns - its item was canceled meanwhile - is canceled.
            current = store.get_item(item.id)
            if current is not None and current.job_id == job.id:
                continue
            if created:
                cancel_job(int(job.id))
    return submitted


# ---- completion -------------------------------------------------------------------------------
@dataclass
class Outcome:
    set_id: int
    runnable: list[int] = field(default_factory=list)


def _finishing_result(stage: Any, meta: dict[str, Any]) -> validators.Result | None:
    """Did every requested finishing step actually run? A skipped step degrades
    to the unfinished image by design, so without this a set asking for
    transparent PNGs could quietly collect opaque ones."""
    requested = [str(s.get("processor")) for s in stage.finishing]
    if not requested:
        return None
    ran = [str(r.get("operation")) for r in meta.get("post") or [] if isinstance(r, dict)]
    missing = [p for p in requested if p not in ran]
    details = {"requested": requested, "ran": [p for p in ran if p in requested]}
    if missing:
        warnings = [w for w in meta.get("warnings") or [] if isinstance(w, str)]
        return validators.Result("finishing", "fail",
                                 f"requested finishing did not complete: {', '.join(missing)}",
                                 {**details, "missing": missing, "warnings": warnings[:5]})
    return validators.Result("finishing", "pass", "every requested finishing step ran", details)


def _judge(recipe: RecipeSpec, item: VariantItem,
           asset_ids: list[int]) -> tuple[str, str, str, list[dict[str, Any]]]:
    """(state, reason, validation state, results) for a finished child."""
    live = db.get_assets(asset_ids[:1])
    if not live:
        return FAILED, "the output is no longer in the library", "skipped", []
    asset = live[0]
    stage = recipe.stages[item.stage]
    verdict, results = validators.run(
        asset.path, stage.validation, meta=asset.meta, asset_id=asset.id,
        source_asset_ids=tuple(item.source_asset_ids))
    finishing = _finishing_result(stage, asset.meta)
    if finishing is not None:
        results.append(finishing)
        verdict = validators.verdict(results)
    rows = [r.as_dict() for r in results]
    if verdict == "failed":
        failed = next(r for r in results if r.status == "fail")
        return INVALID, failed.message, verdict, rows
    return SUCCEEDED, "", verdict, rows


def _block_descendants(item: VariantItem, reason: str) -> None:
    assert item.id is not None
    for child in store.descendants_of(item.id):
        if child.id is not None:
            store.transition(child.id, when_state=(PENDING,), state=BLOCKED, state_reason=reason)


def process_job_terminal(job_id: int) -> Outcome | None:
    """Record a finished child: judge its output and decide what its dependents
    do next. Safe to call twice for one job — only the first call moves the item.
    Synchronous (it reads image files); the listener runs it off the loop."""
    item = store.item_for_job(job_id)
    job = db.get_job(job_id)
    if item is None or item.id is None or job is None or item.state != QUEUED:
        return None
    if job.status not in (JobStatus.done.value, JobStatus.error.value, JobStatus.canceled.value):
        return None
    row = store.get_set(item.set_id)
    if row is None:
        return None
    recipe = snapshot_spec(row)
    asset_ids = [int(a) for a in (job.result.get("asset_ids") or db.asset_ids_for_job(job_id))]
    validation_state: str = "skipped"
    results: list[dict[str, Any]] = []
    if job.status == JobStatus.done.value:
        if asset_ids:
            state, reason, validation_state, results = _judge(recipe, item, asset_ids)
        else:
            state, reason = FAILED, job.message or "the operation produced no image"
    elif job.status == JobStatus.error.value:
        state = FAILED
        reason = job.tip or (job.error or "the job failed").strip().splitlines()[0][:300]
    else:
        state, reason = CANCELED, job.message or "canceled"
    won = store.transition(
        item.id, when_state=(QUEUED,), when_job=job_id, state=state, state_reason=reason,
        asset_ids_json=store.dumps(asset_ids), validation_state=validation_state,
        validation_json=store.dumps(results),
    )
    if not won:
        return None
    outcome = Outcome(item.set_id)
    if state == SUCCEEDED:
        if row.collection_id is not None and asset_ids:
            db.add_to_collection(row.collection_id, asset_ids)
        outcome.runnable = [int(c.id) for c in store.children_of(item.id)
                            if c.id is not None and c.state == PENDING]
    else:
        _block_descendants(item, f"waiting on {item.key or 'its source'}, which {state}")
    store.refresh_set(item.set_id)
    return outcome


def on_job_terminal(job: Job) -> Any:
    """Queue listener: cheap filter here, the work in a task."""
    if not is_variant_group(job.group_id) or job.id is None:
        return None
    return advance_after_job(int(job.id))


async def advance_after_job(job_id: int) -> None:
    try:
        outcome = await asyncio.to_thread(process_job_terminal, job_id)
        if outcome is None:
            return
        row = store.get_set(outcome.set_id)
        if row is not None and outcome.runnable:
            await submit_items(row, store.get_items(outcome.runnable))
        _refresh(outcome.set_id)
    except Exception:  # a failure here is recovered by reconciliation, never lost
        logger.warning("could not advance variant set after job %s", job_id, exc_info=True)


# ---- user actions ---------------------------------------------------------------------------------
def _archive(item: VariantItem) -> str:
    """The item's history with its latest attempt appended."""
    history = item.history
    if item.job_id is not None or item.attempts:
        history.append({
            "attempt": item.attempts, "job_id": item.job_id, "state": item.state,
            "reason": item.state_reason, "asset_ids": item.asset_ids,
            "validation_state": item.validation_state,
            "seed": item.params.get("seed"), "at": _now().isoformat(),
        })
    return store.dumps(history)


def _reset(item: VariantItem, *, params: dict[str, Any] | None = None) -> bool:
    """Back to pending, keeping the finished attempt in history."""
    assert item.id is not None
    fields: dict[str, Any] = {
        "state": PENDING, "state_reason": "", "history_json": _archive(item),
        "asset_ids_json": "[]", "validation_state": "pending", "validation_json": "[]",
    }
    if params is not None:
        fields["params_json"] = store.params_json(params)
    return store.transition(item.id, when_state=(*store.FINISHED, BLOCKED),
                            when_job=item.job_id, **fields)


def _runnable_now(items: list[VariantItem]) -> tuple[list[VariantItem], list[VariantItem]]:
    """Split pending items into (can run now, must wait or be blocked)."""
    parents = {int(p.id): p for p in store.get_items(
        {int(i.parent_item_id) for i in items if i.parent_item_id is not None}) if p.id is not None}
    ready: list[VariantItem] = []
    other: list[VariantItem] = []
    for item in items:
        parent = parents.get(int(item.parent_item_id)) if item.parent_item_id is not None else None
        (ready if item.parent_item_id is None or (parent and parent.state == SUCCEEDED)
         else other).append(item)
    return ready, other


def _unblock_descendants(item: VariantItem) -> None:
    """A parent is running again: its dependents wait instead of staying blocked."""
    assert item.id is not None
    for child in store.descendants_of(item.id):
        if child.id is not None and child.state == BLOCKED:
            store.transition(child.id, when_state=(BLOCKED,), state=PENDING, state_reason="")


def _require(set_id: int) -> VariantSet:
    row = store.get_set(set_id)
    if row is None:
        raise VariantSetError(404, "variant set not found")
    return row


async def retry(set_id: int, *, include_canceled: bool = False) -> dict[str, Any]:
    """Run failed and invalid items again (and canceled ones on request).
    Succeeded items are never touched."""
    row = _require(set_id)
    targets = set(store.RETRYABLE) | ({CANCELED} if include_canceled else set())
    candidates = [i for i in store.items_for_set(set_id) if i.state in targets]
    if row.canceled_at is not None:
        store.update_set(set_id, canceled_at=None)
        row = _require(set_id)
    reset = [i for i in candidates if _reset(i)]
    for item in reset:
        _unblock_descendants(item)
    fresh = store.get_items(int(i.id) for i in reset if i.id is not None)
    ready, waiting = _runnable_now(fresh)
    for item in waiting:
        _settle_waiting(item)
    submitted = await submit_items(row, ready)
    _refresh(set_id)
    return {"retried": len(reset), "submitted": submitted}


def _settle_waiting(item: VariantItem) -> None:
    """A pending item whose parent is not running and did not succeed is blocked."""
    parent = store.get_item(int(item.parent_item_id)) if item.parent_item_id is not None else None
    if parent is not None and parent.state not in (PENDING, QUEUED, SUCCEEDED) and item.id:
        store.transition(item.id, when_state=(PENDING,), state=BLOCKED,
                         state_reason=f"waiting on {parent.key or 'its source'}, which {parent.state}")


async def rerun_item(set_id: int, item_id: int, *, reseed: bool = False,
                     cascade: bool = False) -> VariantItem:
    """Run one variant again on purpose — including one that succeeded.

    `reseed` gives it a fresh seed (recorded, so the new attempt is replayable).
    `cascade` also resets everything derived from it, so later stages rebuild
    from the new output; without it they keep what they already made.
    """
    row = _require(set_id)
    item = store.get_item(item_id)
    if item is None or item.set_id != set_id:
        raise VariantSetError(404, "variant not found in this set")
    if item.state in (PENDING, QUEUED):
        raise VariantSetError(409, "this variant is already queued")
    if item.state == BLOCKED:
        raise VariantSetError(409, "this variant is blocked: retry the variant it derives from first")
    if item.parent_item_id is not None:
        parent = store.get_item(int(item.parent_item_id))
        if parent is None or parent.state != SUCCEEDED or not parent.asset_ids:
            raise VariantSetError(409, "the variant it derives from has no successful output")
    params = item.params
    if reseed:
        params["seed"] = resolve_seed(None)
    if row.canceled_at is not None:
        store.update_set(set_id, canceled_at=None)
        row = _require(set_id)
    if not _reset(item, params=params):
        raise VariantSetError(409, "this variant changed while the rerun was requested")
    if cascade:
        # Dependents rebuild from the new output: their stored request replaces
        # the effective params of the attempt that used the old one.
        spec = snapshot_spec(row)
        for child in store.descendants_of(item_id):
            if child.id is None or child.state in (PENDING, QUEUED):
                continue
            request = render_request(spec.content, spec.stages[child.stage], child.values,
                                     child.params.get("seed"))
            _reset(child, params=request)
    else:
        _unblock_descendants(item)
    fresh = store.get_item(item_id)
    if fresh is not None:
        await submit_items(row, [fresh])
    _refresh(set_id)
    updated = store.get_item(item_id)
    assert updated is not None
    return updated


async def cancel(set_id: int) -> dict[str, Any]:
    """Stop the remaining work. Finished items keep their results."""
    from ..queue import cancel as cancel_job

    _require(set_id)
    store.update_set(set_id, canceled_at=_now())
    stopped = 0
    for item in store.items_for_set(set_id):
        if item.id is None:
            continue
        if item.state in (PENDING, BLOCKED):
            if store.transition(item.id, when_state=(PENDING, BLOCKED), state=CANCELED,
                                state_reason="canceled with the set"):
                stopped += 1
        elif item.state == QUEUED and item.job_id is not None:
            cancel_job(int(item.job_id))   # a queued job resolves now, a running one soon
            stopped += 1
    _refresh(set_id)
    return {"canceled": stopped}


def delete(set_id: int) -> None:
    row = _require(set_id)
    if row.status == store.ACTIVE:
        raise VariantSetError(409, "cancel the set before deleting it")
    store.delete_set(set_id)


async def retry_for_job(job_id: int) -> dict[str, Any] | None:
    """Queue-page Retry on a failed child retries its variant, so the set keeps
    tracking it instead of losing the new attempt to an unrelated job."""
    item = store.item_for_job(job_id)
    if item is None or item.id is None or item.state not in store.RETRYABLE | {CANCELED}:
        return None
    row = store.get_set(item.set_id)
    if row is None:
        return None
    updated = await rerun_item(item.set_id, item.id)
    return {"job_id": updated.job_id, "variant_set_id": item.set_id,
            "replaced_job_id": job_id}


# ---- recovery -------------------------------------------------------------------------------------
async def reconcile_set(set_id: int) -> None:
    """Bring a set's items back in line with its jobs.

    Handles everything a missed listener call could leave behind: a child that
    finished (or was canceled by restart reconciliation) while nobody was
    listening, a runnable item whose job was never created, and an item waiting
    on a parent that will never succeed.
    """
    row = store.get_set(set_id)
    if row is None:
        return
    items = store.items_for_set(set_id)
    queued_jobs = {int(i.job_id) for i in items if i.state == QUEUED and i.job_id is not None}
    jobs = {int(j.id): j for j in _jobs(queued_jobs) if j.id is not None}
    runnable: list[int] = []
    for job_id, job in jobs.items():
        if job.status in (JobStatus.done.value, JobStatus.error.value, JobStatus.canceled.value):
            outcome = await asyncio.to_thread(process_job_terminal, job_id)
            if outcome:
                runnable.extend(outcome.runnable)
    # A queued item whose job row is gone (pruned, or a wiped history) cannot finish.
    for item in items:
        if item.state == QUEUED and item.job_id is not None and int(item.job_id) not in jobs \
                and item.id is not None:
            store.transition(item.id, when_state=(QUEUED,), when_job=item.job_id,
                             state=FAILED, state_reason="its job no longer exists")
    pending = [i for i in store.items_for_set(set_id) if i.state == PENDING]
    if row.canceled_at is not None:
        for item in pending:
            if item.id is not None:
                store.transition(item.id, when_state=(PENDING,), state=CANCELED,
                                 state_reason="canceled with the set")
    else:
        ready, waiting = _runnable_now(pending)
        for item in waiting:
            _settle_waiting(item)
        runnable.extend(int(i.id) for i in ready if i.id is not None)
        if runnable:
            await submit_items(row, store.get_items(set(runnable)))
    _refresh(set_id)


def _jobs(ids: set[int]) -> list[Job]:
    if not ids:
        return []
    from sqlmodel import select

    with db.session() as s:
        return list(s.exec(select(Job).where(Job.id.in_(ids))))  # type: ignore[union-attr]


async def reconcile_all() -> int:
    """At startup, after queued jobs are back on their lanes."""
    count = 0
    for row in store.sets_with_status((store.ACTIVE,)):
        if row.id is None:
            continue
        try:
            await reconcile_set(int(row.id))
            count += 1
        except Exception:  # one damaged set must not stop the others recovering
            logger.warning("could not reconcile variant set %s", row.id, exc_info=True)
    return count


# ---- read models ------------------------------------------------------------------------------------
def set_view(row: VariantSet) -> dict[str, Any]:
    recipe = row.recipe
    return {
        "id": row.id, "name": row.name, "status": row.status, "operation": row.operation,
        "recipe_id": row.recipe_id, "group_id": row.group_id, "expected": row.expected,
        "counts": row.counts, "collection_id": row.collection_id,
        "source_asset_ids": row.source_asset_ids,
        "stages": [{"index": i, "name": s.get("name", ""), "operation": s.get("operation"),
                    "axes": s.get("axes", [])} for i, s in enumerate(recipe.get("stages", []))],
        "replay": row.replay, "canceled_at": row.canceled_at,
        "created_at": row.created_at, "updated_at": row.updated_at,
    }


def item_views(items: list[VariantItem]) -> list[dict[str, Any]]:
    """Items with their live job state and output thumbnails, in three queries."""
    from ..models import AssetRead

    jobs = {int(j.id): j for j in _jobs({int(i.job_id) for i in items if i.job_id is not None})
            if j.id is not None}
    asset_ids = [a for i in items for a in i.asset_ids[:1]]
    assets = {int(a.id): a for a in db.get_assets(asset_ids) if a.id is not None}
    out = []
    for item in items:
        job = jobs.get(int(item.job_id)) if item.job_id is not None else None
        first = item.asset_ids[0] if item.asset_ids else None
        asset = assets.get(first) if first is not None else None
        params = item.params
        out.append({
            "id": item.id, "stage": item.stage, "ordinal": item.ordinal, "key": item.key,
            "values": item.values, "state": item.state, "state_reason": item.state_reason,
            "job_id": item.job_id, "job_status": job.status if job else None,
            "progress": job.progress if job else None,
            "attempts": item.attempts, "parent_item_id": item.parent_item_id,
            "source_asset_ids": item.source_asset_ids, "asset_ids": item.asset_ids,
            "asset": AssetRead.of(asset).model_dump(mode="json") if asset else None,
            "validation_state": item.validation_state, "validation": item.validation,
            "output_name": item.output_name,
            "prompt": str(params.get("prompt", ""))[:MAX_PROMPT_CHARS],
            "seed": params.get("seed"), "history": item.history,
        })
    return out


def params_of(item: VariantItem) -> dict[str, Any]:
    return json.loads(item.params_json or "{}")
