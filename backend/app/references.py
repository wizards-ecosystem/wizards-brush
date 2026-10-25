"""Reference sets: named, revisioned, ordered references for presets and runs.

A set groups gallery asset ids by role (``character``, ``product``, ``style``
or any other role name), in order. Each entry may carry a mask painted on that
asset and a short note. The set is reference material only: it is not
training data, and nothing here claims a model will preserve an identity.

Every save writes a new immutable revision and moves the head with a
compare-and-set on ``base_revision``, so two editors cannot silently overwrite
each other, and a plan that pinned revision 2 can always read revision 2. The
resolver reads a set through :func:`role_entries`; it pins the revision it
used in the plan, so editing the set never changes work already queued.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from . import db, log
from .models import ReferenceSet, ReferenceSetRevision
from .problems import RequestError
from .studio.contracts import digest

logger = log.get("references")

MAX_ROLES = 8
MAX_ENTRIES = 16
MAX_REPRESENTATIVES = 4


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: int = Field(ge=1)
    mask_id: int | None = Field(default=None, ge=1,
                                description="A mask painted on this asset: white marks the subject.")
    note: str = Field(default="", max_length=400)


class Role(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    label: str = Field(default="", max_length=80)
    entries: list[Entry] = Field(min_length=1, max_length=MAX_ENTRIES)


class Definition(BaseModel):
    """What one revision holds. Order is meaning and is kept exactly."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(default="", max_length=1000)
    notes: str = Field(default="", max_length=2000,
                       description="Text a preset role with notes_in_prompt may place in "
                                   "{{notes}}.")
    roles: list[Role] = Field(min_length=1, max_length=MAX_ROLES)
    representatives: list[int] = Field(
        default_factory=list, max_length=MAX_REPRESENTATIVES,
        description="Approved representative asset ids; each must be an entry of this set.")

    @model_validator(mode="after")
    def _shape(self) -> Definition:
        names = [r.role for r in self.roles]
        if len(set(names)) != len(names):
            raise ValueError("roles has a duplicate role")
        members = {e.asset_id for r in self.roles for e in r.entries}
        stray = [a for a in self.representatives if a not in members]
        if stray:
            raise ValueError(f"representatives {stray} are not entries of this set")
        return self


def _bad(message: str, field: str, code: str = "invalid_request") -> RequestError:
    return RequestError(400, message, code=code, field=field)


def parse(raw: Any) -> Definition:
    try:
        return Definition.model_validate(raw)
    except ValidationError as error:
        first = error.errors()[0]
        path = ".".join(str(p) for p in first.get("loc", ())) or "definition"
        raise _bad(f"{path}: {first.get('msg')}", f"definition.{path}".rstrip(".")) from None


def _check_assets(definition: Definition) -> None:
    """Every entry names a live image; a mask is the size of its image."""
    for r_index, role in enumerate(definition.roles):
        for e_index, entry in enumerate(role.entries):
            where = f"definition.roles.{r_index}.entries.{e_index}"
            asset = db.get_asset(entry.asset_id)
            if asset is None:
                raise RequestError(404, f"{where}: asset {entry.asset_id} does not exist or is "
                                        "in the trash", code="not_found", field=f"{where}.asset_id")
            if asset.kind != "image":
                raise _bad(f"{where}: asset {entry.asset_id} is a {asset.kind}, not an image",
                           f"{where}.asset_id")
            if entry.mask_id is None:
                continue
            mask = db.get_asset(entry.mask_id)
            if mask is None or mask.kind != "image":
                raise _bad(f"{where}: mask {entry.mask_id} is not a live image asset",
                           f"{where}.mask_id")
            if (mask.width, mask.height) != (asset.width, asset.height):
                raise _bad(f"{where}: the mask is {mask.width}x{mask.height} but the image is "
                           f"{asset.width}x{asset.height}; a mask must match its image",
                           f"{where}.mask_id", code="invalid_mask")


def _clean_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned or len(cleaned) > 80:
        raise _bad("name must be 1-80 characters", "name")
    return cleaned


def _view(head: ReferenceSet, row: ReferenceSetRevision) -> dict[str, Any]:
    definition = row.definition
    return {
        "id": head.id, "name": head.name, "revision": row.revision,
        "head_revision": head.revision, "digest": row.digest,
        "archived": head.archived_at is not None, "definition": definition,
        "roles": [{"role": r["role"], "label": r.get("label", ""), "count": len(r["entries"])}
                  for r in definition.get("roles", [])],
        "client": row.client, "created_at": head.created_at, "updated_at": head.updated_at,
    }


def _summary(head: ReferenceSet, row: ReferenceSetRevision | None) -> dict[str, Any]:
    definition = row.definition if row is not None else {}
    return {
        "id": head.id, "name": head.name, "head_revision": head.revision,
        "archived": head.archived_at is not None,
        "description": definition.get("description", ""),
        "roles": [{"role": r["role"], "label": r.get("label", ""), "count": len(r["entries"])}
                  for r in definition.get("roles", [])],
        "representatives": definition.get("representatives", []),
        "updated_at": head.updated_at,
    }


def _revision(s: Any, set_id: int, revision: int) -> ReferenceSetRevision | None:
    return s.exec(select(ReferenceSetRevision).where(
        ReferenceSetRevision.set_id == set_id, ReferenceSetRevision.revision == revision)).first()


def _name_taken(name: str) -> RequestError:
    return RequestError(409, f"a reference set named '{name}' already exists",
                        code="name_taken", field="name",
                        suggestions=["Choose another name, or update the existing set."])


def create(name: str, raw: Any, *, client: str = "") -> dict[str, Any]:
    definition = parse(raw)
    _check_assets(definition)
    clean = _clean_name(name)
    body = definition.model_dump()
    hashed = digest(body, domain="reference-set")
    now = datetime.now(UTC)
    with db.session() as s:
        head = ReferenceSet(name=clean, revision=1, digest=hashed, client=client,
                            created_at=now, updated_at=now)
        s.add(head)
        try:
            s.flush()
            row = ReferenceSetRevision(set_id=int(head.id or 0), revision=1,
                                       definition_json=json.dumps(body), digest=hashed,
                                       client=client, created_at=now)
            s.add(row)
            s.commit()
        except IntegrityError:
            s.rollback()
            raise _name_taken(clean) from None
        s.refresh(head)
        s.refresh(row)
        logger.info("created reference set %s (%s)", head.id, clean)
        return _view(head, row)


def update(set_id: int, raw: Any, *, base_revision: int, name: str | None = None,
           client: str = "") -> dict[str, Any]:
    """Write a new revision if the head is still `base_revision`."""
    definition = parse(raw)
    _check_assets(definition)
    body = definition.model_dump()
    hashed = digest(body, domain="reference-set")
    now = datetime.now(UTC)
    with db.session() as s:
        head = s.get(ReferenceSet, set_id)
        if head is None:
            raise _missing(set_id)
        if head.revision != base_revision:
            raise RequestError(
                409, f"reference set {set_id} is at revision {head.revision}; this save started "
                     f"from revision {base_revision} and was not applied",
                code="revision_conflict", field="base_revision", retryable=False,
                state={"current_revision": head.revision, "current_digest": head.digest,
                       "attempted_digest": hashed},
                suggestions=["Read the current revision and reapply your change."])
        if name is not None:
            head.name = _clean_name(name)
        head.revision = base_revision + 1
        head.digest = hashed
        head.updated_at = now
        s.add(head)
        try:
            s.flush()
            row = ReferenceSetRevision(set_id=set_id, revision=head.revision,
                                       definition_json=json.dumps(body), digest=hashed,
                                       based_on=base_revision, client=client, created_at=now)
            s.add(row)
            s.commit()
        except IntegrityError:
            s.rollback()
            current = s.get(ReferenceSet, set_id)
            if current is not None and current.revision != base_revision:
                raise RequestError(409, f"reference set {set_id} changed while saving",
                                   code="revision_conflict", field="base_revision",
                                   state={"current_revision": current.revision}) from None
            raise _name_taken(head.name) from None
        s.refresh(head)
        s.refresh(row)
        return _view(head, row)


def _missing(set_id: int) -> RequestError:
    return RequestError(404, f"reference set {set_id} does not exist", code="not_found",
                        field="reference_set",
                        suggestions=["List reference sets with GET /api/reference-sets."])


def get(set_id: int, revision: int | None = None) -> dict[str, Any]:
    """One revision (the head by default). Archived sets stay readable."""
    with db.session() as s:
        head = s.get(ReferenceSet, set_id)
        if head is None:
            raise _missing(set_id)
        row = _revision(s, set_id, revision or head.revision)
        if row is None:
            raise RequestError(404, f"reference set {set_id} has no revision {revision}",
                               code="not_found", field="revision",
                               valid_options=list(range(1, head.revision + 1)))
        return _view(head, row)


def listing(*, include_archived: bool = False) -> list[dict[str, Any]]:
    with db.session() as s:
        query = select(ReferenceSet).order_by(func.lower(col(ReferenceSet.name)))
        if not include_archived:
            query = query.where(col(ReferenceSet.archived_at).is_(None))
        heads = list(s.exec(query))
        return [_summary(h, _revision(s, int(h.id or 0), h.revision)) for h in heads]


def archive(set_id: int, archived: bool = True) -> dict[str, Any]:
    """Hide a set from pickers. Its revisions stay readable for old plans."""
    with db.session() as s:
        head = s.get(ReferenceSet, set_id)
        if head is None:
            raise _missing(set_id)
        head.archived_at = datetime.now(UTC) if archived else None
        head.updated_at = datetime.now(UTC)
        s.add(head)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            raise _name_taken(head.name) from None
        s.refresh(head)
        row = _revision(s, set_id, head.revision)
        assert row is not None
        return _view(head, row)


def role_entries(set_id: int, revision: int | None, role: str) -> dict[str, Any]:
    """What the resolver binds for one role: the pinned revision and its entries.

    Archived sets still resolve when named by a preset: archiving hides a set
    from pickers, it does not break a setup that already uses it.
    """
    found = get(set_id, revision)
    roles = {r["role"]: r for r in found["definition"]["roles"]}
    if role not in roles:
        raise RequestError(400, f"reference set {set_id} ('{found['name']}') has no "
                                f"{role!r} role", code="invalid_request",
                           field=f"inputs.reference_sets.{role}", valid_options=list(roles))
    return {"id": found["id"], "name": found["name"], "revision": found["revision"],
            "digest": found["digest"], "notes": found["definition"].get("notes", ""),
            "entries": roles[role]["entries"]}
