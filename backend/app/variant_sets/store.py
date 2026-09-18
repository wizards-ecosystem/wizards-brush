"""Persistence for Variant Sets and recipes.

Item transitions are compare-and-set: an UPDATE guarded by the state (and job)
the caller expects, reporting whether it matched. A completion arriving from
the queue listener, the same completion found again by restart reconciliation,
and a user's cancel can all race for one row; exactly one of them may move it.

Set counts are a cache recomputed from the items in one query — never
incremented — so they cannot drift from what the items say.
"""
from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import desc, select

from .. import db
from ..models import Job, JobStatus, VariantItem, VariantRecipe, VariantSet, stamp_params

# Item states. `queued` covers a child job that is queued *or running*; which
# one is read from the job itself.
PENDING, QUEUED = "pending", "queued"
SUCCEEDED, INVALID, FAILED, CANCELED, BLOCKED = (
    "succeeded", "invalid", "failed", "canceled", "blocked")
ITEM_STATES = (PENDING, QUEUED, SUCCEEDED, INVALID, FAILED, CANCELED, BLOCKED)
IN_FLIGHT = frozenset({PENDING, QUEUED})
RETRYABLE = frozenset({FAILED, INVALID})
FINISHED = frozenset({SUCCEEDED, INVALID, FAILED, CANCELED})

# Set status.
ACTIVE, COMPLETE, INCOMPLETE, SET_CANCELED = "active", "complete", "incomplete", "canceled"

_ANY: Any = object()


def _now() -> datetime:
    return datetime.now(UTC)


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---- sets ------------------------------------------------------------------------------
def insert_set(row: VariantSet, stages: list[list[dict[str, Any]]]) -> tuple[VariantSet, bool]:
    """Insert a set and every item of every stage in one transaction.

    `stages[k]` lists item fields for stage k; an item's `parent` is an index
    into stage k-1's list and is resolved to that row's id here. Returns
    (set, created); a duplicate `request_id` returns the set that owns it.
    """
    with db.session() as s:
        s.add(row)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            existing = s.exec(select(VariantSet).where(
                VariantSet.request_id == row.request_id)).first() if row.request_id else None
            if existing is None:
                raise
            return existing, False
        assert row.id is not None
        previous_ids: list[int] = []
        for fields_list in stages:
            items = []
            for fields in fields_list:
                parent = fields.pop("parent", None)
                item = VariantItem(set_id=row.id, **fields)
                if parent is not None:
                    item.parent_item_id = previous_ids[parent]
                items.append(item)
            s.add_all(items)
            s.flush()
            previous_ids = [int(item.id) for item in items if item.id is not None]
        s.commit()
        s.refresh(row)
        return row, True


def get_set(set_id: int) -> VariantSet | None:
    with db.session() as s:
        return s.get(VariantSet, set_id)


def set_for_request(request_id: str) -> VariantSet | None:
    with db.session() as s:
        return s.exec(select(VariantSet).where(VariantSet.request_id == request_id)).first()


def list_sets(limit: int = 100, offset: int = 0) -> list[VariantSet]:
    with db.session() as s:
        return list(s.exec(select(VariantSet).order_by(desc(VariantSet.id))
                           .offset(offset).limit(limit)))


def sets_with_status(statuses: Collection[str]) -> list[VariantSet]:
    with db.session() as s:
        return list(s.exec(select(VariantSet).where(
            VariantSet.status.in_(list(statuses)))))  # type: ignore[attr-defined]


def update_set(set_id: int, **fields: Any) -> None:
    with db.session() as s:
        s.execute(sa_update(VariantSet).where(VariantSet.id == set_id)  # type: ignore[arg-type]
                  .values(**fields, updated_at=_now()))
        s.commit()


def delete_set(set_id: int) -> bool:
    """Remove the record of a run. Its jobs and assets are untouched."""
    with db.session() as s:
        row = s.get(VariantSet, set_id)
        if row is None:
            return False
        s.execute(sa_delete(VariantItem).where(VariantItem.set_id == set_id))  # type: ignore[arg-type]
        s.delete(row)
        s.commit()
        return True


def delete_all_sets() -> int:
    """Every set and item — for clear_all(), which wipes the jobs they point at."""
    with db.session() as s:
        n = len(list(s.exec(select(VariantSet.id))))
        s.execute(sa_delete(VariantItem))
        s.execute(sa_delete(VariantSet))
        s.commit()
        return n


def refresh_set(set_id: int) -> VariantSet | None:
    """Recompute counts and status from the items, in one query, and store them."""
    with db.session() as s:
        row = s.get(VariantSet, set_id)
        if row is None:
            return None
        rows = s.exec(
            select(VariantItem.state, VariantItem.stage, Job.status)
            .join(Job, Job.id == VariantItem.job_id, isouter=True)  # type: ignore[arg-type]
            .where(VariantItem.set_id == set_id)
        ).all()
        totals = dict.fromkeys((*ITEM_STATES, "running"), 0)
        stages: dict[int, dict[str, int]] = {}
        for state, stage, job_status in rows:
            shown = "running" if state == QUEUED and job_status == JobStatus.running.value else state
            totals[shown] = totals.get(shown, 0) + 1
            per = stages.setdefault(int(stage), {"total": 0})
            per["total"] += 1
            per[shown] = per.get(shown, 0) + 1
        active = totals[PENDING] + totals[QUEUED] + totals["running"]
        if active:
            status = ACTIVE
        elif row.canceled_at is not None:
            status = SET_CANCELED
        elif totals[SUCCEEDED] == len(rows):
            status = COMPLETE
        else:
            status = INCOMPLETE
        counts = {"total": len(rows), **totals,
                  "stages": [{"stage": k, **v} for k, v in sorted(stages.items())]}
        row.counts_json = dumps(counts)
        row.status = status
        row.updated_at = _now()
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


# ---- items ------------------------------------------------------------------------------
def items_for_set(set_id: int, stage: int | None = None) -> list[VariantItem]:
    with db.session() as s:
        q = select(VariantItem).where(VariantItem.set_id == set_id)
        if stage is not None:
            q = q.where(VariantItem.stage == stage)
        return list(s.exec(q.order_by(VariantItem.stage, VariantItem.ordinal)))  # type: ignore[arg-type]


def get_items(ids: Iterable[int]) -> list[VariantItem]:
    wanted = list(ids)
    if not wanted:
        return []
    with db.session() as s:
        return list(s.exec(select(VariantItem).where(VariantItem.id.in_(wanted))  # type: ignore[union-attr]
                           .order_by(VariantItem.stage, VariantItem.ordinal)))  # type: ignore[arg-type]


def get_item(item_id: int) -> VariantItem | None:
    with db.session() as s:
        return s.get(VariantItem, item_id)


def item_for_job(job_id: int) -> VariantItem | None:
    with db.session() as s:
        return s.exec(select(VariantItem).where(VariantItem.job_id == job_id)).first()


def children_of(item_id: int) -> list[VariantItem]:
    with db.session() as s:
        return list(s.exec(select(VariantItem).where(VariantItem.parent_item_id == item_id)
                           .order_by(VariantItem.ordinal)))  # type: ignore[arg-type]


def descendants_of(item_id: int) -> list[VariantItem]:
    """Every item derived from this one, nearest stage first."""
    out: list[VariantItem] = []
    frontier = [item_id]
    while frontier:
        with db.session() as s:
            level = list(s.exec(select(VariantItem).where(
                VariantItem.parent_item_id.in_(frontier))  # type: ignore[union-attr]
                .order_by(VariantItem.ordinal)))  # type: ignore[arg-type]
        out.extend(level)
        frontier = [int(i.id) for i in level if i.id is not None]
    return out


def transition(item_id: int, *, when_state: Collection[str] | None = None,
               when_job: Any = _ANY, **fields: Any) -> bool:
    """Move one item, only if it is still in the expected state (and owned by
    the expected job). Returns whether this caller won."""
    stmt = sa_update(VariantItem).where(VariantItem.id == item_id)  # type: ignore[arg-type]
    if when_state is not None:
        stmt = stmt.where(VariantItem.state.in_(list(when_state)))  # type: ignore[attr-defined]
    if when_job is not _ANY:
        stmt = stmt.where(VariantItem.job_id.is_(None) if when_job is None  # type: ignore[union-attr]
                          else VariantItem.job_id == when_job)
    with db.session() as s:
        result = s.execute(stmt.values(**fields, updated_at=_now()))
        s.commit()
        return bool(getattr(result, "rowcount", 0) == 1)


def params_json(params: dict[str, Any]) -> str:
    return dumps(stamp_params(params))


def referenced_job_ids() -> Any:
    """A subquery of every job a variant item points at, for history pruning."""
    return select(VariantItem.job_id).where(VariantItem.job_id.is_not(None))  # type: ignore[union-attr]


# ---- recipes ---------------------------------------------------------------------------------
def list_recipes() -> list[VariantRecipe]:
    with db.session() as s:
        return list(s.exec(select(VariantRecipe).order_by(desc(VariantRecipe.updated_at))))


def get_recipe(recipe_id: int) -> VariantRecipe | None:
    with db.session() as s:
        return s.get(VariantRecipe, recipe_id)


def save_recipe(name: str, description: str, config: dict[str, Any],
                recipe_id: int | None = None) -> VariantRecipe | None:
    with db.session() as s:
        row = s.get(VariantRecipe, recipe_id) if recipe_id is not None else VariantRecipe()
        if row is None:
            return None
        row.name, row.description = name, description
        row.config_json = dumps(config)
        row.updated_at = _now()
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def delete_recipe(recipe_id: int) -> bool:
    with db.session() as s:
        row = s.get(VariantRecipe, recipe_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True
