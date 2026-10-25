"""Sprite sheets and atlases from a Variant Set's final stage.

Frames are the final stage's variants in the set's own order (or by key), and
each keeps its index whether or not the frames before it exist: a missing
frame is reported at its index, never closed over by shifting the rest. By
default every frame must exist; ``partial`` exports what does and lists the
omissions in the manifest.

The pages and manifest are built in staging, checked (every rectangle inside
its page, none overlapping, every frame cut back out of the written PNG and
compared with what was meant to be there), zipped, and only then published as
an artifact. Pixels come from the gallery files; nothing here touches the GPU.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image

from .. import artifacts, db, spritesheet
from ..spritesheet import ExportProfile, Frame, SpriteError
from . import store
from .service import VariantSetError


def _frames(set_id: int, profile: ExportProfile, *, include_labels: bool
            ) -> tuple[Any, list[Frame], list[dict[str, Any]]]:
    row = store.get_set(set_id)
    if row is None:
        raise VariantSetError(404, "variant set not found")
    items = store.items_for_set(set_id)
    if not items:
        raise VariantSetError(409, "this set has no variants")
    final = max(i.stage for i in items)
    chosen = [i for i in items if i.stage == final]
    if profile.order == "key":
        chosen.sort(key=lambda i: i.key)
    wanted = [i.asset_ids[0] for i in chosen if i.state == store.SUCCEEDED and i.asset_ids]
    assets = {int(a.id): a for a in db.get_assets(wanted) if a.id is not None}
    stage_name = (row.recipe.get("stages") or [{}])[final].get("name", "")
    frames: list[Frame] = []
    missing: list[dict[str, Any]] = []
    for index, item in enumerate(chosen):
        asset = assets.get(item.asset_ids[0]) if item.asset_ids else None
        if item.state != store.SUCCEEDED or asset is None or not Path(asset.path).is_file():
            missing.append({"index": index, "key": item.key, "state": item.state,
                            "reason": (item.state_reason if item.state != store.SUCCEEDED
                                       else "its output is in the trash or no longer on disk")})
            continue
        with Image.open(asset.path) as source:
            image = source.convert("RGBA")
        labels = ({"values": item.values, "stage": final, "stage_name": stage_name,
                   "output_name": item.output_name} if include_labels else {})
        frames.append(Frame(index, item.key, image, asset_id=int(asset.id or 0), labels=labels))
    if missing and not profile.partial:
        first = missing[0]
        raise VariantSetError(409, f"{len(missing)} frame(s) are missing (e.g. #{first['index']} "
                                   f"{first['key']}: {first['reason']}); retry them, or export "
                                   "with partial to list them as omissions")
    return row, frames, missing


def preview(set_id: int, profile: ExportProfile) -> dict[str, Any]:
    """Pages, dimensions, order and omissions, without drawing anything."""
    _row, frames, missing = _frames(set_id, profile, include_labels=False)
    try:
        return {"profile": profile.model_dump(mode="json"),
                **spritesheet.preview(spritesheet.layout(frames, profile), missing)}
    except SpriteError as error:
        raise VariantSetError(400, str(error)) from None


def export(set_id: int, profile: ExportProfile, *, include_generation: bool,
           client: str = "") -> dict[str, Any]:
    """Build, validate and publish one sheet or atlas. Returns the artifact view."""
    row, frames, missing = _frames(set_id, profile, include_labels=include_generation)
    try:
        layout = spritesheet.layout(frames, profile)
        pages = spritesheet.render(layout, profile)
        spritesheet.validate(layout, pages, profile)
    except SpriteError as error:
        raise VariantSetError(400, str(error)) from None
    with artifacts.staging() as work:
        export_id = work.name
        document = spritesheet.manifest(layout, profile, export_id=export_id, missing=missing,
                                        include_labels=include_generation)
        if include_generation:
            document["variant_set"] = {"id": row.id, "name": row.name}
        names = [page["file"] for page in document["pages"]]
        for name, page in zip(names, pages, strict=True):
            page.save(work / name, format="PNG")
        # Check what was written, not only what was drawn.
        reread = []
        for name in names:
            with Image.open(work / name) as written:
                reread.append(written.convert("RGBA"))
        try:
            spritesheet.validate(layout, reread, profile)
        except SpriteError as error:
            raise VariantSetError(500, f"the written pages did not validate: {error}") from None
        (work / "manifest.json").write_text(json.dumps(document, indent=2) + "\n")
        bundle = work / "bundle.zip"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in [*names, "manifest.json"]:
                archive.write(work / name, name)
        safe = spritesheet.page_name(profile, 0, 1)[:-4]
        published = artifacts.publish(
            bundle, kind=f"sprite_{profile.mode}", filename=f"{safe}-{row.id}.zip",
            mime_type="application/zip", source=f"variant_set:{row.id}", client=client,
            meta={"pages": document["pages"], "frames": len(document["frames"]),
                  "missing": missing, "format": spritesheet.FORMAT,
                  "format_version": spritesheet.FORMAT_VERSION, "export_id": export_id})
    for frame in frames:
        if frame.asset_id:
            db.mark_used(frame.asset_id)
    return {**artifacts.view(published), "manifest": document}
