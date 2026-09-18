"""Exporting a Variant Set: successful outputs under their deterministic names,
and a manifest that maps every file back to the variant that made it.

It is the ordinary export (``app/exports.py``) with two additions: each file's
record carries a ``variant`` block — key, axis values, output name, lineage,
validation — and the manifest carries the set itself, with the recipe snapshot
it ran. Nothing here parses a filename: every mapping comes from the item rows.

The generation-metadata privacy setting governs the set exactly as it governs
any export. With it off, prompts, seeds, models and settings are left out of
every record, and the recipe snapshot is reduced to its structure (operations,
axes, naming, validation) — the templates are prompts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import db, exports
from ..models import VariantItem, VariantSet
from . import naming, store
from .service import VariantSetError

# Server-side paths are never written into a manifest: they describe this
# machine, not the variant. Lineage is recorded as asset ids instead.
_LOCAL_KEYS = frozenset({"image_path", "mask_path", "image_paths", "src_path",
                         "last_image_path", "variant"})


@dataclass
class Plan:
    row: VariantSet
    entries: list[exports.Entry]
    extra: dict[str, Any]


def _redacted_recipe(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": snapshot.get("version"),
        "sources": snapshot.get("sources", []),
        "stages": [{
            "name": stage.get("name", ""),
            "operation": stage.get("operation"),
            "axes": stage.get("axes", []),
            "naming": stage.get("naming", {}),
            "validation": stage.get("validation", {}),
            "finishing": [{"processor": step.get("processor")}
                          for step in stage.get("finishing", []) if isinstance(step, dict)],
        } for stage in snapshot.get("stages", []) if isinstance(stage, dict)],
    }


def _variant_record(row: VariantSet, item: VariantItem, parent: VariantItem | None,
                    asset_meta: dict[str, Any], *, include_generation: bool) -> dict[str, Any]:
    stages = row.recipe.get("stages", [])
    record: dict[str, Any] = {
        "set_id": row.id, "item_id": item.id, "stage": item.stage, "key": item.key,
        "values": item.values, "output_name": item.output_name,
        "operation": stages[item.stage].get("operation") if item.stage < len(stages) else None,
        "source_asset_ids": item.source_asset_ids,
        "parent": ({"item_id": parent.id, "key": parent.key,
                    "asset_id": parent.asset_ids[0] if parent.asset_ids else None}
                   if parent else None),
        "job_id": item.job_id, "attempts": item.attempts,
        "validation": {"state": item.validation_state, "results": item.validation},
    }
    if include_generation:
        params = {k: v for k, v in item.params.items()
                  if k not in _LOCAL_KEYS and not k.startswith("_")}
        record.update({
            "model": asset_meta.get("model"),
            "prompt": params.get("prompt"),
            "negative_prompt": params.get("negative_prompt"),
            "seed": params.get("seed"),
            "params": params,
        })
    return record


def plan(set_id: int, *, include_intermediate: bool = False,
         include_generation: bool = True) -> Plan:
    """What an export of this set contains. Raises VariantSetError."""
    row = store.get_set(set_id)
    if row is None:
        raise VariantSetError(404, "variant set not found")
    items = store.items_for_set(set_id)
    by_id = {int(i.id): i for i in items if i.id is not None}
    final_stage = max((i.stage for i in items), default=0)
    chosen = [i for i in items if include_intermediate or i.stage == final_stage]
    wanted = [i.asset_ids[0] for i in chosen if i.state == store.SUCCEEDED and i.asset_ids]
    assets = {int(a.id): a for a in db.get_assets(wanted) if a.id is not None}

    entries: list[exports.Entry] = []
    missing: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    for item in chosen:
        asset = assets.get(item.asset_ids[0]) if item.asset_ids else None
        if item.state != store.SUCCEEDED or asset is None or not Path(asset.path).exists():
            missing.append({
                "key": item.key, "stage": item.stage, "state": item.state,
                "reason": (item.state_reason if item.state != store.SUCCEEDED
                           else "its output is in the trash or no longer on disk"),
            })
            continue
        name = item.output_name if item.stage == final_stage else \
            f"stage-{item.stage + 1}/{item.output_name}"
        names[f"{item.stage}:{item.key}"] = name
        parent = by_id.get(int(item.parent_item_id)) if item.parent_item_id is not None else None
        entries.append(exports.Entry(asset, name, {"variant": _variant_record(
            row, item, parent, asset.meta, include_generation=include_generation)}))

    collisions = naming.find_collisions(names)
    if collisions:
        name, keys = next(iter(collisions.items()))
        raise VariantSetError(409, f"{len(keys)} outputs would share the archive path {name!r}")

    replay = row.replay if include_generation else {"app_version": row.replay.get("app_version")}
    extra = {"variant_set": {
        "id": row.id, "name": row.name, "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expected": row.expected, "counts": row.counts,
        "exported": len(entries), "missing": missing,
        "source_asset_ids": row.source_asset_ids,
        "recipe": row.recipe if include_generation else _redacted_recipe(row.recipe),
        "replay": replay,
    }}
    return Plan(row, entries, extra)


def manifest(set_id: int, *, include_intermediate: bool = False,
             include_generation: bool = True) -> dict[str, Any]:
    """The manifest an export would contain, without writing an archive."""
    p = plan(set_id, include_intermediate=include_intermediate,
             include_generation=include_generation)
    records = [{**exports.asset_record(e.asset, e.archive_name or e.asset.filename,
                                       include_generation=include_generation), **e.extra}
               for e in p.entries]
    return exports.manifest(records, p.extra)
