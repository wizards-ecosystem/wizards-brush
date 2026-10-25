"""Persistence for resources and their installs.

Upserts keep the identity fixed and update only the facts passed, so a later
scan can add an observation without erasing what an import recorded.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from .. import db
from ..models import Resource, ResourceInstall

_JSON_FIELDS = {"files": "files_json", "license": "license_json", "compat": "compat_json",
                "constraints": "constraints_json"}


def _now() -> datetime:
    return datetime.now(UTC)


def _detach(s: Any, row: Any) -> Any:
    for name in type(row).model_fields:
        getattr(row, name)
    s.expunge(row)
    return row


def get(resource_id: str) -> Resource | None:
    with db.session() as s:
        row = s.get(Resource, resource_id)
        return None if row is None else _detach(s, row)


def by_hash(content_hash: str) -> list[Resource]:
    with db.session() as s:
        rows = list(s.exec(select(Resource).where(Resource.content_hash == content_hash)))
        return [_detach(s, row) for row in rows]


def listing(kind: str | None = None) -> list[Resource]:
    with db.session() as s:
        query = select(Resource)
        if kind:
            query = query.where(Resource.kind == kind)
        rows = list(s.exec(query.order_by(col(Resource.kind), col(Resource.label))))
        return [_detach(s, row) for row in rows]


def upsert(resource_id: str, **facts: Any) -> Resource:
    """Insert the resource or update the given facts on the existing row.

    A library scan and an import can register the same new file at once;
    the one that loses the insert race retries as an update.
    """
    try:
        return _upsert(resource_id, facts)
    except IntegrityError:
        return _upsert(resource_id, facts)


def _upsert(resource_id: str, facts: dict[str, Any]) -> Resource:
    with db.session() as s:
        row = s.get(Resource, resource_id)
        if row is None:
            row = Resource(id=resource_id, created_at=_now())
        for key, value in facts.items():
            column = _JSON_FIELDS.get(key)
            if column is not None:
                setattr(row, column, json.dumps(value, ensure_ascii=False))
            else:
                setattr(row, key, value)
        row.updated_at = _now()
        s.add(row)
        s.commit()
        s.refresh(row)
        return _detach(s, row)


def installs(resource_id: str | None = None, *, target: str | None = None) -> list[ResourceInstall]:
    with db.session() as s:
        query = select(ResourceInstall)
        if resource_id is not None:
            query = query.where(ResourceInstall.resource_id == resource_id)
        if target is not None:
            query = query.where(ResourceInstall.target == target)
        rows = list(s.exec(query.order_by(col(ResourceInstall.id))))
        return [_detach(s, row) for row in rows]


def install_at(target: str, location: str) -> ResourceInstall | None:
    with db.session() as s:
        row = s.exec(select(ResourceInstall).where(ResourceInstall.target == target,
                                                   ResourceInstall.location == location)).first()
        return None if row is None else _detach(s, row)


def upsert_install(resource_id: str, target: str, location: str, **facts: Any) -> ResourceInstall:
    """One install per (target, location). A location that now holds different
    content is re-pointed at the new resource rather than duplicated. A lost
    insert race (scan and import at once) retries as an update."""
    try:
        return _upsert_install(resource_id, target, location, facts)
    except IntegrityError:
        return _upsert_install(resource_id, target, location, facts)


def _upsert_install(resource_id: str, target: str, location: str,
                    facts: dict[str, Any]) -> ResourceInstall:
    with db.session() as s:
        row = s.exec(select(ResourceInstall).where(ResourceInstall.target == target,
                                                   ResourceInstall.location == location)).first()
        if row is None:
            row = ResourceInstall(resource_id=resource_id, target=target, location=location,
                                  created_at=_now())
        row.resource_id = resource_id
        for key, value in facts.items():
            setattr(row, key, value)
        row.updated_at = _now()
        s.add(row)
        s.commit()
        s.refresh(row)
        return _detach(s, row)


def mark_installs(ids: Iterable[int], **facts: Any) -> None:
    wanted = list(ids)
    if not wanted:
        return
    with db.session() as s:
        for row in s.exec(select(ResourceInstall).where(col(ResourceInstall.id).in_(wanted))):
            for key, value in facts.items():
                setattr(row, key, value)
            row.updated_at = _now()
            s.add(row)
        s.commit()
