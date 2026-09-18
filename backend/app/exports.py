"""Portable ZIP exports: the files plus a manifest that keeps the library record.

One writer for every export — a gallery selection and a Variant Set alike — so
the archive layout, video sidecars and the generation-metadata privacy rule are
identical wherever an export comes from. Media bytes alone lose ratings, tags,
captions, job and model identity; the manifest is what makes an export a record
rather than a pile of files.

Spooled to disk by the caller, never built in memory: a selection of videos is
easily several gigabytes.
"""
from __future__ import annotations

import json
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import db
from .models import Asset
from .version import get_version

MANIFEST_NAME = "wizards-brush-manifest.json"
SCHEMA = "wizards-brush-export/v1"


@dataclass
class Entry:
    asset: Asset
    # Path inside the archive. None keeps the asset's own filename, prefixed with
    # its id when two selected assets share one.
    archive_name: str | None = None
    # Merged into this asset's manifest record.
    extra: dict[str, Any] = field(default_factory=dict)


def asset_record(asset: Asset, archive_name: str, *, include_generation: bool) -> dict[str, Any]:
    """The library record for one exported file."""
    content = db.content_of(int(asset.id)) if asset.id is not None else None
    return {
        "asset_id": asset.id,
        "archive_path": archive_name,
        "kind": asset.kind,
        "width": asset.width,
        "height": asset.height,
        "generator": asset.generator,
        "job_id": asset.job_id,
        "created_at": asset.created_at.isoformat(),
        "content": ({"hash": content.hash, "size_bytes": content.size_bytes,
                     "mime_type": content.mime_type} if content else None),
        # The file-metadata privacy choice governs the manifest too: with
        # generation metadata off, no prompt, seed or model leaves in a record.
        "generation": asset.meta if include_generation else None,
        "library": {
            "favorite": bool(asset.favorite),
            "rating": int(asset.rating or 0),
            "grade": asset.grade,
            "tags": asset.tags,
            "caption": asset.caption or "",
            "used_count": int(asset.used_count or 0),
        },
    }


def manifest(records: list[dict[str, Any]], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "app": {"name": "The Wizard's Brush", "version": get_version()},
        "exported_at": datetime.now(UTC).isoformat(),
        **(extra or {}),
        "assets": records,
    }


def write_zip(path: Path, entries: Iterable[Entry], *, include_generation: bool,
              extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the archive at `path` and return the manifest it contains.

    An entry whose file has gone missing is skipped; callers that must account
    for every requested item check existence first and say so in `extra`.
    """
    used_names: set[str] = set()
    records: list[dict[str, Any]] = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in entries:
            asset = entry.asset
            source = Path(asset.path)
            if not source.exists():
                continue
            archive_name = entry.archive_name or asset.filename
            if archive_name in used_names:
                archive_name = f"asset-{asset.id}-{archive_name}"
            used_names.add(archive_name)
            zf.write(source, arcname=archive_name)
            sidecar = source.with_suffix(source.suffix + ".json")
            if include_generation and asset.kind == "video" and sidecar.exists():
                zf.write(sidecar, arcname=archive_name + ".json")
            records.append({**asset_record(asset, archive_name,
                                           include_generation=include_generation),
                            **entry.extra})
        document = manifest(records, extra)
        zf.writestr(MANIFEST_NAME, json.dumps(
            document, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")
    return document
