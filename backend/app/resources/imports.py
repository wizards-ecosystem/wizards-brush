"""Provider import tasks: explicit, durable, off the GPU lane.

`create` binds an exact choice: it inspects the link again, requires a
concrete selection when the link is ambiguous, refuses unusable files, gated
access without a token, an unknown licence without an explicit
acknowledgement, and a download the disk cannot hold. Then one row is written
and a small I/O worker (never the serialized GPU lane) downloads it.

A single file streams into the kind's quarantine directory, resumes with a
ranged request when the provider supports it, is checked against the sha256
the provider published, and then goes through the same intake as a local
upload: structural validation, the adapter-versus-checkpoint check, content
deduplication and atomic promotion. A known-good resource is never
overwritten. A Diffusers repository downloads file by file at the inspected
commit into the managed Hugging Face cache, each file checked against its
LFS hash.

States: queued, running, canceling, canceled, failed, interrupted, done. A
task that was running when the process died becomes `interrupted` at startup
and waits for an explicit retry; nothing is marked done that did not finish.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from sqlmodel import col, desc, select

from .. import db, log
from ..config import settings
from ..models import ResourceImport
from ..problems import RequestError
from . import catalogue, intake, providers, store

logger = log.get("imports")

MAX_SECONDS = 6 * 3600
DISK_MARGIN = intake.DISK_MARGIN
_PROGRESS_EVERY = 0.5
_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
_CANCEL: set[int] = set()
ACTIVE = frozenset({"queued", "running", "canceling"})
RETRYABLE = frozenset({"failed", "interrupted", "canceled"})
# Replaced in tests: the per-file Hub download used for Diffusers repositories.
hf_hub_download: Any = None


def _now() -> datetime:
    return datetime.now(UTC)


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            # Two at a time: downloads are network-bound, and the GPU lane is
            # never involved.
            _EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="resource-import")
        return _EXECUTOR


# ---- rows ------------------------------------------------------------------------------
def _get(task_id: int) -> ResourceImport | None:
    with db.session() as s:
        row = s.get(ResourceImport, task_id)
        if row is not None:
            s.expunge(row)
        return row


def _update(task_id: int, **fields: Any) -> None:
    with db.session() as s:
        row = s.get(ResourceImport, task_id)
        if row is None:
            return
        for key, value in fields.items():
            setattr(row, key, value)
        row.updated_at = _now()
        s.add(row)
        s.commit()


def view(row: ResourceImport) -> dict[str, Any]:
    return {
        "id": row.id, "provider": row.provider, "provider_ref": row.provider_ref,
        "revision": row.revision, "source_url": row.source_url, "selection": row.selection,
        "kind": row.kind, "filename": row.filename, "expected_size": row.expected_size,
        "expected_sha256": row.expected_sha256, "state": row.state, "progress": row.progress,
        "bytes_done": row.bytes_done, "bytes_total": row.bytes_total,
        "error": providers.redact(row.error), "error_code": row.error_code,
        "resource_id": row.resource_id, "license": row.license,
        "license_acknowledged": row.license_acknowledged, "client": row.client,
        "retryable": row.state in RETRYABLE, "created_at": row.created_at,
        "updated_at": row.updated_at, "finished_at": row.finished_at,
    }


def get(task_id: int) -> dict[str, Any]:
    row = _get(task_id)
    if row is None:
        raise RequestError(404, "no such import", code="not_found", field="import_id")
    return view(row)


def listing(limit: int = 100) -> list[dict[str, Any]]:
    with db.session() as s:
        rows = list(s.exec(select(ResourceImport).order_by(desc(col(ResourceImport.id)))
                           .limit(limit)))
        for row in rows:
            s.expunge(row)
    return [view(r) for r in rows]


# ---- creating --------------------------------------------------------------------------
def _choose(info: dict[str, Any], selection: str | None) -> dict[str, Any]:
    candidates = info["candidates"]
    # A link that names exactly one file is that file, usable or not: its
    # refusal is a better answer than "choose one".
    only = candidates[0]["id"] if len(candidates) == 1 else None
    wanted = selection or info.get("default_selection") or only
    if not wanted:
        raise RequestError(
            400, "this link offers several files or versions; choose one",
            code="selection_required", field="selection",
            valid_options=[c["id"] for c in candidates if c["supported"]],
            state={"candidates": candidates},
        )
    chosen = next((c for c in candidates if c["id"] == wanted), None)
    if chosen is None:
        raise RequestError(400, f"{wanted!r} is not one of this link's candidates",
                           code="invalid_option", field="selection",
                           valid_options=[c["id"] for c in candidates])
    if not chosen["supported"]:
        code = ("unsafe_format" if "pickle" in chosen["reason"] else "unsupported_format")
        raise RequestError(400, chosen["reason"] or "this file cannot be imported", code=code,
                           field="selection")
    return chosen


def create(link: str, *, selection: str | None = None, kind: str | None = None,
           acknowledge_license: bool = False, client: str = "") -> dict[str, Any]:
    """Bind an exact provider file (or repository) and queue its download."""
    info = providers.inspect(link)
    chosen = _choose(info, selection)
    task_kind = chosen.get("kind") or kind
    if chosen["type"] == "file":
        if task_kind not in intake.KINDS:
            raise RequestError(400, "say whether this file is an adapter or a model",
                               code="missing_input", field="kind",
                               valid_options=list(intake.KINDS))
        if chosen.get("kind") and kind and kind != chosen["kind"]:
            raise RequestError(400, f"the provider lists this as a {chosen['kind']}",
                               code="invalid_request", field="kind")
    else:
        task_kind = "diffusers"
    if info["access"] in {"gated", "private"} and not (settings.hf_token or "").strip():
        raise RequestError(
            403, f"{info['provider_ref']} is {info['access']}: accept its terms on Hugging Face "
                 "and set HF_TOKEN in .env, then import again",
            code="access_required", field="link")
    license = dict(info["license"])
    if info["license_needs_acknowledgement"]:
        if not acknowledge_license:
            raise RequestError(
                409, "the licence of this resource is unknown; review it on the provider's page "
                     "and import again with acknowledge_license",
                code="license_unreviewed", field="acknowledge_license", retryable=False,
                state={"license": license})
        license.update(acknowledged_at=_now().isoformat(), acknowledged_by=client or "")
    size = int(chosen.get("size") or 0) or None
    if chosen["type"] == "file" and size and size > intake.MAX_BYTES[str(task_kind)]:
        raise RequestError(400, f"the file is larger than {intake.MAX_BYTES[str(task_kind)] >> 30} GB",
                           code="too_large", field="selection")
    _check_disk(str(task_kind), size or 0)
    sha = str(chosen.get("sha256") or "").lower()
    existing = list(store.by_hash(f"sha256:{sha}")) if sha else []
    present = next((r for r in existing if catalogue.local_file(r.id) is not None), None)
    row = ResourceImport(
        provider=info["provider"], provider_ref=info["provider_ref"],
        revision=str(chosen.get("version_id") or info["revision"]),
        source_url=providers.public_url(info["url"]),
        selection_json=json.dumps({"id": chosen["id"], "candidate": _stored(chosen),
                                   "access": info["access"], "family": chosen.get("family")
                                   or info.get("family") or ""}),
        kind=str(task_kind), filename=str(chosen.get("path") or chosen.get("name") or ""),
        expected_size=size, expected_sha256=sha, license_json=json.dumps(license),
        license_acknowledged=bool(acknowledge_license), client=client,
        bytes_total=size, created_at=_now(), updated_at=_now(),
    )
    if present is not None:
        # Known-good content is already installed: nothing to fetch or replace.
        row.state, row.progress, row.resource_id = "done", 1.0, present.id
        row.error_code, row.finished_at = "already_present", _now()
    with db.session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        s.expunge(row)
    if row.state == "queued":
        _executor().submit(_run, int(row.id or 0))
    logger.info("import %s queued: %s %s", row.id, row.provider, row.filename or row.provider_ref)
    return view(row)


def _stored(chosen: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "type", "path", "name", "size", "sha256", "version_id", "file_id",
            "download_url", "files", "base_model")
    return {k: chosen[k] for k in keep if k in chosen}


def _check_disk(kind: str, size: int) -> None:
    root = settings.hf_hub_path if kind == "diffusers" else intake.quarantine_dir(kind)
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free
    if free < size + DISK_MARGIN:
        raise RequestError(
            507, f"not enough disk space: {size >> 20} MB plus a {DISK_MARGIN >> 30} GB margin is "
                 f"needed and {free >> 20} MB is free",
            code="insufficient_disk", field="selection", retryable=True)


# ---- controlling -----------------------------------------------------------------------
def cancel(task_id: int) -> dict[str, Any]:
    row = _get(task_id)
    if row is None:
        raise RequestError(404, "no such import", code="not_found", field="import_id")
    if row.state == "queued":
        _CANCEL.add(task_id)
        _update(task_id, state="canceled", finished_at=_now())
    elif row.state == "running":
        _CANCEL.add(task_id)
        _update(task_id, state="canceling")
    return get(task_id)


def retry(task_id: int) -> dict[str, Any]:
    row = _get(task_id)
    if row is None:
        raise RequestError(404, "no such import", code="not_found", field="import_id")
    if row.state not in RETRYABLE:
        raise RequestError(409, f"an import that is {row.state} cannot be retried",
                           code="conflict", field="import_id")
    _check_disk(row.kind, max(0, int(row.expected_size or 0) - int(row.bytes_done or 0)))
    _CANCEL.discard(task_id)
    _update(task_id, state="queued", error="", error_code="", finished_at=None)
    _executor().submit(_run, task_id)
    return get(task_id)


def recover_on_startup() -> int:
    """Tasks the previous process left mid-flight wait for an explicit retry."""
    changed = 0
    with db.session() as s:
        for row in s.exec(select(ResourceImport).where(col(ResourceImport.state).in_(list(ACTIVE)))):
            row.state = "canceled" if row.state == "canceling" else "interrupted"
            row.error = row.error or "the app stopped before this import finished"
            row.updated_at = _now()
            s.add(row)
            changed += 1
        s.commit()
    return changed


# ---- running ---------------------------------------------------------------------------
def _part(row: ResourceImport):
    return intake.quarantine_dir(row.kind) / f"import-{row.id}.part"


def _run(task_id: int) -> None:
    row = _get(task_id)
    if row is None or row.state != "queued" or task_id in _CANCEL:
        return
    _update(task_id, state="running")
    try:
        if row.kind == "diffusers":
            resource = _run_repo(row)
        else:
            resource = _run_file(row)
    except providers.Canceled:
        if row.kind != "diffusers":
            _part(row).unlink(missing_ok=True)
        _update(task_id, state="canceled", finished_at=_now())
        logger.info("import %s canceled", task_id)
        return
    except RequestError as error:
        retryable = bool(error.retryable)
        if not retryable and row.kind != "diffusers":
            _part(row).unlink(missing_ok=True)
        _update(task_id, state="failed", error=providers.redact(str(error.detail)),
                error_code=error.code, finished_at=_now())
        logger.warning("import %s failed (%s): %s", task_id, error.code,
                       providers.redact(str(error.detail)))
        return
    except Exception as error:  # noqa: BLE001 — a task records its failure, never a crash
        _update(task_id, state="failed", error=providers.redact(str(error)),
                error_code="import_failed", finished_at=_now())
        logger.warning("import %s failed: %s", task_id, providers.redact(str(error)))
        return
    finally:
        _CANCEL.discard(task_id)
    _update(task_id, state="done", progress=1.0, resource_id=resource["id"],
            error_code=resource.get("status_code", ""), finished_at=_now())
    logger.info("import %s done: %s", task_id, resource["id"])


def _progress(task_id: int, total: int | None):
    last = [0.0]

    def report(done: int) -> None:
        now = time.monotonic()
        if now - last[0] < _PROGRESS_EVERY:
            return
        last[0] = now
        _update(task_id, bytes_done=done, progress=min(0.99, done / total) if total else 0.0)

    return report


def _file_url(row: ResourceImport, candidate: dict[str, Any]) -> str:
    if row.provider == "hf":
        return (f"https://{providers.HF_HOST}/{row.provider_ref}/resolve/{row.revision}/"
                f"{candidate['path']}")
    url = str(candidate.get("download_url") or "")
    if not url:
        raise RequestError(400, "the provider lists no download for this file",
                           code="unsupported_format", field="selection")
    return url


def _run_file(row: ResourceImport) -> dict[str, Any]:
    task_id = int(row.id or 0)
    candidate = row.selection.get("candidate") or {}
    part = _part(row)
    offset = part.stat().st_size if part.exists() else 0
    limit = min(int(row.expected_size or intake.MAX_BYTES[row.kind]), intake.MAX_BYTES[row.kind])
    providers.download(
        _file_url(row, candidate), part, offset=offset, limit=limit,
        deadline=time.monotonic() + MAX_SECONDS, cancel=lambda: task_id in _CANCEL,
        progress=_progress(task_id, row.expected_size),
    )
    if row.expected_size and part.stat().st_size != row.expected_size:
        part.unlink(missing_ok=True)
        raise RequestError(502, f"received {part.stat().st_size if part.exists() else 0} of "
                                f"{row.expected_size} bytes", code="size_mismatch",
                           field="selection", retryable=True)
    digest = catalogue.sha256_file(part)
    if digest is None:
        raise RequestError(500, "the download could not be read back", code="import_failed")
    if row.expected_sha256 and digest != f"sha256:{row.expected_sha256}":
        part.unlink(missing_ok=True)
        raise RequestError(400, "the file does not match the sha256 the provider published; it "
                                "was discarded", code="hash_mismatch", field="selection")
    name = intake.safe_filename(str(candidate.get("name") or row.filename).rsplit("/", 1)[-1])
    result = intake.import_file(part, kind=row.kind, original_name=name, digest=digest)
    part.unlink(missing_ok=True)
    resource = result["resource"]
    catalogue.annotate_source(
        resource["id"], provider=row.provider,
        provider_ref=(f"{row.provider_ref}/version/{candidate.get('version_id')}/file/"
                      f"{candidate.get('file_id')}" if row.provider == "civitai"
                      else f"{row.provider_ref}/{candidate.get('path')}"),
        revision=row.revision, license=row.license, access=row.selection.get("access") or "",
        source_url=row.source_url, family=row.selection.get("family") or None)
    return {"id": resource["id"],
            "status_code": "already_present" if result["status"] == "already_present" else ""}


def _run_repo(row: ResourceImport) -> dict[str, Any]:
    task_id = int(row.id or 0)
    candidate = row.selection.get("candidate") or {}
    files = list(candidate.get("files") or [])
    download = hf_hub_download
    if download is None:
        from huggingface_hub import hf_hub_download as download  # lazy: optional at runtime
    total = sum(int(f.get("size") or 0) for f in files) or None
    done = 0
    verified = True
    for entry in files:
        if task_id in _CANCEL:
            raise providers.Canceled()
        path = download(row.provider_ref, entry["path"], revision=row.revision,
                        cache_dir=str(settings.hf_hub_path),
                        token=(settings.hf_token or None))
        expected = str(entry.get("sha256") or "")
        if expected:
            from pathlib import Path

            digest = catalogue.sha256_file(Path(path))
            if digest != f"sha256:{expected}":
                raise RequestError(400, f"{entry['path']} does not match its published sha256",
                                   code="hash_mismatch", field="selection")
        elif entry["path"].lower().endswith(".safetensors"):
            verified = False   # a weight without a published hash cannot be verified
        done += int(entry.get("size") or 0)
        _update(task_id, bytes_done=done, progress=min(0.99, done / total) if total else 0.0)
    resource = catalogue.register_hub_model(row.provider_ref, row.revision, files,
                                            license=row.license,
                                            access=row.selection.get("access") or "",
                                            verified=verified)
    return {"id": resource.id}
