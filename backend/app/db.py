"""SQLite engine + small helper functions for jobs and assets."""
from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import event, func
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, desc, select

from . import migrations
from .config import settings
from .models import (
    Asset,
    AssetContent,
    AssetKind,
    Collection,
    CollectionItem,
    Job,
    JobStatus,
    PromptHistory,
    UserPreset,
    stamp_params,
)

_engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},  # used from the worker thread too
)


@event.listens_for(_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    """WAL lets the lanes' per-step progress writes coexist with UI reads, and a
    generous busy timeout keeps a long transaction (clear_all) from surfacing as
    'database is locked' inside a running job's progress callback."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()


# ---- single-process guard -------------------------------------------------
#
# SQLite's WAL keeps the *file* consistent under concurrent writers. It does
# nothing about two schedulers: `make dev` twice, or `make dev` alongside
# `make start`, gives two processes two job lanes draining one queue, both
# claiming the same rows and both running the same generation.
#
# An OS-level advisory lock on a sidecar file is the fix. The OS releases it when
# the process exits, including on a crash, so there is no stale lock to clean up.
_PROCESS_LOCK: Any = None


class DatabaseInUseError(RuntimeError):
    """Another process already holds this database."""


def acquire_process_lock(db_path: Path | None = None) -> bool:
    """Take the single-process lock, or raise. True if taken, False if skipped.

    Fails immediately rather than waiting: a second instance is a mistake to
    report, not a queue to join. Returns False when there is nothing to lock (an
    in-memory database) or when `filelock` is unavailable, because refusing to
    start over a missing optional dependency would be worse than the race it
    prevents.
    """
    global _PROCESS_LOCK
    if _PROCESS_LOCK is not None:
        return True
    path = db_path or Path(settings.db_path)
    if not path or str(path) == ":memory:":
        return False
    try:
        from filelock import FileLock, Timeout
    except ImportError:  # pragma: no cover - filelock ships with the lockfile
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path) + ".lock")
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise DatabaseInUseError(
            f"Another instance of The Wizard's Brush is already using {path}.\n"
            "Two processes sharing one database means two job queues running the "
            "same work.\n"
            "Stop the other instance, or point this one somewhere else with "
            "OUTPUT_DIR."
        ) from None
    _PROCESS_LOCK = lock
    return True


def release_process_lock() -> None:
    """Give up the lock. Called on shutdown; the OS would do it anyway."""
    global _PROCESS_LOCK
    if _PROCESS_LOCK is not None:
        with contextlib.suppress(Exception):
            _PROCESS_LOCK.release()
        _PROCESS_LOCK = None


def init_db() -> None:
    settings.ensure_dirs()
    SQLModel.metadata.create_all(_engine)
    _migrate()


def _migrate(engine=None) -> None:
    """Run every unapplied schema migration, backing the database up first.

    SQLModel.create_all() builds tables that do not exist but never ALTERs one
    that does, so anything added to a model after a database was created has to
    arrive here. What used to be an additive column list is now an ordered,
    recorded, transactional sequence — see backend/app/migrations/.
    """
    eng = engine or _engine
    raw = eng.raw_connection()
    try:
        conn = raw.driver_connection
        assert conn is not None, "sqlite engine must expose a DBAPI connection"
        # An in-memory engine (tests) has no file to back up.
        url_path = eng.url.database
        db_path = Path(url_path) if url_path and url_path != ":memory:" else None
        migrations.run_all(conn, db_path)
    finally:
        raw.close()


def reconcile_orphans() -> int:
    """Resolve jobs the previous process left mid-flight.

    Only *running* jobs. Their handler lived in a process that is gone, and a
    generation cannot be picked up half-done, so they resolve to canceled.

    Queued jobs are deliberately left alone. They never started, so there is no
    partial work to reconcile — and cancelling them threw away a queue the user
    had deliberately built, which is a much worse thing for a restart to cost
    than the one job that was actually interrupted. `queue.resume_queued()` puts
    them back on their lanes once those exist.
    """
    n = 0
    with session() as s:
        for j in s.exec(select(Job).where(Job.status == JobStatus.running.value)):
            j.status = JobStatus.canceled.value
            j.message = "interrupted by restart"
            j.finished_at = datetime.now(UTC)
            asset_ids = list(s.exec(
                select(Asset.id).where(Asset.job_id == j.id).order_by(Asset.id)  # type: ignore[arg-type]
            ))
            if asset_ids:
                previous = j.result
                previous["asset_ids"] = [int(value) for value in asset_ids if value is not None]
                previous["partial"] = True
                j.result_json = json.dumps(previous)
            s.add(j)
            n += 1
        s.commit()
    return n


def remote_orphans() -> list[tuple[int, str | None, str | None]]:
    """Remote identities for running rows that restart reconciliation will cancel."""
    with session() as s:
        rows = s.exec(select(Job).where(
            Job.status == JobStatus.running.value,
            (Job.remote_token.is_not(None) | Job.remote_client_id.is_not(None)),  # type: ignore[union-attr]
        ))
        return [(int(j.id), j.remote_token, j.remote_client_id)
                for j in rows if j.id is not None]


def session() -> Session:
    return Session(_engine)


# ---- jobs -----------------------------------------------------------------
def create_job_once(
    kind: str,
    params: dict[str, Any],
    group_id: str | None = None,
    model_key: str | None = None,
    request_id: str | None = None,
) -> tuple[Job, bool]:
    """Create a job, or return the row already owning ``request_id``.

    The unique index is the authority. A read-before-write check alone races
    when two retried requests arrive together; catching the constraint turns
    both callers into the same durable job identity.
    """
    with session() as s:
        job = Job(kind=kind, params_json=json.dumps(stamp_params(params)), group_id=group_id,
                  model_key=model_key, request_id=request_id)
        s.add(job)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            if not request_id:
                raise
            existing = s.exec(select(Job).where(Job.request_id == request_id)).first()
            if existing is None:
                raise
            return existing, False
        s.refresh(job)
        return job, True


def create_job(kind: str, params: dict[str, Any], group_id: str | None = None,
               model_key: str | None = None, request_id: str | None = None) -> Job:
    """Backward-compatible row creator; submission paths use create_job_once."""
    return create_job_once(kind, params, group_id, model_key, request_id)[0]


def job_for_request(request_id: str) -> Job | None:
    with session() as s:
        return s.exec(select(Job).where(Job.request_id == request_id)).first()


def queued_in_order(ids: list[int]) -> list[Job]:
    """Of the given job ids, the ones still queued — in run order
    (priority DESC, id ASC). Used by the lanes to pick their next job.

    Whole rows rather than ids because the scheduler needs `priority` and
    `model_key` for every candidate it considers. Fetching ids here and then
    calling `get_job` per candidate opened a fresh Session per row — up to 33 of
    them for one pick, on the dequeue path and again on every poll of
    /api/jobs/next. One query answers the whole question.
    """
    if not ids:
        return []
    with session() as s:
        return list(s.exec(
            select(Job)
            .where(Job.id.in_(ids), Job.status == JobStatus.queued.value)  # type: ignore[union-attr]
            .order_by(desc(Job.priority), Job.id)  # type: ignore[arg-type]
        ))


def queued_ids_in_order(ids: list[int]) -> list[int]:
    """`queued_in_order` as bare ids. One query, two shapes — the SQL lives once."""
    return [int(j.id) for j in queued_in_order(ids) if j.id is not None]


def list_jobs_by_group(group_id: str) -> list[Job]:
    with session() as s:
        return list(s.exec(select(Job).where(Job.group_id == group_id).order_by(Job.id)))  # type: ignore[arg-type]


def queued_jobs_for_lane(kinds: set[str] | None = None) -> list[Job]:
    """All queued jobs (optionally restricted to a kind set), in run order."""
    with session() as s:
        q = select(Job).where(Job.status == JobStatus.queued.value)
        if kinds is not None:
            q = q.where(Job.kind.in_(kinds))  # type: ignore[attr-defined]
        return list(s.exec(q.order_by(desc(Job.priority), Job.id)))  # type: ignore[arg-type]


def max_queued_priority() -> int:
    with session() as s:
        rows = s.exec(select(Job.priority).where(Job.status == JobStatus.queued.value))
        vals = [int(v or 0) for v in rows]
        return max(vals) if vals else 0


def get_job(job_id: int) -> Job | None:
    with session() as s:
        return s.get(Job, job_id)


def list_jobs(limit: int = 100, status: str | None = None) -> list[Job]:
    with session() as s:
        q = select(Job)
        if status:
            q = q.where(Job.status == status)
        q = q.order_by(desc(Job.created_at)).limit(limit)
        return list(s.exec(q))


def update_job(job_id: int, **fields: Any) -> Job | None:
    with session() as s:
        job = s.get(Job, job_id)
        if not job:
            return None
        for k, v in fields.items():
            if k in ("params", "result"):
                setattr(job, f"{k}_json", json.dumps(v))
            else:
                setattr(job, k, v)
        s.add(job)
        s.commit()
        s.refresh(job)
        return job


def set_remote_identity(job_id: int, client_id: str, token: str | None = None) -> None:
    """Record the one remote invocation this local job currently owns."""
    with session() as s:
        s.execute(sa_update(Job).where(Job.id == job_id)  # type: ignore[arg-type]
                  .values(remote_client_id=client_id, remote_token=token))
        s.commit()


def set_progress(job_id: int, frac: float, message: str = "") -> None:
    """Hot path — called from progress callbacks every denoising step. A single
    UPDATE with no row fetch/refresh, unlike update_job."""
    with session() as s:
        s.execute(sa_update(Job).where(Job.id == job_id)  # type: ignore[arg-type]
                  .values(progress=frac, message=message))
        s.commit()


def mark_running(job_id: int) -> None:
    update_job(job_id, status=JobStatus.running.value, started_at=datetime.now(UTC),
               progress=0.0, message="starting")


def mark_done(job_id: int, result: dict[str, Any]) -> None:
    update_job(job_id, status=JobStatus.done.value, finished_at=datetime.now(UTC),
               progress=1.0, message="done", result=result)


def mark_canceled(job_id: int, message: str, result: dict[str, Any] | None = None) -> None:
    fields: dict[str, Any] = {
        "status": JobStatus.canceled.value,
        "finished_at": datetime.now(UTC),
        "message": message,
    }
    if result is not None:
        fields["result"] = result
    update_job(job_id, **fields)


def mark_error(
    job_id: int, err: str, tip: str = "", result: dict[str, Any] | None = None,
) -> None:
    """Record a failure. `tip` is the actionable sentence the UI leads with; the
    traceback stays in `error` behind a disclosure."""
    fields: dict[str, Any] = {
        "status": JobStatus.error.value,
        "finished_at": datetime.now(UTC),
        "error": err,
        "message": "error",
        "tip": tip,
    }
    if result is not None:
        fields["result"] = result
    update_job(job_id, **fields)


def asset_ids_for_job(job_id: int) -> list[int]:
    """Every committed output for a job, including outputs made before failure.

    A batch handler returns its ids only after the loop. Querying the durable
    rows is therefore the only truthful recovery path when item k fails after
    items 0..k-1 were already saved.
    """
    with session() as s:
        values = s.exec(
            select(Asset.id).where(Asset.job_id == job_id).order_by(Asset.id)  # type: ignore[arg-type]
        )
        return [int(value) for value in values if value is not None]


# ---- assets ---------------------------------------------------------------
def add_asset(
    kind: str, path: Path, *, thumb: str | None = None, width: int | None = None,
    height: int | None = None, job_id: int | None = None, generator: str = "",
    meta: dict[str, Any] | None = None, content_hash: str | None = None,
    size_bytes: int = 0, mime_type: str = "",
) -> Asset:
    """Record a produced file.

    `content_hash` is supplied by the save path, which hashes while writing.
    When it is absent (an older call site, or a file we did not write ourselves)
    the row lands with content_id NULL and the enrichment worker fills it in —
    the same path existing rows take.
    """
    content_id = None
    if content_hash:
        content_id = get_or_create_content(
            content_hash, size_bytes=size_bytes, mime_type=mime_type).id
    with session() as s:
        a = Asset(
            kind=kind, path=str(path), filename=path.name, thumb=thumb, width=width,
            height=height, job_id=job_id, generator=generator,
            meta_json=json.dumps(stamp_params(meta or {})), content_id=content_id,
            mtime_ns=_mtime_ns(path),
        )
        s.add(a)
        s.commit()
        s.refresh(a)
        return a


def get_asset(asset_id: int, *, include_deleted: bool = False) -> Asset | None:
    """One asset, or None. Trashed rows are invisible unless asked for — they
    must not surface in the lightbox, exports, or "more like this"."""
    with session() as s:
        a = s.get(Asset, asset_id)
        if a is not None and a.deleted_at is not None and not include_deleted:
            return None
        return a


def clear_all() -> dict:
    """Delete every generated file and wipe the asset + job history. Destructive."""
    files = 0
    for d in (settings.images_dir, settings.videos_dir, settings.thumbs_dir, settings.uploads_dir):
        for f in d.glob("*"):
            try:
                f.unlink()
                files += 1
            except Exception:  # noqa: BLE001 — degrade quietly; the caller must not fail here
                pass
    with session() as s:
        n_assets = int(s.exec(select(func.count()).select_from(Asset)).one())
        n_jobs = int(s.exec(select(func.count()).select_from(Job)).one())
        # Bulk DELETE — loading every row just to delete it would drag a large
        # library through the ORM one object at a time.
        s.execute(sa_delete(Asset))
        s.execute(sa_delete(Job))
        s.commit()
    return {"files_deleted": files, "assets_deleted": n_assets, "jobs_deleted": n_jobs}


def delete_asset(asset_id: int) -> bool:
    """Move one asset to the trash. Files stay on disk until it is emptied."""
    return delete_assets([asset_id]) == 1


def _mtime_ns(path: Path) -> int | None:
    """File mtime, or None if it is already gone. Used to detect edits later."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _unlink_asset_files(a: Asset) -> None:
    """Remove an asset's files — unless another live row points at the same path.

    The guard is on the **path**, not on the content hash. Two assets sharing a
    content hash have identical bytes in *two different files*: every save goes
    to its own millisecond-stamped name, so nothing is stored once and referenced
    twice. Refusing to unlink because a duplicate exists elsewhere would delete
    the thumbnail, drop the row, and strand the full-size file — and no sweep
    reclaims `images/`, so it would leak for the life of the library.

    Content addressing here is duplicate *detection* (the gallery's "2 copies"
    badge), not deduplicated *storage*. If a future call site ever registers an
    existing file under a second row, this guard is what keeps that safe.
    """
    if a.id is not None and path_is_shared(a.id):
        thumb = str(settings.thumbs_dir / a.thumb) if a.thumb else None
        if thumb:
            with contextlib.suppress(Exception):
                Path(thumb).unlink(missing_ok=True)
        return
    sidecar = str(Path(a.path).with_suffix(Path(a.path).suffix + ".json")) \
        if a.kind == AssetKind.video.value else None
    for fp in (a.path, str(settings.thumbs_dir / a.thumb) if a.thumb else None, sidecar):
        if fp:
            with contextlib.suppress(Exception):
                Path(fp).unlink(missing_ok=True)


def delete_assets(ids: list[int]) -> int:
    """Soft-delete: stamp deleted_at and leave the row and files alone.

    Deletion used to be immediate and irreversible — one misclick on a
    multi-select and the files were gone. Now it is a state change, and
    purge_deleted() is the only thing that touches the filesystem.
    """
    if not ids:
        return 0
    now = datetime.now(UTC)
    n = 0
    with session() as s:
        for aid in ids:
            a = s.get(Asset, aid)
            if a and a.deleted_at is None:
                a.deleted_at = now
                s.add(a)
                n += 1
        s.commit()
    return n


def restore_assets(ids: list[int]) -> int:
    """Bring assets back out of the trash."""
    n = 0
    with session() as s:
        for aid in ids:
            a = s.get(Asset, aid)
            if a and a.deleted_at is not None:
                a.deleted_at = None
                s.add(a)
                n += 1
        s.commit()
    return n


def purge_deleted(ids: list[int] | None = None, older_than_days: int | None = None) -> int:
    """Permanently remove trashed assets: unlink the files, drop the rows.

    `ids` empties specific items, `older_than_days` empties by age, neither
    empties the whole trash. Only ever touches rows already soft-deleted, so it
    cannot take a live asset even if handed a wrong id.
    """
    with session() as s:
        stmt = select(Asset).where(Asset.deleted_at.is_not(None))  # type: ignore[union-attr]
        if ids:
            stmt = stmt.where(Asset.id.in_(ids))  # type: ignore[union-attr]
        if older_than_days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
            stmt = stmt.where(Asset.deleted_at < cutoff)  # type: ignore[operator]
        rows = list(s.exec(stmt))
        for a in rows:
            _unlink_asset_files(a)
            s.delete(a)
        s.commit()
        return len(rows)


def count_deleted() -> int:
    with session() as s:
        return len(list(s.exec(select(Asset.id).where(Asset.deleted_at.is_not(None)))))  # type: ignore[union-attr]


# ---- asset organization (favorites / ratings / tags) ----------------------
def get_assets(ids: list[int], *, include_deleted: bool = False) -> list[Asset]:
    if not ids:
        return []
    with session() as s:
        stmt = select(Asset).where(Asset.id.in_(ids))  # type: ignore[union-attr]
        if not include_deleted:
            stmt = stmt.where(Asset.deleted_at.is_(None))  # type: ignore[union-attr]
        return list(s.exec(stmt))


def merge_asset_tags(asset_id: int, new_tags: list[str], limit: int = 20,
                     **fields: Any) -> Asset | None:
    """Read-merge-write in ONE session: enrichment finishes seconds after its
    initial read, and a tag the user set in between must survive the update."""
    with session() as s:
        a = s.get(Asset, asset_id)
        if not a:
            return None
        tags = a.tags
        tags += [t for t in new_tags if t not in tags]
        a.tags_json = json.dumps(tags[:limit])
        for k, v in fields.items():
            setattr(a, k, v)
        s.add(a)
        s.commit()
        s.refresh(a)
        return a


def update_asset(asset_id: int, **fields: Any) -> Asset | None:
    with session() as s:
        a = s.get(Asset, asset_id)
        if not a:
            return None
        for k, v in fields.items():
            if k == "tags":
                a.tags_json = json.dumps(list(v))
            else:
                setattr(a, k, v)
        s.add(a)
        s.commit()
        s.refresh(a)
        return a


def set_asset_grade(asset_id: int, grade: dict[str, Any], rating: int) -> Asset | None:
    """Persist a complete human rubric and its gallery-friendly star summary.

    They change together in one transaction. Saving the rubric and the star
    rating separately could make the leaderboard rank a new review by its old
    score after a crash or a browser retry.
    """
    with session() as s:
        a = s.get(Asset, asset_id)
        if not a:
            return None
        a.grade_json = json.dumps(grade)
        a.rating = max(0, min(int(rating), 5))
        s.add(a)
        s.commit()
        s.refresh(a)
        return a


def search_assets(
    *, kind: str | None = None, generator: str | None = None, favorite: bool | None = None,
    min_rating: int = 0, q: str | None = None, since_days: int | None = None,
    sort: str = "newest", limit: int = 120, offset: int = 0, deleted: bool = False,
    collection_id: int | None = None,
) -> tuple[list[Asset], int]:
    """Filtered, paginated gallery query. Returns (rows, total_matching)."""
    def _filtered(stmt):
        # The trash is a separate view, so every normal query hides it. Opt in
        # with deleted=True rather than leaking soft-deleted rows into search,
        # stats, exports and the "more like this" pickers.
        stmt = stmt.where(Asset.deleted_at.is_not(None) if deleted  # type: ignore[union-attr]
                          else Asset.deleted_at.is_(None))  # type: ignore[union-attr]
        if kind:
            stmt = stmt.where(Asset.kind == kind)
        if generator:
            stmt = stmt.where(Asset.generator.like(f"{generator}%"))  # type: ignore[attr-defined]
        if favorite is not None:
            stmt = stmt.where(Asset.favorite == favorite)
        if min_rating:
            stmt = stmt.where(Asset.rating >= min_rating)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                (Asset.meta_json.ilike(like)) | (Asset.tags_json.ilike(like))  # type: ignore[attr-defined]
                | (Asset.caption.ilike(like))  # type: ignore[attr-defined]  # Florence-2 captions are searchable
            )
        if collection_id is not None:
            member_ids = select(CollectionItem.asset_id).where(
                CollectionItem.collection_id == collection_id)
            stmt = stmt.where(Asset.id.in_(member_ids))  # type: ignore[union-attr]
        if since_days:
            cutoff = datetime.now(UTC) - timedelta(days=since_days)
            stmt = stmt.where(Asset.created_at >= cutoff)
        return stmt

    with session() as s:
        total = s.exec(_filtered(select(func.count()).select_from(Asset))).one()
        # Every sort ends in a unique column.
        #
        # This is a paging requirement, not a nicety. `rating`, `favorite` and
        # `used` all have enormous tie groups — most of a library shares rating
        # 0 and used_count 0 — and SQLite is free to return tied rows in any
        # order it likes, including a different order for the same query. With
        # OFFSET/LIMIT that means an asset can appear on two consecutive pages
        # while another never appears at all. `id DESC` breaks every tie and
        # matches the newest-first intent of the primary key.
        tiebreak = desc(Asset.id)
        order = {
            "newest": (desc(Asset.created_at),), "oldest": (Asset.created_at,),
            "rating": (desc(Asset.rating),), "favorite": (desc(Asset.favorite),),
            # Behaviour, not intent: what you actually exported or built on.
            # NULL last_used_at sorts last under DESC, so assets you have never
            # used sink below the ones you have — which is the useful order.
            "used": (desc(Asset.used_count),),
            "recently_used": (desc(Asset.last_used_at),),
        }.get(sort, (desc(Asset.created_at),))
        rows = list(s.exec(_filtered(select(Asset))
                           .order_by(*order, tiebreak).offset(offset).limit(limit)))
        return rows, int(total)


# ---- prompt history -------------------------------------------------------
def add_history(kind: str, prompt: str, negative: str, params: dict) -> None:
    if not (prompt or "").strip():
        return
    with session() as s:
        # de-dup: if the most recent entry has the same prompt+kind, skip.
        last = s.exec(select(PromptHistory).order_by(desc(PromptHistory.created_at)).limit(1)).first()
        if last and last.prompt == prompt and last.kind == kind:
            return
        s.add(PromptHistory(kind=kind, prompt=prompt, negative=negative,
                            params_json=json.dumps(stamp_params(params))))
        s.commit()


def list_history(limit: int = 100, favorite: bool | None = None) -> list[PromptHistory]:
    with session() as s:
        q = select(PromptHistory)
        if favorite:
            q = q.where(PromptHistory.favorite == True)  # noqa: E712
        q = q.order_by(desc(PromptHistory.created_at)).limit(limit)
        return list(s.exec(q))


def set_history_favorite(hist_id: int, favorite: bool) -> bool:
    with session() as s:
        h = s.get(PromptHistory, hist_id)
        if not h:
            return False
        h.favorite = favorite
        s.add(h)
        s.commit()
        return True


def delete_history(hist_id: int) -> bool:
    with session() as s:
        h = s.get(PromptHistory, hist_id)
        if not h:
            return False
        s.delete(h)
        s.commit()
        return True


# ---- user presets ---------------------------------------------------------
def add_user_preset(type: str, name: str, payload: dict, scope: str = "all") -> UserPreset:
    with session() as s:
        p = UserPreset(type=type, name=name, payload_json=json.dumps(payload), scope=scope)
        s.add(p)
        s.commit()
        s.refresh(p)
        return p


def list_user_presets(type: str | None = None) -> list[UserPreset]:
    with session() as s:
        q = select(UserPreset)
        if type:
            q = q.where(UserPreset.type == type)
        return list(s.exec(q.order_by(desc(UserPreset.created_at))))


def preset_previews(presets: list[UserPreset]) -> dict[int, Asset]:
    """preset_id -> the live asset it uses as a thumbnail.

    One query for the whole list rather than one per preset. A preview pointing
    at a deleted or purged asset simply drops out, so the picker degrades to a
    name instead of showing a broken image.
    """
    wanted = {p.id: p.preview_asset_id for p in presets
              if p.id is not None and p.preview_asset_id is not None}
    if not wanted:
        return {}
    with session() as s:
        rows = s.exec(
            select(Asset).where(Asset.id.in_(set(wanted.values())),  # type: ignore[union-attr]
                                Asset.deleted_at.is_(None))  # type: ignore[union-attr]
        )
        by_id = {a.id: a for a in rows}
    return {pid: by_id[aid] for pid, aid in wanted.items() if aid in by_id}


def set_preset_preview(preset_id: int, asset_id: int | None) -> UserPreset | None:
    """Star a gallery image as this preset's thumbnail, or clear it."""
    with session() as s:
        p = s.get(UserPreset, preset_id)
        if p is None:
            return None
        p.preview_asset_id = asset_id
        s.add(p)
        s.commit()
        s.refresh(p)
        return p


def delete_user_preset(preset_id: int) -> bool:
    with session() as s:
        p = s.get(UserPreset, preset_id)
        if not p:
            return False
        s.delete(p)
        s.commit()
        return True


# ---- stats + maintenance --------------------------------------------------
def stats() -> dict:
    with session() as s:
        live = Asset.deleted_at.is_(None)  # type: ignore[union-attr]
        n_img = s.exec(select(func.count()).select_from(Asset).where(
            Asset.kind == "image", live)).one()
        n_vid = s.exec(select(func.count()).select_from(Asset).where(
            Asset.kind == "video", live)).one()
        n_fav = s.exec(select(func.count()).select_from(Asset).where(
            Asset.favorite == True, live)).one()  # noqa: E712
        cutoff = datetime.now(UTC) - timedelta(days=1)
        n_today = s.exec(select(func.count()).select_from(Asset).where(
            Asset.created_at >= cutoff, live)).one()
    disk = 0
    for d in (settings.images_dir, settings.videos_dir, settings.thumbs_dir, settings.uploads_dir):
        for f in d.glob("*"):
            with contextlib.suppress(Exception):
                disk += f.stat().st_size
    return {"images": int(n_img), "videos": int(n_vid), "favorites": int(n_fav),
            "today": int(n_today), "disk_bytes": disk}


def referenced_paths() -> set[str]:
    """Every file path still referenced by an Asset OR by a Job's saved params
    (uploads are reused for rerun), so orphan cleanup never deletes a live input."""
    keep: set[str] = set()
    with session() as s:
        keep |= {a.path for a in s.exec(select(Asset))}
        for j in s.exec(select(Job)):
            p = j.params
            for k in ("image_path", "mask_path", "src_path", "last_image_path"):
                if p.get(k):
                    keep.add(p[k])
            for extra in p.get("image_paths") or []:
                keep.add(extra)
    return keep


def sweep_orphan_files(max_age_min: int = 30) -> int:
    """Delete stale upload + leftover stitching segment files no asset points at.
    Only touches files older than max_age_min so an in-flight job is never hit."""
    import time

    keep = referenced_paths()
    removed = 0
    now = time.time()
    targets = list(settings.uploads_dir.glob("*"))
    targets += list(settings.videos_dir.glob("_seg_*"))
    targets += list(settings.videos_dir.glob("_concat_*"))
    targets += list(settings.videos_dir.glob("_persist_*"))  # crashed mid-persist
    for f in targets:
        try:
            if str(f) in keep:
                continue
            if now - f.stat().st_mtime < max_age_min * 60:
                continue
            f.unlink(missing_ok=True)
            removed += 1
        except Exception:  # noqa: BLE001 — degrade quietly; the caller must not fail here
            pass
    return removed


# ---- collections ----------------------------------------------------------
MAX_COLLECTION_NAME = 80


def list_collections() -> list[tuple[Collection, int]]:
    """(collection, member count), newest first. One query for counts rather
    than one per collection — the gallery sidebar renders all of them."""
    with session() as s:
        cols = list(s.exec(select(Collection).order_by(desc(Collection.created_at))))
        counts: dict[int, int] = {}
        for cid, n in s.exec(
            select(CollectionItem.collection_id, func.count(CollectionItem.id))  # type: ignore[arg-type]
            .group_by(CollectionItem.collection_id)  # type: ignore[arg-type]
        ):
            counts[cid] = n
        return [(c, counts.get(c.id or -1, 0)) for c in cols]


def create_collection(name: str) -> Collection:
    clean = (name or "").strip()[:MAX_COLLECTION_NAME] or "Untitled"
    with session() as s:
        c = Collection(name=clean)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c


def rename_collection(collection_id: int, name: str) -> bool:
    clean = (name or "").strip()[:MAX_COLLECTION_NAME]
    if not clean:
        return False
    with session() as s:
        c = s.get(Collection, collection_id)
        if not c:
            return False
        c.name = clean
        s.add(c)
        s.commit()
        return True


def delete_collection(collection_id: int) -> bool:
    """Removes the collection and its membership rows. Never the assets —
    a collection is a view over them, not a container that owns them."""
    with session() as s:
        c = s.get(Collection, collection_id)
        if not c:
            return False
        s.exec(sa_delete(CollectionItem).where(
            CollectionItem.collection_id == collection_id))  # type: ignore[arg-type]
        s.delete(c)
        s.commit()
        return True


def add_to_collection(collection_id: int, asset_ids: list[int]) -> int:
    """Idempotent: adding an asset twice is a no-op, not a duplicate row."""
    if not asset_ids:
        return 0
    with session() as s:
        if not s.get(Collection, collection_id):
            return 0
        existing = set(s.exec(
            select(CollectionItem.asset_id)
            .where(CollectionItem.collection_id == collection_id)))
        n = 0
        for aid in asset_ids:
            if aid in existing:
                continue
            s.add(CollectionItem(collection_id=collection_id, asset_id=aid))
            n += 1
        s.commit()
        return n


def remove_from_collection(collection_id: int, asset_ids: list[int]) -> int:
    if not asset_ids:
        return 0
    with session() as s:
        rows = list(s.exec(select(CollectionItem).where(
            CollectionItem.collection_id == collection_id,
            CollectionItem.asset_id.in_(asset_ids),  # type: ignore[attr-defined]
        )))
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


def collection_asset_ids(collection_id: int) -> list[int]:
    with session() as s:
        return list(s.exec(
            select(CollectionItem.asset_id)
            .where(CollectionItem.collection_id == collection_id)
            .order_by(desc(CollectionItem.added_at))))


# ---- history retention ----------------------------------------------------
JOB_HISTORY_KEEP = 2000


def prune_job_history(keep: int = JOB_HISTORY_KEEP) -> int:
    """Drop old finished jobs, keeping the most recent `keep`. Returns how many went.

    The `job` table otherwise grows without bound — every generation ever run
    stays a row that the lane's dequeue query has to look past.

    **A job referenced by a live asset is never pruned.** Rerun reads the job's
    params, and the asset modal shows them, so deleting the row would quietly
    break both for that image. Retention therefore has two floors: the recency
    window, and reachability from the gallery.
    """
    with session() as s:
        terminal = (JobStatus.done.value, JobStatus.error.value, JobStatus.canceled.value)
        # Reachable from a live asset.
        referenced = select(Asset.job_id).where(
            Asset.job_id.is_not(None),  # type: ignore[union-attr]
            Asset.deleted_at.is_(None),  # type: ignore[union-attr]
        )
        # The most recent `keep`, by when they actually finished.
        recent = (
            select(Job.id)
            .where(Job.status.in_(terminal))  # type: ignore[attr-defined]
            .order_by(desc(func.coalesce(Job.finished_at, Job.created_at)))
            .limit(keep)
        )
        doomed = list(s.exec(
            select(Job.id).where(
                Job.status.in_(terminal),  # type: ignore[attr-defined]
                Job.id.not_in(referenced),  # type: ignore[union-attr]
                Job.id.not_in(recent),  # type: ignore[union-attr]
            )
        ))
        if not doomed:
            return 0
        s.execute(sa_delete(Job).where(Job.id.in_(doomed)))  # type: ignore[union-attr]
        s.commit()
        return len(doomed)


# ---- content addressing ---------------------------------------------------
def get_or_create_content(hash_: str, *, size_bytes: int = 0,
                          mime_type: str = "") -> AssetContent:
    """The content row for `hash_`, inserting it if this is the first sighting.

    Races on the UNIQUE index rather than locking: two saves of identical bytes
    finishing at the same instant is a real possibility (a batch where two seeds
    produced the same image), and losing that race just means re-reading the row
    the winner inserted.
    """
    with session() as s:
        row = s.exec(select(AssetContent).where(AssetContent.hash == hash_)).first()
        if row is not None:
            return row
        row = AssetContent(hash=hash_, size_bytes=size_bytes, mime_type=mime_type)
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            existing = s.exec(select(AssetContent).where(AssetContent.hash == hash_)).first()
            if existing is None:
                raise
            return existing
        s.refresh(row)
        return row


def content_of(asset_id: int) -> AssetContent | None:
    with session() as s:
        a = s.get(Asset, asset_id)
        if a is None or a.content_id is None:
            return None
        return s.get(AssetContent, a.content_id)


def duplicate_ids(asset_id: int) -> list[int]:
    """Ids of other live assets whose bytes are identical to this one's.

    Empty when the asset has no content row yet (older rows are hashed in the
    background) or when nothing else shares it.
    """
    with session() as s:
        a = s.get(Asset, asset_id)
        if a is None or a.content_id is None:
            return []
        rows = s.exec(
            select(Asset.id)
            .where(Asset.content_id == a.content_id,
                   Asset.id != asset_id,
                   Asset.deleted_at.is_(None))  # type: ignore[union-attr]
            .order_by(Asset.id)  # type: ignore[arg-type]
        )
        return [int(i) for i in rows if i is not None]


def duplicate_counts(asset_ids: list[int]) -> dict[int, int]:
    """asset_id -> how many live assets share its bytes (including itself).

    One query for a whole gallery page. The alternative — asking per tile — is
    the classic N+1 that makes a grid view crawl once the library is large.
    """
    if not asset_ids:
        return {}
    with session() as s:
        rows = list(s.exec(
            select(Asset.id, Asset.content_id)
            .where(Asset.id.in_(asset_ids))  # type: ignore[union-attr]
        ))
        wanted = {int(aid): cid for aid, cid in rows if aid is not None and cid is not None}
        if not wanted:
            return {}
        # .all() before dict(): a multi-column exec() yields a result object,
        # not rows, and dict() over it raises rather than iterating.
        counts = dict(s.exec(
            select(Asset.content_id, func.count())
            .where(Asset.content_id.in_(set(wanted.values())),  # type: ignore[union-attr]
                   Asset.deleted_at.is_(None))  # type: ignore[union-attr]
            .group_by(Asset.content_id)  # type: ignore[arg-type]
        ).all())
        return {aid: int(counts.get(cid) or 1) for aid, cid in wanted.items()}


def path_is_shared(asset_id: int) -> bool:
    """Whether another live asset row points at the same file on disk.

    The guard on unlinking. Distinct from `duplicate_ids`, which answers the
    different question of which assets have identical *bytes* — those live in
    separate files and each owns its own.

    A row already soft-deleted does not count as a holder: it is either being
    purged in this same pass or is itself scheduled for one.
    """
    with session() as s:
        a = s.get(Asset, asset_id)
        if a is None or not a.path:
            return False
        other = s.exec(
            select(Asset.id)
            .where(Asset.path == a.path,
                   Asset.id != asset_id,
                   Asset.deleted_at.is_(None))  # type: ignore[union-attr]
            .limit(1)
        ).first()
        return other is not None


def prune_orphan_content() -> int:
    """Drop content rows nothing references. Returns how many went."""
    with session() as s:
        referenced = select(Asset.content_id).where(Asset.content_id.is_not(None))  # type: ignore[union-attr]
        rows = list(s.exec(select(AssetContent).where(AssetContent.id.not_in(referenced))))  # type: ignore[union-attr]
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


def assets_needing_hash(limit: int = 200) -> list[int]:
    """Live assets with no content row yet, oldest first.

    Rows created before content addressing existed. The enrichment worker drains
    this in the background so the gallery stays usable throughout rather than
    blocking a migration on hashing every video in the library.
    """
    with session() as s:
        rows = s.exec(
            select(Asset.id)
            .where(Asset.content_id.is_(None),  # type: ignore[union-attr]
                   Asset.deleted_at.is_(None),  # type: ignore[union-attr]
                   Asset.is_missing == False)  # noqa: E712 — SQL needs the comparison
            .order_by(Asset.id)  # type: ignore[arg-type]
            .limit(limit)
        )
        return [int(i) for i in rows if i is not None]


def attach_content(asset_id: int, hash_: str, *, size_bytes: int = 0,
                   mime_type: str = "", mtime_ns: int | None = None) -> None:
    content = get_or_create_content(hash_, size_bytes=size_bytes, mime_type=mime_type)
    with session() as s:
        a = s.get(Asset, asset_id)
        if a is None:
            return
        a.content_id = content.id
        if mtime_ns is not None:
            a.mtime_ns = mtime_ns
        s.add(a)
        s.commit()


def mark_missing(asset_id: int, missing: bool = True) -> None:
    """Record that an asset's file is gone (or has come back).

    The mirror of sweep_orphan_files(), which handles files with no row. Together
    they mean a desynchronised library is something the UI can show and offer to
    fix, rather than something that raises when a tile is opened.
    """
    with session() as s:
        a = s.get(Asset, asset_id)
        if a is None or bool(a.is_missing) == missing:
            return
        a.is_missing = missing
        s.add(a)
        s.commit()


# ---- semantic search ------------------------------------------------------
def search_by_vector(query: bytes, *, limit: int = 60,
                     kind: str | None = None) -> list[tuple[int, float]]:
    """(asset_id, similarity) for the closest embeddings, best first.

    Brute force, deliberately. Vectors are stored normalised, so similarity is a
    dot product, and a personal gallery of tens of thousands of images is one
    numpy matmul over a few megabytes — well under a frame. A vector index would
    add a dependency, a build step and a staleness problem to solve a scaling
    problem this application does not have.
    """
    import numpy as np

    q = np.frombuffer(query, dtype=np.float32)
    if q.size == 0:
        return []
    with session() as s:
        stmt = select(Asset.id, Asset.embedding).where(
            Asset.embedding.is_not(None),  # type: ignore[union-attr]
            Asset.deleted_at.is_(None),  # type: ignore[union-attr]
        )
        if kind:
            stmt = stmt.where(Asset.kind == kind)
        rows = [(int(i), b) for i, b in s.exec(stmt) if i is not None and b]
    if not rows:
        return []
    ids = [i for i, _ in rows]
    try:
        matrix = np.stack([np.frombuffer(b, dtype=np.float32) for _, b in rows])
    except ValueError:
        # A stored vector of the wrong length (a model change mid-library) would
        # break the stack. Fall back to filtering rather than failing the search.
        good = [(i, np.frombuffer(b, dtype=np.float32)) for i, b in rows]
        good = [(i, v) for i, v in good if v.size == q.size]
        if not good:
            return []
        ids = [i for i, _ in good]
        matrix = np.stack([v for _, v in good])
    if matrix.shape[1] != q.size:
        return []
    scores = matrix @ q
    order = np.argsort(-scores)[:limit]
    return [(ids[int(i)], float(scores[int(i)])) for i in order]


def mark_used(asset_id: int) -> None:
    """Record that an asset was actually used — exported, remixed, fed back in.

    Distinct from `favorite`, which is intent. This is behaviour, and the two
    disagree often enough to be worth keeping apart: the image you starred is
    not always the one you ended up using.
    """
    with session() as s:
        a = s.get(Asset, asset_id)
        if a is None:
            return
        a.used_count = int(a.used_count or 0) + 1
        a.last_used_at = datetime.now(UTC)
        s.add(a)
        s.commit()
