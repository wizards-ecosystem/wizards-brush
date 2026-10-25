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
from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from .redaction import redact, redact_text


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
    inpaint_remote = "inpaint_remote"
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
    matte = "matte"
    region_mask = "region_mask"


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
    # Who submitted this job: "ui", "mcp", "openai", or the API token's name
    # ("api" when the token is unnamed). Empty on rows written before the column existed.
    client: str = ""
    # Human-readable guidance for a failure, kept apart from `error` so the UI
    # can lead with what to do and put the traceback behind a disclosure.
    tip: str = ""
    # ---- preset jobs (0015); NULL on every job made the kind-based way --------------
    # The immutable plan this job executes, the digest of the request that
    # produced it (idempotency compares this, not the params), and the target
    # that runs it ("local", or the remote worker's identity).
    plan_id: int | None = Field(default=None, index=True)
    request_digest: str | None = None
    backend_id: str | None = None
    # What actually executed, recorded when the job starts: runtime and library
    # versions, the worker build. Kept apart from the plan's expectation so a
    # mismatch is visible rather than folded into a replay claim.
    execution_json: str = "{}"
    created_at: datetime = Field(default_factory=_now, index=True)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def params(self) -> dict[str, Any]:
        return migrate_params(json.loads(self.params_json or "{}"))

    @property
    def result(self) -> dict[str, Any]:
        return json.loads(self.result_json or "{}")

    @property
    def execution(self) -> dict[str, Any]:
        return _json_or(self.execution_json, {})


class ExecutionPlan(SQLModel, table=True):
    """The immutable effective request behind a preset job (0015).

    Written once, before its job, and never updated. `plan_json` is what the
    preview showed (model and adapter identities, composed prompts, effective
    settings, inputs with hashes, seed, finishing); `request_json` is the
    resolve input that produced it, kept for "run with the latest preset".
    """
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    request_id: str | None = None
    request_digest: str = ""
    creative_hash: str = Field(default="", index=True)
    execution_hash: str = ""
    material_hash: str = ""
    schema_version: int = 1
    resolver_version: str = ""
    preset_id: str | None = Field(default=None, index=True)
    preset_revision: int | None = None
    kind: str = ""
    target: str = ""
    plan_json: str = "{}"
    request_json: str = "{}"
    replay_of: int | None = None
    created_at: datetime = Field(default_factory=_now)

    @property
    def plan(self) -> dict[str, Any]:
        return _json_or(self.plan_json, {})

    @property
    def request(self) -> dict[str, Any]:
        return _json_or(self.request_json, {})


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


def _json_or(raw: str | None, fallback: Any) -> Any:
    """Decode a JSON column, degrading to `fallback` for an old or corrupt row."""
    try:
        value = json.loads(raw or "null")
    except (TypeError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


class Recipe(SQLModel, table=True):
    """A named generation or edit setup with a narrow input schema.

    `params_json` is the frozen settings of one job kind. `fields_json` is the
    subset a later run may override, each with the type, default, constraints
    and description captured when the recipe was saved. `inputs_json` is the
    frozen gallery inputs (`images`, `mask`, `last_frame`). `defaulted_json`
    names settings that were left at the kind's default, so a run can recompute
    them when their source changes. Running a recipe merges a caller's values
    onto this snapshot and submits an ordinary job.

    This is not a Variant Set recipe. Those live on `variantrecipe` and fan out
    across axes. Ids are never reused: a saved link or an agent transcript may
    name this id after the row is gone, and it must not come back as a
    different setup.
    """
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    slug: str = Field(index=True, unique=True)
    description: str = ""
    kind: str = ""
    params_json: str = "{}"
    fields_json: str = "[]"
    inputs_json: str = "{}"
    # Setting names filled from the kind's defaults (or sent equal to them).
    # A run recomputes these when the field they follow changes.
    defaulted_json: str = "[]"
    # Ordered style fragments ({"id", "text"}) frozen at save time and composed
    # onto the run's subject prompt, so a run matches Generate (0012).
    styles_json: str = "[]"
    # Who saved it: "ui", "mcp", or the API token's name. Provenance, not a
    # permission. Empty on a row written before the column existed.
    client: str = ""
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)
    # ---- studio presets (0013) ------------------------------------------------
    # 1: a saved generator (the fields above). 2: a studio preset, whose
    # content is `definition_json` and whose history is `reciperevision`.
    schema_version: int = Field(default=1, index=True)
    # The preset's stable id ("curated/fast-general", "custom/my-look"). NULL
    # for saved generators. Unique among presets (a partial index).
    preset_id: str | None = None
    # The head revision: what the catalogue shows and a new run resolves.
    revision: int = 1
    origin: str = ""                    # "" (v1) | "curated" | "custom"
    definition_json: str = "{}"         # the head revision's authored content
    definition_hash: str = ""
    # Archived presets leave the catalogue; their revisions stay readable for
    # every job that names them.
    archived_at: datetime | None = None

    @property
    def params(self) -> dict[str, Any]:
        return _json_or(self.params_json, {})

    @property
    def fields(self) -> list[dict[str, Any]]:
        return [item for item in _json_or(self.fields_json, []) if isinstance(item, dict)]

    @property
    def inputs(self) -> dict[str, Any]:
        return _json_or(self.inputs_json, {})

    @property
    def defaulted(self) -> list[str]:
        return [item for item in _json_or(self.defaulted_json, []) if isinstance(item, str)]

    @property
    def definition(self) -> dict[str, Any]:
        return _json_or(self.definition_json, {})

    @property
    def styles(self) -> list[dict[str, str]]:
        return [{"id": str(item.get("id") or ""), "text": str(item.get("text") or "")}
                for item in _json_or(self.styles_json, []) if isinstance(item, dict)]


class RecipeRevision(SQLModel, table=True):
    """One immutable revision of a studio preset.

    Append-only: an edit writes a new row and never changes an old one, and a
    preset that is archived keeps all of them, because jobs and Variant Set
    snapshots name (preset_id, revision) and must resolve to exactly what ran.
    """
    __table_args__ = (
        Index("ux_reciperevision_preset_revision", "preset_id", "revision", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    recipe_id: int = Field(index=True)
    preset_id: str = ""
    revision: int = 1
    schema_version: int = 2
    origin: str = ""
    definition_json: str = "{}"
    definition_hash: str = ""
    # The revision this one was edited from, or None for a first revision.
    based_on: int | None = None
    client: str = ""
    created_at: datetime = Field(default_factory=_now)

    @property
    def definition(self) -> dict[str, Any]:
        return _json_or(self.definition_json, {})


class Resource(SQLModel, table=True):
    """An immutable identity for model weights or an adapter (0014).

    `id` is the provider identity plus an exact version (studio.contracts
    resource_id): "hf:<repo>@<commit>", "civitai:model/../file/..",
    "local:sha256:<hash>". Facts are kept apart on purpose: `state` is the
    lifecycle, `verification` whether bytes were checked against the identity,
    `compat_json` what probes observed, `license_json` what the provider said.
    None of them implies another.
    """
    id: str = Field(primary_key=True)
    kind: str = Field(default="model", index=True)   # model | adapter | component
    family: str = "unknown"
    provider: str = ""
    provider_ref: str = ""
    revision: str = ""
    content_hash: str = Field(default="", index=True)  # "sha256:<hex>" or ""
    format: str = ""
    size_bytes: int = 0
    label: str = ""
    files_json: str = "[]"
    license_json: str = "{}"
    access: str = "unknown"                           # public | gated | unknown
    state: str = "known"
    verification: str = "unverified"
    compat_json: str = "{}"
    constraints_json: str = "{}"
    error: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def files(self) -> list[dict[str, Any]]:
        return [f for f in _json_or(self.files_json, []) if isinstance(f, dict)]

    @property
    def license(self) -> dict[str, Any]:
        return _json_or(self.license_json, {})

    @property
    def compat(self) -> dict[str, Any]:
        return _json_or(self.compat_json, {})

    @property
    def constraints(self) -> dict[str, Any]:
        return _json_or(self.constraints_json, {})


class ResourceInstall(SQLModel, table=True):
    """One copy of a resource on one target (0014).

    `location` is private: relative to the managed root it lives under
    ("loras/imported/x.safetensors", "hf-cache:<repo>"), never an absolute path
    and never shown to API clients as one. A missing file stays a row with
    state "missing" so the adapter remains visible instead of vanishing.
    """
    __table_args__ = (
        Index("ux_resourceinstall_target_location", "target", "location", unique=True),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True)
    resource_id: str = Field(index=True)
    target: str = "local"
    location: str = ""
    state: str = "installed"                     # installed | verified | missing | failed
    size_bytes: int = 0
    mtime_ns: int | None = None
    verified_hash: str = ""
    verified_at: datetime | None = None
    error: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ResourceImport(SQLModel, table=True):
    """One explicit provider import task (0016). Never holds a credential."""
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    provider: str = ""                       # "hf" | "civitai"
    provider_ref: str = ""                   # repo id, or model/<id>/version/<id>/file/<id>
    revision: str = ""                       # exact commit or version id
    source_url: str = ""                     # without query string
    selection_json: str = "{}"
    kind: str = ""                           # adapter | model | diffusers
    filename: str = ""
    expected_size: int | None = None
    expected_sha256: str = ""
    # queued | running | canceling | canceled | failed | interrupted | done
    state: str = Field(default="queued", index=True)
    progress: float = 0.0
    bytes_done: int = 0
    bytes_total: int | None = None
    error: str = ""
    error_code: str = ""
    resource_id: str | None = None
    license_json: str = "{}"
    license_acknowledged: bool = False
    client: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None

    @property
    def selection(self) -> dict[str, Any]:
        return _json_or(self.selection_json, {})

    @property
    def license(self) -> dict[str, Any]:
        return _json_or(self.license_json, {})


class ReferenceSet(SQLModel, table=True):
    """The head of a reference set (0017): its name and latest revision."""
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    revision: int = 1
    digest: str = ""
    client: str = ""
    archived_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ReferenceSetRevision(SQLModel, table=True):
    """One immutable revision of a reference set (0017)."""
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    set_id: int = Field(foreign_key="referenceset.id", index=True)
    revision: int
    definition_json: str = "{}"
    digest: str = ""
    based_on: int | None = None
    client: str = ""
    created_at: datetime = Field(default_factory=_now)

    @property
    def definition(self) -> dict[str, Any]:
        return _json_or(self.definition_json, {})


class CanvasProject(SQLModel, table=True):
    """The head of a canvas project (0019): its name and latest revision."""
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    revision: int = 1
    digest: str = ""
    width: int = 0
    height: int = 0
    client: str = ""
    archived_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class CanvasRevision(SQLModel, table=True):
    """One immutable revision of a canvas document (0019)."""
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="canvasproject.id", index=True)
    revision: int
    document_json: str = "{}"
    digest: str = ""
    based_on: int | None = None
    client: str = ""
    created_at: datetime = Field(default_factory=_now)

    @property
    def document(self) -> dict[str, Any]:
        return _json_or(self.document_json, {})


class VariantRecipe(SQLModel, table=True):
    """A reusable Variant Set definition: configuration only.

    It never points at the jobs or results of a run, so editing it cannot change
    what an earlier set meant — every set carries its own snapshot. Survives
    `clear_all()` for the same reason presets do: it is something the user
    built, not something a run produced.
    """
    # Never reused: a deleted recipe's id must not come back as a different
    # recipe behind a set's `recipe_id` or a link someone kept.
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    description: str = ""
    config_json: str = "{}"
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def config(self) -> dict[str, Any]:
        return _json_or(self.config_json, {})


class VariantSet(SQLModel, table=True):
    """One execution of a recipe: an immutable snapshot plus live state.

    Every child is an ordinary job of an existing kind, grouped by `group_id` —
    the same column X/Y grids use — so lanes, rerun and restart handle them with
    no special case. `counts_json` is a cache recomputed from the items, never
    incremented, so it cannot drift from what the items say.
    """
    # Ids are never reused (AUTOINCREMENT). Deleting a set keeps its jobs and
    # assets, whose provenance names this id; SQLite would otherwise hand the
    # same id to the next set and make that provenance point at the wrong run.
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    name: str = ""
    # Informational: which saved recipe this came from, if any. The snapshot in
    # recipe_json is what the set actually ran and is what replay reads.
    recipe_id: int | None = Field(default=None, index=True)
    recipe_json: str = "{}"
    operation: str = ""                     # stage 1's job kind, for listing
    source_asset_ids_json: str = "[]"
    group_id: str = Field(index=True, unique=True)
    # Caller-supplied submission identity; a retried create returns this set.
    request_id: str | None = Field(default=None, index=True, unique=True)
    status: str = Field(default="active", index=True)
    expected: int = 0                       # every item across every stage
    counts_json: str = "{}"
    collection_id: int | None = None        # optional output collection
    # Replay context: the resolved seed and the versions that produced it.
    replay_json: str = "{}"
    canceled_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def recipe(self) -> dict[str, Any]:
        return _json_or(self.recipe_json, {})

    @property
    def counts(self) -> dict[str, int]:
        return _json_or(self.counts_json, {})

    @property
    def source_asset_ids(self) -> list[int]:
        return [int(v) for v in _json_or(self.source_asset_ids_json, []) if isinstance(v, int)]

    @property
    def replay(self) -> dict[str, Any]:
        return _json_or(self.replay_json, {})


class VariantItem(SQLModel, table=True):
    """One materialized combination at one stage, with its own state machine.

    The row exists from the moment the set is created; a Job row exists only
    once the item is runnable. That is what lets a dependent stage sit in
    `pending` or `blocked` without inventing a job status for it, and why a
    restart needs no special handling for work that never started.
    """
    __table_args__ = (
        Index("ux_variantitem_set_stage_key", "set_id", "stage", "key", unique=True),
        {"sqlite_autoincrement": True},   # never reused; see VariantSet
    )

    id: int | None = Field(default=None, primary_key=True)
    set_id: int = Field(index=True)
    stage: int = 0                          # 0-based position in the recipe's stage list
    ordinal: int = 0                        # deterministic position within the stage
    key: str = ""                           # canonical, accumulated across stages
    values_json: str = "{}"                 # axis -> selected value, every stage so far
    parent_item_id: int | None = Field(default=None, index=True)
    source_asset_ids_json: str = "[]"       # what this attempt consumed
    params_json: str = "{}"                 # effective child params, latest attempt
    job_id: int | None = Field(default=None, index=True)
    attempts: int = 0
    history_json: str = "[]"                # earlier attempts, oldest first
    asset_ids_json: str = "[]"
    state: str = Field(default="pending", index=True)
    state_reason: str = ""
    validation_state: str = "pending"
    validation_json: str = "[]"
    output_name: str = ""                   # deterministic export path
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def values(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in _json_or(self.values_json, {}).items()}

    @property
    def params(self) -> dict[str, Any]:
        return migrate_params(_json_or(self.params_json, {}))

    @property
    def asset_ids(self) -> list[int]:
        return [int(v) for v in _json_or(self.asset_ids_json, []) if isinstance(v, int)]

    @property
    def source_asset_ids(self) -> list[int]:
        return [int(v) for v in _json_or(self.source_asset_ids_json, []) if isinstance(v, int)]

    @property
    def history(self) -> list[dict[str, Any]]:
        return [h for h in _json_or(self.history_json, []) if isinstance(h, dict)]

    @property
    def validation(self) -> list[dict[str, Any]]:
        return [r for r in _json_or(self.validation_json, []) if isinstance(r, dict)]


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
    client: str = ""
    # Preset jobs only: the plan they execute and where they run.
    plan_id: int | None = None
    backend_id: str | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, j: Job) -> JobRead:
        # Params hold absolute input paths and errors hold tracebacks: shown
        # relative to the app folder, never where it is installed (redaction.py).
        return cls(
            id=j.id, kind=j.kind, status=j.status, progress=j.progress,
            message=redact_text(j.message or ""), params=redact(j.params),
            result=redact(j.result), error=redact_text(j.error or ""), tip=j.tip or "",
            priority=j.priority or 0, group_id=j.group_id, model_key=j.model_key,
            client=j.client or "", plan_id=j.plan_id, backend_id=j.backend_id,
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
    # Who submitted the job that produced this file. Empty for imports and for
    # files saved before client tagging.
    client: str = ""

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
            client=str(a.meta.get("client") or ""),
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
                   params=redact(h.params), favorite=bool(h.favorite), created_at=h.created_at)


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
