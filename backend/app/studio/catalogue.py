"""The preset catalogue: curated manifests and custom presets, by revision.

Storage is the `recipe` table (schema_version 2 rows are preset heads) and the
append-only `reciperevision` table. This module owns the rules:

* **Curated revisions are immutable.** They ship as one JSON file per revision
  under ``studio/curated/`` and are checked against ``LOCK.json``, so a shipped
  revision that was edited in place is refused instead of silently retuning
  saved work. Saving an edit to a curated preset makes a custom copy.
* **Every custom save is a new revision**, made only from the revision the
  caller says it started from. A save from a stale base is a conflict that
  loses neither version: the head stays, and the attempt can be kept as a copy.
* **Archive, never delete.** Jobs and Variant Set snapshots name
  (preset_id, revision); those revisions stay readable.

Nothing here resolves, validates against the live registry or runs a preset;
that is the resolver's job. Storage checks the schema only, so a valid preset
whose model is not installed here still saves.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from .. import db, log
from ..problems import RequestError
from . import contracts

logger = log.get("presets")

CURATED_DIR = Path(__file__).with_name("curated")
_MANIFEST = re.compile(r"^(?P<name>[a-z0-9][a-z0-9.-]*)\.r(?P<rev>[1-9][0-9]*)\.json$")


# ---- views ----------------------------------------------------------------------------
def view(head: Any, *, revision: Any = None) -> dict[str, Any]:
    """The document HTTP and MCP return. `revision` selects an older one."""
    definition = dict(revision.definition if revision is not None else head.definition)
    rev = int(revision.revision if revision is not None else head.revision or 1)
    return {
        "id": head.preset_id,
        "revision": rev,
        "head_revision": int(head.revision or 1),
        "name": str(definition.get("name") or head.name),
        "description": str(definition.get("description") or ""),
        "origin": head.origin,
        "tags": list(definition.get("tags") or []),
        "operation": definition.get("operation"),
        "archived": head.archived_at is not None,
        "client": (revision.client if revision is not None else head.client) or "",
        "definition_hash": (revision.definition_hash if revision is not None
                            else head.definition_hash),
        "created_at": revision.created_at if revision is not None else head.created_at,
        "updated_at": head.updated_at,
        "definition": definition,
    }


def _missing(preset_id: str, revision: int | None = None) -> RequestError:
    what = f"revision {revision} of preset '{preset_id}'" if revision else f"preset '{preset_id}'"
    return RequestError(
        404, f"{what} was not found", code="unknown_preset", field="preset_id",
        suggestions=["List presets with GET /api/studio/presets."],
    )


def _contract_error(error: contracts.ContractError) -> RequestError:
    field = f"definition.{error.field}" if error.field else "definition"
    return RequestError(400, str(error), code=error.code, field=field)


# ---- reading ---------------------------------------------------------------------------
def get(preset_id: str, revision: int | None = None) -> dict[str, Any]:
    head = db.preset_head(preset_id)
    if head is None:
        raise _missing(preset_id)
    if revision is None or revision == int(head.revision or 1):
        return view(head)
    row = db.preset_revision(preset_id, revision)
    if row is None:
        raise _missing(preset_id, revision)
    return view(head, revision=row)


def definition(preset_id: str, revision: int | None = None) -> contracts.PresetDefinition:
    """A stored revision as a validated definition (the resolver's input)."""
    return contracts.parse_preset(get(preset_id, revision)["definition"])


def history(preset_id: str) -> list[dict[str, Any]]:
    head = db.preset_head(preset_id)
    if head is None:
        raise _missing(preset_id)
    return [{"revision": r.revision, "based_on": r.based_on, "client": r.client,
             "definition_hash": r.definition_hash, "created_at": r.created_at}
            for r in db.preset_revisions(preset_id)]


def listing(*, include_archived: bool = False, origin: str | None = None,
            tag: str | None = None, query: str | None = None) -> list[dict[str, Any]]:
    out = []
    needle = (query or "").strip().lower()
    for head in db.list_presets(include_archived=include_archived):
        item = view(head)
        if origin and item["origin"] != origin:
            continue
        if tag and tag not in item["tags"]:
            continue
        if needle and needle not in f"{item['name']} {item['description']} {' '.join(item['tags'])}".lower():
            continue
        item.pop("definition")
        out.append(item)
    return out


# ---- writing ---------------------------------------------------------------------------
def _slug_for(preset_id: str) -> str:
    base = "preset-" + re.sub(r"[^a-z0-9]+", "-", preset_id.lower()).strip("-")
    base = base[:60].rstrip("-")
    slug = base
    for number in range(2, 1000):
        if not db.slug_taken(slug):
            return slug
        slug = f"{base[:56].rstrip('-')}-{number}"
    raise RequestError(409, f"no free slug near '{base}'", code="conflict", field="id")


def _new_custom_id(name: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60].strip("-") or "preset"
    candidate = f"custom/{stem}"
    for number in range(2, 1000):
        if db.preset_head(candidate) is None:
            return candidate
        candidate = f"custom/{stem}-{number}"
    raise RequestError(409, "no free preset id for that name", code="conflict", field="id")


def _custom_definition(raw: Mapping[str, Any], *, preset_id: str,
                       revision: int) -> contracts.PresetDefinition:
    if not isinstance(raw, Mapping):
        raise RequestError(400, "definition must be an object", code="invalid_type",
                           field="definition")
    if raw.get("origin") == "curated":
        raise RequestError(
            400, "curated presets ship with the app; save yours as custom",
            code="curated_reserved", field="definition.origin",
            suggestions=["Leave origin out, or send \"custom\"."],
        )
    doc = {**raw, "id": preset_id, "revision": revision, "origin": "custom"}
    doc.setdefault("schema", contracts.PRESET_SCHEMA)
    doc.setdefault("schema_version", contracts.PRESET_SCHEMA_VERSION)
    try:
        return contracts.parse_preset(doc)
    except contracts.ContractError as error:
        raise _contract_error(error) from None


def create(raw: Mapping[str, Any], *, client: str) -> dict[str, Any]:
    """A new custom preset at revision 1."""
    if not isinstance(raw, Mapping):
        raise RequestError(400, "definition must be an object", code="invalid_type",
                           field="definition")
    requested = str(raw.get("id") or "").strip()
    if requested.startswith("curated/"):
        raise RequestError(400, "the curated/ namespace is reserved for shipped presets",
                           code="curated_reserved", field="definition.id")
    if raw.get("revision") not in (None, 1):
        raise RequestError(400, "a new preset starts at revision 1",
                           code="invalid_request", field="definition.revision")
    preset_id = requested or _new_custom_id(str(raw.get("name") or ""))
    if db.preset_head(preset_id) is not None:
        raise RequestError(409, f"preset '{preset_id}' already exists",
                           code="preset_exists", field="definition.id",
                           suggestions=["Update it with its base_revision, or choose another id."])
    preset = _custom_definition(raw, preset_id=preset_id, revision=1)
    return _write(preset, base_revision=None, client=client)


def _write(preset: contracts.PresetDefinition, *, base_revision: int | None,
           client: str) -> dict[str, Any]:
    authored = preset.authored()
    try:
        head, row = db.save_preset_revision(
            preset_id=preset.id, name=preset.name,
            slug=_slug_for(preset.id) if base_revision is None else "",
            description=preset.description, origin=preset.origin, definition=authored,
            definition_hash=preset.definition_hash(), revision=preset.revision,
            base_revision=base_revision, client=client,
        )
    except db.RevisionConflict as conflict:
        raise _conflict(preset.id, conflict.current, preset.definition_hash()) from None
    logger.info("saved preset %s revision %d", preset.id, preset.revision)
    return view(head, revision=row)


def _conflict(preset_id: str, current: int, attempted_hash: str) -> RequestError:
    head = db.preset_head(preset_id)
    return RequestError(
        409, f"preset '{preset_id}' is at revision {current}; this save started from an "
             "older one and was not applied",
        code="revision_conflict", field="base_revision", retryable=False,
        state={"current_revision": current,
               "current_hash": head.definition_hash if head else "",
               "attempted_hash": attempted_hash},
        suggestions=["Read the current revision and reapply your change.",
                     "Send on_conflict: \"copy\" to keep your version as a new preset."],
    )


def update(preset_id: str, raw: Mapping[str, Any], *, base_revision: int, client: str,
           on_conflict: Literal["error", "copy"] = "error") -> dict[str, Any]:
    """Save an edit as a new revision. Returns the saved view, with `cloned_from`
    when the edit had to become a copy (a curated preset, or a stale base with
    on_conflict "copy")."""
    head = db.preset_head(preset_id)
    if head is None:
        raise _missing(preset_id)
    if head.archived_at is not None:
        raise RequestError(409, f"preset '{preset_id}' is archived; unarchive it to edit",
                           code="preset_archived", field="preset_id")
    if head.origin == "curated":
        # Curated revisions never change. The edit becomes the person's own copy.
        return _as_copy(raw, source=(preset_id, base_revision), client=client)
    current = int(head.revision or 1)
    preset = _custom_definition(raw, preset_id=preset_id, revision=current + 1)
    if base_revision != current:
        if on_conflict == "copy":
            return _as_copy(raw, source=(preset_id, base_revision), client=client)
        raise _conflict(preset_id, current, preset.definition_hash())
    unchanged = contracts.parse_preset({**preset.authored(), "revision": current})
    if unchanged.definition_hash() == head.definition_hash:
        return view(head)  # nothing changed: no empty revision
    return _write(preset, base_revision=current, client=client)


def _as_copy(raw: Mapping[str, Any], *, source: tuple[str, int], client: str) -> dict[str, Any]:
    name = str(raw.get("name") or source[0])
    copy = {**raw, "name": name, "id": _new_custom_id(f"{name} copy")}
    copy.pop("revision", None)
    copy.pop("origin", None)
    copy.pop("curation", None)  # evidence belongs to the curated revision, not the copy
    saved = create(copy, client=client)
    saved["cloned_from"] = {"id": source[0], "revision": source[1]}
    return saved


def clone(preset_id: str, *, revision: int | None = None, name: str | None = None,
          new_id: str | None = None, client: str) -> dict[str, Any]:
    source = get(preset_id, revision)
    doc = dict(source["definition"])
    doc["name"] = name or f"{doc.get('name') or preset_id} copy"
    doc["id"] = new_id or _new_custom_id(doc["name"])
    doc.pop("revision", None)
    doc.pop("origin", None)
    doc.pop("curation", None)
    saved = create(doc, client=client)
    saved["cloned_from"] = {"id": preset_id, "revision": source["revision"]}
    return saved


def archive(preset_id: str, archived: bool = True) -> dict[str, Any]:
    head = db.set_preset_archived(preset_id, archived)
    if head is None:
        raise _missing(preset_id)
    return view(head)


# ---- curated manifests -----------------------------------------------------------------
def load_manifests(directory: Path | None = None) -> dict[str, list[contracts.PresetDefinition]]:
    """Shipped curated revisions, by preset id, oldest first.

    A file that is not a valid curated definition, or a revision whose content
    does not match LOCK.json, is refused (logged and skipped): a shipped
    revision must never change meaning in place.
    """
    root = directory or CURATED_DIR
    lock_path = root / "LOCK.json"
    lock: dict[str, str] = json.loads(lock_path.read_text()) if lock_path.exists() else {}
    found: dict[str, list[contracts.PresetDefinition]] = {}
    for path in sorted(root.glob("*.json")) if root.exists() else []:
        if path.name == "LOCK.json":
            continue
        match = _MANIFEST.fullmatch(path.name)
        if match is None:
            logger.error("curated manifest %s is not named <name>.r<revision>.json", path.name)
            continue
        try:
            preset = contracts.parse_preset(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, contracts.ContractError) as error:
            logger.error("curated manifest %s is invalid: %s", path.name, error)
            continue
        key = f"{preset.id}@{preset.revision}"
        expected_id = f"curated/{match.group('name')}"
        if (preset.origin != "curated" or preset.id != expected_id
                or preset.revision != int(match.group("rev"))):
            logger.error("curated manifest %s does not declare %s revision %s as curated",
                         path.name, expected_id, match.group("rev"))
            continue
        if lock.get(key) != preset.definition_hash():
            logger.error("curated manifest %s does not match LOCK.json; a shipped revision "
                         "is immutable, ship a new revision instead", path.name)
            continue
        found.setdefault(preset.id, []).append(preset)
    for revisions in found.values():
        revisions.sort(key=lambda p: p.revision)
    return found


def ensure_curated(directory: Path | None = None) -> int:
    """Install missing curated revisions; archive entries no longer shipped.

    Idempotent and append-only. Returns how many revisions were written. A
    person's archive of a curated preset is left alone.
    """
    shipped = load_manifests(directory)
    written = 0
    for preset_id, revisions in shipped.items():
        head = db.preset_head(preset_id)
        if head is not None and head.origin != "curated":
            logger.error("preset id %s is taken by a custom preset; curated revision skipped",
                         preset_id)
            continue
        current = int(head.revision or 0) if head is not None else 0
        for preset in revisions:
            if preset.revision <= current:
                continue
            _write(preset, base_revision=current or None, client="shipped")
            current = preset.revision
            written += 1
    for head in db.list_presets(include_archived=True):
        if head.origin == "curated" and head.preset_id not in shipped and head.archived_at is None:
            db.set_preset_archived(str(head.preset_id), True)
            logger.info("curated preset %s is no longer shipped; archived", head.preset_id)
    return written


def lock_entry(preset: contracts.PresetDefinition) -> tuple[str, str]:
    """The LOCK.json key and value for a curated revision (used when adding one)."""
    return f"{preset.id}@{preset.revision}", preset.definition_hash()
