"""Canvas projects: persistent layered documents over gallery assets.

A document is a fixed-size sRGB canvas with ordered layers (index 0 is the
bottom). A layer points at a gallery image and, optionally, a mask asset the
size of that image (white keeps, black hides). Its transform places it:
the source is scaled by ``scale_x``/``scale_y``, its top-left lands at
``(x, y)`` in canvas pixels, and it is rotated by ``rotation`` degrees
clockwise about the centre of that scaled box, the same convention as CSS
``left/top/width/height`` plus ``rotate()``. Opacity multiplies alpha, and the
blend mode is one of a small fixed set.

Assets are immutable sources. Saving stores references and transforms only,
so editing never changes a gallery file, and deleting a project never deletes
one. A layer whose asset is gone (trashed or purged) stays in the document as
a placeholder: reads report it under ``missing``, flattening refuses unless
the caller accepts the omission, and nothing crashes on reopen.

Every save is a new immutable revision behind a compare-and-set on
``base_revision``, so two editors (the UI's autosave and an MCP client, say)
cannot overwrite each other unnoticed. Flattening renders a revision with the
one deterministic Pillow renderer below into a new gallery asset that records
the project and revision it came from; the document itself is untouched.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from PIL import Image, ImageChops
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from . import db, log
from .models import CanvasProject, CanvasRevision
from .problems import RequestError
from .studio.contracts import digest

logger = log.get("canvas")

SCHEMA_VERSION = 1
MAX_SIDE = 8192
# Flattening holds a few full-size RGBA buffers; this keeps one under ~200 MB.
MAX_PIXELS = 40_000_000
MAX_LAYERS = 64
GENERATOR = "canvas"
_LAYER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class Transform(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float = Field(default=0.0, ge=-4 * MAX_SIDE, le=4 * MAX_SIDE)
    y: float = Field(default=0.0, ge=-4 * MAX_SIDE, le=4 * MAX_SIDE)
    scale_x: float = Field(default=1.0, ge=0.01, le=64.0)
    scale_y: float = Field(default=1.0, ge=0.01, le=64.0)
    rotation: float = Field(default=0.0, ge=-360.0, le=360.0,
                            description="Degrees clockwise about the layer's centre.")


class Lineage(BaseModel):
    """Where a layer came from, when an operation produced it."""

    model_config = ConfigDict(extra="forbid")

    job_id: int | None = Field(default=None, ge=1)
    from_layer: str | None = Field(default=None, max_length=32)


class Layer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=32)
    name: str = Field(default="", max_length=80)
    asset_id: int = Field(ge=1)
    mask_id: int | None = Field(default=None, ge=1,
                                description="A mask the size of the asset: white keeps.")
    transform: Transform = Field(default_factory=Transform)
    opacity: float = Field(default=1.0, ge=0.0, le=1.0)
    visible: bool = True
    blend_mode: Literal["normal", "multiply", "screen"] = "normal"
    lineage: Lineage | None = None

    @model_validator(mode="after")
    def _id(self) -> Layer:
        if not _LAYER_ID.fullmatch(self.id):
            raise ValueError(f"layer id {self.id!r} must be lowercase letters, digits, - or _")
        return self


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    width: int = Field(ge=1, le=MAX_SIDE)
    height: int = Field(ge=1, le=MAX_SIDE)
    color_space: Literal["srgb"] = "srgb"
    background: str = Field(default="transparent",
                            description='"transparent" or a #rrggbb colour.')
    layers: list[Layer] = Field(default_factory=list, max_length=MAX_LAYERS)

    @model_validator(mode="after")
    def _shape(self) -> Document:
        if self.width * self.height > MAX_PIXELS:
            raise ValueError(f"the canvas may have at most {MAX_PIXELS} pixels")
        if self.background != "transparent" and not _HEX.fullmatch(self.background):
            raise ValueError('background must be "transparent" or a #rrggbb colour')
        ids = [layer.id for layer in self.layers]
        if len(set(ids)) != len(ids):
            raise ValueError("layer ids must be unique")
        return self


# ---- validation ----------------------------------------------------------------------------
def _bad(message: str, field: str, code: str = "invalid_request") -> RequestError:
    return RequestError(400, message, code=code, field=field)


def parse(raw: Any) -> Document:
    try:
        return Document.model_validate(raw)
    except ValidationError as error:
        first = error.errors()[0]
        path = ".".join(str(p) for p in first.get("loc", ())) or "document"
        raise _bad(f"{path}: {first.get('msg')}", f"document.{path}".rstrip(".")) from None


def _asset_ids(document: Document) -> set[int]:
    out: set[int] = set()
    for layer in document.layers:
        out.add(layer.asset_id)
        if layer.mask_id is not None:
            out.add(layer.mask_id)
    return out


def _check_assets(document: Document, previous: Document | None) -> None:
    """New references must name live images; a mask must match its image.

    References the previous revision already held may be missing now: a
    placeholder survives saves, so an unrelated edit is never blocked by a
    source someone trashed.
    """
    known = _asset_ids(previous) if previous is not None else set()
    for index, layer in enumerate(document.layers):
        where = f"document.layers.{index}"
        asset = db.get_asset(layer.asset_id)
        if asset is None:
            if layer.asset_id in known:
                continue
            raise RequestError(404, f"{where}: asset {layer.asset_id} does not exist or is in "
                                    "the trash", code="not_found", field=f"{where}.asset_id")
        if asset.kind != "image":
            raise _bad(f"{where}: asset {layer.asset_id} is a {asset.kind}, not an image",
                       f"{where}.asset_id")
        if layer.mask_id is None:
            continue
        mask = db.get_asset(layer.mask_id)
        if mask is None:
            if layer.mask_id in known:
                continue
            raise RequestError(404, f"{where}: mask {layer.mask_id} does not exist or is in "
                                    "the trash", code="not_found", field=f"{where}.mask_id")
        if mask.kind != "image":
            raise _bad(f"{where}: mask {layer.mask_id} is not an image", f"{where}.mask_id")
        if (mask.width, mask.height) != (asset.width, asset.height):
            raise _bad(f"{where}: the mask is {mask.width}x{mask.height} but the image is "
                       f"{asset.width}x{asset.height}; a mask must match its image",
                       f"{where}.mask_id", code="invalid_mask")


def missing_assets(document: Document) -> list[int]:
    """Referenced assets that are no longer live, in document order."""
    out: list[int] = []
    for asset_id in [a for layer in document.layers
                     for a in (layer.asset_id, layer.mask_id) if a is not None]:
        if asset_id not in out and db.get_asset(asset_id) is None:
            out.append(asset_id)
    return out


# ---- storage -------------------------------------------------------------------------------
def _missing(project_id: int) -> RequestError:
    return RequestError(404, f"canvas project {project_id} does not exist", code="not_found",
                        field="project", suggestions=["List projects with GET /api/canvas."])


def _clean_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned or len(cleaned) > 80:
        raise _bad("name must be 1-80 characters", "name")
    return cleaned


def _revision(s: Any, project_id: int, revision: int) -> CanvasRevision | None:
    return s.exec(select(CanvasRevision).where(
        CanvasRevision.project_id == project_id, CanvasRevision.revision == revision)).first()


def _view(head: CanvasProject, row: CanvasRevision) -> dict[str, Any]:
    document = row.document
    try:
        missing = missing_assets(Document.model_validate(document))
    except ValidationError:
        missing = []
    return {
        "id": head.id, "name": head.name, "revision": row.revision,
        "head_revision": head.revision, "digest": row.digest,
        "archived": head.archived_at is not None, "document": document,
        "missing": missing, "client": row.client,
        "created_at": head.created_at, "updated_at": head.updated_at,
    }


def _summary(head: CanvasProject) -> dict[str, Any]:
    return {"id": head.id, "name": head.name, "head_revision": head.revision,
            "width": head.width, "height": head.height,
            "archived": head.archived_at is not None, "updated_at": head.updated_at}


def create(name: str, raw: Any, *, client: str = "") -> dict[str, Any]:
    document = parse(raw)
    _check_assets(document, None)
    body = document.model_dump(mode="json")
    hashed = digest(body, domain="canvas")
    now = datetime.now(UTC)
    with db.session() as s:
        head = CanvasProject(name=_clean_name(name), revision=1, digest=hashed,
                             width=document.width, height=document.height, client=client,
                             created_at=now, updated_at=now)
        s.add(head)
        s.flush()
        row = CanvasRevision(project_id=int(head.id or 0), revision=1,
                             document_json=json.dumps(body), digest=hashed, client=client,
                             created_at=now)
        s.add(row)
        s.commit()
        s.refresh(head)
        s.refresh(row)
        logger.info("created canvas project %s", head.id)
        return _view(head, row)


def _conflict(project_id: int, current: int, base: int, hashed: str,
              head_digest: str) -> RequestError:
    return RequestError(
        409, f"canvas project {project_id} is at revision {current}; this save started from "
             f"revision {base} and was not applied",
        code="revision_conflict", field="base_revision", retryable=False,
        state={"current_revision": current, "current_digest": head_digest,
               "attempted_digest": hashed},
        suggestions=["Read the current revision and reapply your change."])


def save(project_id: int, raw: Any, *, base_revision: int, name: str | None = None,
         client: str = "") -> dict[str, Any]:
    """Write the next revision if the head is still `base_revision`.

    Saving the document the head already holds is not a new revision: an
    autosave with nothing to save returns the head unchanged.
    """
    document = parse(raw)
    body = document.model_dump(mode="json")
    hashed = digest(body, domain="canvas")
    now = datetime.now(UTC)
    with db.session() as s:
        head = s.get(CanvasProject, project_id)
        if head is None:
            raise _missing(project_id)
        if head.revision != base_revision:
            raise _conflict(project_id, head.revision, base_revision, hashed, head.digest)
        current = _revision(s, project_id, head.revision)
        assert current is not None
        if hashed == head.digest and (name is None or _clean_name(name) == head.name):
            return _view(head, current)
        previous = Document.model_validate(current.document)
        _check_assets(document, previous)
        if name is not None:
            head.name = _clean_name(name)
        head.revision = base_revision + 1
        head.digest = hashed
        head.width, head.height = document.width, document.height
        head.updated_at = now
        s.add(head)
        try:
            s.flush()
            row = CanvasRevision(project_id=project_id, revision=head.revision,
                                 document_json=json.dumps(body), digest=hashed,
                                 based_on=base_revision, client=client, created_at=now)
            s.add(row)
            s.commit()
        except IntegrityError:
            s.rollback()
            latest = s.get(CanvasProject, project_id)
            raise _conflict(project_id, latest.revision if latest else 0, base_revision,
                            hashed, latest.digest if latest else "") from None
        s.refresh(head)
        s.refresh(row)
        return _view(head, row)


def get(project_id: int, revision: int | None = None) -> dict[str, Any]:
    """One revision (the head by default), with any missing sources listed."""
    with db.session() as s:
        head = s.get(CanvasProject, project_id)
        if head is None:
            raise _missing(project_id)
        row = _revision(s, project_id, revision or head.revision)
        if row is None:
            raise RequestError(404, f"canvas project {project_id} has no revision {revision}",
                               code="not_found", field="revision",
                               valid_options=list(range(1, head.revision + 1)))
        return _view(head, row)


def listing(*, include_archived: bool = False) -> list[dict[str, Any]]:
    with db.session() as s:
        query = select(CanvasProject).order_by(col(CanvasProject.updated_at).desc(),
                                               func.lower(col(CanvasProject.name)))
        if not include_archived:
            query = query.where(col(CanvasProject.archived_at).is_(None))
        return [_summary(h) for h in s.exec(query)]


def archive(project_id: int, archived: bool = True) -> dict[str, Any]:
    with db.session() as s:
        head = s.get(CanvasProject, project_id)
        if head is None:
            raise _missing(project_id)
        head.archived_at = datetime.now(UTC) if archived else None
        head.updated_at = datetime.now(UTC)
        s.add(head)
        s.commit()
        s.refresh(head)
        row = _revision(s, project_id, head.revision)
        assert row is not None
        return _view(head, row)


def delete(project_id: int) -> dict[str, Any]:
    """Remove a project and its revisions. Gallery assets are never touched:
    layers reference them, they do not own them."""
    with db.session() as s:
        head = s.get(CanvasProject, project_id)
        if head is None:
            raise _missing(project_id)
        rows = list(s.exec(select(CanvasRevision).where(
            CanvasRevision.project_id == project_id)))
        for row in rows:
            s.delete(row)
        s.delete(head)
        s.commit()
    logger.info("deleted canvas project %s (%d revisions)", project_id, len(rows))
    return {"ok": True, "deleted_revisions": len(rows)}


# ---- rendering -----------------------------------------------------------------------------
def _background(document: Document) -> tuple[int, int, int, int]:
    if document.background == "transparent":
        return (0, 0, 0, 0)
    value = document.background.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), 255)


def _placed(layer: Layer, source: Image.Image,
            mask: Image.Image | None) -> tuple[Image.Image, tuple[int, int]]:
    """The layer as an RGBA image and the canvas offset of its top-left."""
    image = source.convert("RGBA")
    if mask is not None:
        alpha = image.getchannel("A")
        keep = mask.convert("L")
        if keep.size != image.size:
            keep = keep.resize(image.size, Image.Resampling.NEAREST)
        image.putalpha(ImageChops.multiply(alpha, keep))
    t = layer.transform
    width = max(1, round(image.width * t.scale_x))
    height = max(1, round(image.height * t.scale_y))
    if (width, height) != image.size:
        image = image.resize((width, height), Image.Resampling.BICUBIC)
    if t.rotation % 360:
        # Pillow rotates counter-clockwise; the document is clockwise, like CSS.
        image = image.rotate(-t.rotation, resample=Image.Resampling.BICUBIC, expand=True)
    if layer.opacity < 1.0:
        alpha = image.getchannel("A").point(lambda a: round(a * layer.opacity))
        image.putalpha(alpha)
    left = round(t.x + width / 2 - image.width / 2)
    top = round(t.y + height / 2 - image.height / 2)
    return image, (left, top)


def render(document: Document, load: Any = None) -> tuple[Image.Image, list[int]]:
    """Composite every visible layer, bottom first. Deterministic.

    `load(asset_id)` returns a PIL image or None; by default it reads the
    gallery. Returns the image and the asset ids that could not be loaded
    (their layers are left out).
    """
    load = load or _load_asset
    size = (document.width, document.height)
    canvas = Image.new("RGBA", size, _background(document))
    missing: list[int] = []
    for layer in document.layers:
        if not layer.visible or layer.opacity <= 0:
            continue
        source = load(layer.asset_id)
        mask = load(layer.mask_id) if layer.mask_id is not None else None
        if source is None or (layer.mask_id is not None and mask is None):
            missing.extend(a for a, img in ((layer.asset_id, source), (layer.mask_id, mask))
                           if a is not None and img is None and a not in missing)
            continue
        image, offset = _placed(layer, source, mask)
        full = Image.new("RGBA", size, (0, 0, 0, 0))
        full.paste(image, offset)
        if layer.blend_mode == "normal":
            canvas = Image.alpha_composite(canvas, full)
            continue
        base_rgb = canvas.convert("RGB")
        top_rgb = full.convert("RGB")
        blended = (ImageChops.multiply(base_rgb, top_rgb) if layer.blend_mode == "multiply"
                   else ImageChops.screen(base_rgb, top_rgb))
        # Over a transparent base there is nothing to blend with: the layer's
        # own colour shows, as it would in normal mode.
        blended = Image.composite(blended, top_rgb, canvas.getchannel("A"))
        blended.putalpha(full.getchannel("A"))
        canvas = Image.alpha_composite(canvas, blended)
    return canvas, missing


def _load_asset(asset_id: int) -> Image.Image | None:
    asset = db.get_asset(asset_id)
    if asset is None or not asset.path:
        return None
    try:
        with Image.open(asset.path) as handle:
            handle.load()
            return handle.copy()
    except OSError:
        return None


def flatten(project_id: int, revision: int | None = None, *,
            allow_missing: bool = False) -> dict[str, Any]:
    """Render a revision into a new gallery image. The document is untouched.

    A missing source is refused unless `allow_missing`: a flattened file that
    silently lacks a layer would look complete and is not.
    """
    from .models import AssetKind
    from .utils.io import save_image

    found = get(project_id, revision)
    document = Document.model_validate(found["document"])
    if found["missing"] and not allow_missing:
        raise RequestError(409, f"assets {found['missing']} are missing; restore them from the "
                                "trash, or flatten with allow_missing to leave their layers out",
                           code="missing_sources", field="allow_missing",
                           state={"missing": found["missing"]})
    image, missing = render(document)
    lineage = {"project_id": project_id, "revision": found["revision"],
               "digest": found["digest"], "layers": len(document.layers)}
    meta: dict[str, Any] = {"canvas": {**lineage, **({"missing": missing} if missing else {})}}
    saved = save_image(image, "canvas", meta=meta, kind=GENERATOR)
    asset = db.add_asset(AssetKind.image.value, saved.path, thumb=saved.thumb,
                         width=saved.width, height=saved.height, generator=GENERATOR,
                         meta=meta, content_hash=saved.content_hash,
                         size_bytes=saved.size_bytes, mime_type="image/png")
    assert asset.id is not None
    logger.info("flattened canvas %s r%s into asset %s", project_id, found["revision"], asset.id)
    return {"asset_id": int(asset.id), "project_id": project_id, "revision": found["revision"],
            "width": saved.width, "height": saved.height, "missing": missing}


# ---- editing through the ordinary job API ---------------------------------------------------
def edit_inputs(project_id: int, *, layer_id: str | None, mask_id: int | None,
                revision: int | None = None) -> dict[str, Any]:
    """What to send as `inputs` to POST /api/jobs to edit part of a document.

    With a layer, its source image (untransformed) is the input, so the result
    can come back in that layer's place. Without one, the revision is flattened
    explicitly into a new asset first. The mask must match the input's size.
    No job is created here; the edit runs as an ordinary job of an existing kind.
    """
    found = get(project_id, revision)
    document = Document.model_validate(found["document"])
    flattened: dict[str, Any] | None = None
    if layer_id is not None:
        layer = next((entry for entry in document.layers if entry.id == layer_id), None)
        if layer is None:
            raise _bad(f"this document has no layer {layer_id!r}", "layer_id",
                       code="invalid_option")
        source_id = layer.asset_id
    else:
        flattened = flatten(project_id, found["revision"])
        source_id = flattened["asset_id"]
    source = db.get_asset(source_id)
    if source is None:
        raise RequestError(409, f"asset {source_id} is missing", code="missing_sources",
                           field="layer_id")
    if mask_id is not None:
        mask = db.get_asset(mask_id)
        if mask is None or mask.kind != "image":
            raise RequestError(404, f"mask {mask_id} does not exist or is in the trash",
                               code="not_found", field="mask_id")
        if (mask.width, mask.height) != (source.width, source.height):
            raise _bad(f"the mask is {mask.width}x{mask.height} but the input is "
                       f"{source.width}x{source.height}; a mask must match its image",
                       "mask_id", code="invalid_mask")
    inputs: dict[str, Any] = {"images": [source_id]}
    if mask_id is not None:
        inputs["mask"] = mask_id
    return {"project_id": project_id, "revision": found["revision"], "layer_id": layer_id,
            "inputs": inputs, "flattened": flattened,
            "then": "Run the edit with POST /api/jobs, then add its output with "
                    f"POST /api/canvas/{project_id}/layers (job_id, from_layer)."}


def _next_layer_id(document: Document) -> str:
    taken = {layer.id for layer in document.layers}
    n = len(document.layers) + 1
    while f"layer-{n}" in taken:
        n += 1
    return f"layer-{n}"


def add_layer(project_id: int, *, base_revision: int, asset_id: int | None = None,
              job_id: int | None = None, name: str = "", from_layer: str | None = None,
              mask_id: int | None = None, client: str = "") -> dict[str, Any]:
    """Put a gallery asset (or a finished job's first output) on top as a new
    layer, recording its lineage. With `from_layer`, it takes that layer's
    transform, so an edit of that layer lands in its place."""
    from .models import JobStatus

    if (asset_id is None) == (job_id is None):
        raise _bad("give exactly one of asset_id or job_id", "asset_id")
    if job_id is not None:
        job = db.get_job(job_id)
        if job is None:
            raise RequestError(404, f"job {job_id} does not exist", code="not_found",
                               field="job_id")
        outputs = [int(a) for a in (job.result.get("asset_ids") or [])]
        if job.status != JobStatus.done or not outputs:
            raise RequestError(409, f"job {job_id} has no finished output yet",
                               code="not_ready", field="job_id", retryable=True)
        asset_id = outputs[0]
    found = get(project_id)
    document = Document.model_validate(found["document"])
    transform = Transform()
    if from_layer is not None:
        source = next((entry for entry in document.layers if entry.id == from_layer), None)
        if source is None:
            raise _bad(f"this document has no layer {from_layer!r}", "from_layer",
                       code="invalid_option")
        transform = source.transform.model_copy()
    assert asset_id is not None
    layer = Layer(id=_next_layer_id(document),
                  name=name or (f"Edit of {from_layer}" if from_layer else f"Asset {asset_id}"),
                  asset_id=asset_id, mask_id=mask_id, transform=transform,
                  lineage=(Lineage(job_id=job_id, from_layer=from_layer)
                           if job_id is not None or from_layer is not None else None))
    updated = document.model_copy(update={"layers": [*document.layers, layer]})
    return save(project_id, updated.model_dump(mode="json"), base_revision=base_revision,
                client=client)
