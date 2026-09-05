"""SQLModel tables + Pydantic API schemas for jobs and assets.

One `Job` row per generation/tool request; one `Asset` row per produced file
(image or video). Params and results are stored as JSON strings so any
generator can persist its own shape without a schema migration.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(UTC)


# Persisted param keys that have been renamed.
#
# `params_json` is not a transient request body — it is replayed verbatim on
# rerun, read by the grid viewer, and shown in the asset modal. Rename a key
# without a mapping and every historical job silently loses that value, with no
# error to notice. We already keep this discipline for renamed *generator* names
# (GEN_TO_SPEC in the frontend); param keys are equally persisted and had no
# equivalent.
#
# old key -> current key. Append only; never remove an entry, because rows using
# the old spelling live on disk forever.
PARAM_REMAPS: dict[str, str] = {}

# Semantic version of the persisted parameter vocabulary. Renames are handled
# independently by PARAM_REMAPS; this version is for the harder changes where a
# value's meaning changes while its key stays the same. Jobs and asset metadata
# cross different replay boundaries, so both properties below use this one lazy
# migration chain.
PARAM_SCHEMA_VERSION = 1


def _params_v0_to_v1(raw: dict[str, Any]) -> dict[str, Any]:
    """Establish the version boundary without changing legacy behavior."""
    return raw


# Source version -> migration to the next version. Append only. A migration is
# deliberately pure: reading an old record never rewrites the database, so an
# incorrect future interpretation remains reversible.
PARAM_MIGRATIONS = {0: _params_v0_to_v1}


def remap_params(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate legacy param keys to their current spelling.

    A current key already present always wins: if a row somehow carries both
    spellings, the newer one is the one that was written deliberately.
    """
    if not PARAM_REMAPS:
        return raw
    out: dict[str, Any] = {}
    for k, v in raw.items():
        key = PARAM_REMAPS.get(k, k)
        if key in out and k in PARAM_REMAPS:
            continue  # do not let a legacy alias overwrite the current value
        out[key] = v
    return out


def migrate_params(raw: dict[str, Any]) -> dict[str, Any]:
    """Return current replay semantics without mutating the stored record.

    A record from a newer app is left at its declared version. Downgrading it
    and stamping our older number would falsely claim we understood semantics
    that did not exist when this build shipped.
    """
    out = remap_params(dict(raw))
    try:
        version = max(0, int(out.get("_v", 0)))
    except (TypeError, ValueError):
        version = 0
    if version > PARAM_SCHEMA_VERSION:
        return out
    while version < PARAM_SCHEMA_VERSION:
        migrate = PARAM_MIGRATIONS.get(version)
        if migrate is None:  # a broken chain must not silently skip semantics
            raise ValueError(f"missing parameter migration from version {version}")
        out = migrate(out)
        version += 1
    out["_v"] = PARAM_SCHEMA_VERSION
    return out


def stamp_params(raw: dict[str, Any]) -> dict[str, Any]:
    """Stamp newly persisted params after applying current key spellings."""
    out = remap_params(dict(raw))
    out["_v"] = PARAM_SCHEMA_VERSION
    return out


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"
    canceled = "canceled"


class JobKind(str, Enum):
    image_local = "image_local"
    image_colab = "image_colab"
    image_edit = "image_edit"
    img2img = "img2img"
    inpaint = "inpaint"
    outpaint = "outpaint"
    control_local = "control_local"
    t2v = "t2v"
    i2v = "i2v"
    long_video = "long_video"
    extend_video = "extend_video"
    upscale = "upscale"
    face_restore = "face_restore"
    interpolate = "interpolate"
    detail = "detail"


class AssetKind(str, Enum):
    image = "image"
    video = "video"


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------
class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    kind: str
    status: str = Field(default=JobStatus.queued.value, index=True)
    progress: float = 0.0                       # 0..1
    message: str = ""                           # human-readable status/step
    params_json: str = "{}"                     # request params
    result_json: str = "{}"                     # {"asset_ids": [...], ...}
    error: str = ""
    # Queue ordering (higher runs first) + grid/fan-out grouping. Added via
    # additive migration for existing DBs — see db._migrate.
    priority: int = Field(default=0, index=True)
    group_id: str | None = Field(default=None, index=True)
    # Caller-supplied submission identity. Nullable so reruns/internal jobs need
    # no synthetic value; unique so a retried POST resolves to the first row
    # instead of spending the generation twice.
    request_id: str | None = Field(default=None, index=True, unique=True)
    # Which local model this job needs, resolved once at creation. The lane
    # compares it against the resident model to avoid a 60-120s pipeline swap.
    # NULL = no local model involved (remote jobs, and tools like upscale).
    model_key: str | None = Field(default=None, index=True)
    # Current remote invocation owned by this job. Not exposed by JobRead and
    # never copied into rerun params; it exists so restart reconciliation can
    # reclaim paid A100 work instead of abandoning it in the remote queue.
    remote_client_id: str | None = Field(default=None, index=True)
    remote_token: str | None = Field(default=None, index=True)
    # Human-readable guidance for a failure, kept apart from `error` so the UI
    # can lead with what to do and put the traceback behind a disclosure.
    tip: str = ""
    created_at: datetime = Field(default_factory=_now, index=True)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def params(self) -> dict[str, Any]:
        return migrate_params(json.loads(self.params_json or "{}"))

    @property
    def result(self) -> dict[str, Any]:
        return json.loads(self.result_json or "{}")


class AssetContent(SQLModel, table=True):
    """The bytes. One row per distinct file content, keyed by hash.

    Split out from Asset so that saving identical bytes twice — a rerun with the
    same seed, the same image added to two collections — stores one copy and two
    references. `hash` carries a UNIQUE index, which is what makes the table
    content-addressed rather than merely hash-annotated.

    `int | None` rather than `Optional[int]`: with `from __future__ import
    annotations` SQLModel leaves the latter an unresolved ForwardRef and dies
    building the column. Same trap as Asset.deleted_at.
    """
    id: int | None = Field(default=None, primary_key=True)
    hash: str = Field(index=True, unique=True)   # blake2b-160 of the file bytes
    size_bytes: int = 0
    mime_type: str = ""
    created_at: datetime = Field(default_factory=_now)


class Asset(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    kind: str
    path: str                                   # absolute path on disk
    filename: str
    thumb: str | None = None                 # relative thumb filename
    width: int | None = None
    height: int | None = None
    job_id: int | None = Field(default=None, index=True)
    generator: str = ""                         # which generator made it
    meta_json: str = "{}"                        # prompt, seed, steps, etc.
    created_at: datetime = Field(default_factory=_now, index=True)
    # Organization (added via additive migration for existing DBs — see db._migrate).
    favorite: bool = Field(default=False, index=True)
    rating: int = Field(default=0, index=True)   # 0..5 stars
    # A structured human review. The legacy scalar rating remains the quick
    # gallery signal; this JSON preserves *why* a reviewer gave that verdict.
    # Kept alongside metadata rather than as five more nullable columns because
    # the rubric can grow without a migration for every wording refinement.
    grade_json: str = "{}"
    tags_json: str = "[]"                         # JSON list of free-text tags
    # Enrichment (async worker; see enrichment.py).
    caption: str = ""
    # Soft delete. Set when the user deletes; the row and its files survive until
    # the trash is emptied, so a misclick is recoverable. NULL = live.
    #
    # `datetime | None` rather than `Optional[datetime]`: with
    # `from __future__ import annotations` SQLModel leaves the latter as an
    # unresolved ForwardRef and dies building the column.
    deleted_at: datetime | None = Field(default=None, index=True)
    # Content identity. NULL until the enrichment worker hashes an older row;
    # everything written since hashing landed sets it at save time.
    content_id: int | None = Field(default=None, foreign_key="assetcontent.id", index=True)
    # Filesystem reconciliation. sweep_orphan_files() handles files with no row;
    # these handle the mirror case — a row whose file went away — so it becomes
    # a state the UI can show rather than an exception when something opens it.
    mtime_ns: int | None = None
    is_missing: bool = Field(default=False, index=True)
    needs_verify: bool = False
    # Enrichment ladder. 0 saved, 1 captioned, 2 tagged, 3 embedded. Only ever
    # increases, so a crash mid-caption resumes instead of redoing everything.
    enrich_level: int = Field(default=0, index=True)
    # Behaviour, as distinct from the intent `favorite` records: incremented when
    # an asset is exported, downloaded, remixed or used as an input image.
    used_count: int = Field(default=0, index=True)
    last_used_at: datetime | None = None
    # CLIP vector as raw float32 bytes (rung 3). Brute-force cosine over a
    # personal gallery is fast enough that a vector index would be dead weight.
    embedding: bytes | None = None

    @property
    def meta(self) -> dict[str, Any]:
        return migrate_params(json.loads(self.meta_json or "{}"))

    @property
    def tags(self) -> list[str]:
        try:
            return list(json.loads(self.tags_json or "[]"))
        except Exception:  # noqa: BLE001 — degrade quietly; the caller must not fail here
            return []

    @property
    def grade(self) -> dict[str, Any]:
        try:
            value = json.loads(self.grade_json or "{}")
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001 — an old/corrupt row is simply unreviewed
            return {}


class PromptHistory(SQLModel, table=True):
    """Every generation's prompt, so the UI can offer recall / favorites.
    Kept in its own table so 'Clear all' (which wipes Asset + Job) never deletes it."""
    id: int | None = Field(default=None, primary_key=True)
    kind: str = ""
    prompt: str = ""
    negative: str = ""
    params_json: str = "{}"
    favorite: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)

    @property
    def params(self) -> dict[str, Any]:
        return migrate_params(json.loads(self.params_json or "{}"))


class Collection(SQLModel, table=True):
    """A named grouping of assets. Survives `clear_all()` like presets do —
    the membership rows go with the assets, but the collection itself is
    organisation the user built and should not evaporate with a gallery wipe.

    `int | None` rather than `Optional[int]`: with `from __future__ import
    annotations` SQLModel leaves the latter an unresolved ForwardRef and dies
    building the column. Same trap as Asset.deleted_at."""
    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    created_at: datetime = Field(default_factory=_now, index=True)


class CollectionItem(SQLModel, table=True):
    """Asset <-> collection membership. An asset can sit in several."""
    id: int | None = Field(default=None, primary_key=True)
    collection_id: int = Field(index=True)
    asset_id: int = Field(index=True)
    added_at: datetime = Field(default_factory=_now)


class UserPreset(SQLModel, table=True):
    """User-created presets: a saved prompt, negative, style, param set, or
    gallery search. Survives 'Clear all'.
    type ∈ {prompt, negative, style, params, search}."""
    id: int | None = Field(default=None, primary_key=True)
    type: str = "prompt"
    name: str = ""
    payload_json: str = "{}"                      # {"text": "..."} or full params
    scope: str = "all"                            # generator id, or "all"
    # Any gallery image, starred as this preset's thumbnail. Nothing is
    # generated for it — a style you can recognise beats a name in a list.
    preview_asset_id: int | None = None
    created_at: datetime = Field(default_factory=_now, index=True)

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json or "{}")


# --------------------------------------------------------------------------
# API read schemas (what the frontend receives)
# --------------------------------------------------------------------------
class JobRead(BaseModel):
    id: int
    kind: str
    status: str
    progress: float
    message: str
    params: dict[str, Any]
    result: dict[str, Any]
    error: str
    tip: str = ""
    priority: int = 0
    group_id: str | None = None
    model_key: str | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, j: Job) -> JobRead:
        return cls(
            id=j.id, kind=j.kind, status=j.status, progress=j.progress, message=j.message,
            params=j.params, result=j.result, error=j.error, tip=j.tip or "",
            priority=j.priority or 0, group_id=j.group_id, model_key=j.model_key,
            created_at=j.created_at, started_at=j.started_at, finished_at=j.finished_at,
        )


class AssetRead(BaseModel):
    id: int
    kind: str
    filename: str
    url: str
    thumb_url: str | None
    width: int | None
    height: int | None
    job_id: int | None
    generator: str
    meta: dict[str, Any]
    created_at: datetime
    favorite: bool = False
    rating: int = 0
    grade: dict[str, Any] = {}
    tags: list[str] = []
    caption: str = ""
    deleted_at: datetime | None = None
    # Behaviour, distinct from the `favorite` intent above.
    used_count: int = 0
    last_used_at: datetime | None = None
    # How many live assets share these exact bytes, including this one. 1 means
    # unique. Supplied per page by db.duplicate_counts, not computed per tile.
    copies: int = 1
    is_missing: bool = False

    @classmethod
    def of(cls, a: Asset, copies: int = 1) -> AssetRead:
        sub = "videos" if a.kind == AssetKind.video.value else "images"
        return cls(
            id=a.id, kind=a.kind, filename=a.filename,
            url=f"/files/{sub}/{a.filename}",
            thumb_url=f"/files/thumbs/{a.thumb}" if a.thumb else None,
            width=a.width, height=a.height, job_id=a.job_id,
            generator=a.generator, meta=a.meta, created_at=a.created_at,
            favorite=bool(a.favorite), rating=a.rating or 0, grade=a.grade, tags=a.tags,
            caption=a.caption or "", deleted_at=a.deleted_at,
            used_count=int(a.used_count or 0), last_used_at=a.last_used_at,
            copies=copies, is_missing=bool(a.is_missing),
        )


class PromptHistoryRead(BaseModel):
    id: int
    kind: str
    prompt: str
    negative: str
    params: dict[str, Any]
    favorite: bool
    created_at: datetime

    @classmethod
    def of(cls, h: PromptHistory) -> PromptHistoryRead:
        return cls(id=h.id, kind=h.kind, prompt=h.prompt, negative=h.negative,
                   params=h.params, favorite=bool(h.favorite), created_at=h.created_at)


class CollectionRead(BaseModel):
    id: int
    name: str
    count: int = 0
    created_at: datetime

    @classmethod
    def of(cls, c: Collection, count: int = 0) -> CollectionRead:
        return cls(id=c.id, name=c.name, count=count, created_at=c.created_at)


class UserPresetRead(BaseModel):
    id: int
    type: str
    name: str
    payload: dict[str, Any]
    scope: str
    created_at: datetime
    preview_asset_id: int | None = None
    preview_url: str | None = None

    @classmethod
    def of(cls, p: UserPreset, preview: Asset | None = None) -> UserPresetRead:
        return cls(id=p.id, type=p.type, name=p.name, payload=p.payload,
                   scope=p.scope, created_at=p.created_at,
                   preview_asset_id=p.preview_asset_id,
                   preview_url=(f"/files/thumbs/{preview.thumb}"
                                if preview is not None and preview.thumb else None))
