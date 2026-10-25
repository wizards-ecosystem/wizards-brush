"""Typed export outputs: written aside, validated, published, then served.

An export is built in a private staging directory under the output root, so
the final move into ``output/artifacts`` is one atomic rename on the same
filesystem. Only a published file gets a row, and a row is only ever served
from inside the artifacts directory. A download is an authenticated ``/api``
request like every other, and the file outlives that response: an MCP client
can be handed the link and fetch it later, until the artifact expires.

Artifacts are not gallery assets. They hold JSON, ZIP and sheet files a set
produced, never appear in search or stats, and deleting one never touches the
assets it was made from.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import col, select

from . import db, log
from .config import settings
from .models import Artifact
from .problems import RequestError

logger = log.get("artifacts")

TTL = timedelta(days=7)
STAGING_TTL_S = 24 * 3600


def root() -> Path:
    return settings.output_path / "artifacts"


def _staging_root() -> Path:
    return settings.output_path / ".artifacts-staging"


@contextmanager
def staging() -> Iterator[Path]:
    """A private directory for building one export. Always removed afterwards;
    an abandoned one (a killed process) is swept a day later."""
    base = _staging_root()
    base.mkdir(parents=True, exist_ok=True)
    work = base / secrets.token_hex(8)
    work.mkdir()
    try:
        yield work
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def publish(built: Path, *, kind: str, filename: str, mime_type: str, source: str,
            meta: dict[str, Any] | None = None, client: str = "") -> Artifact:
    """Move a validated file into managed storage and record it."""
    token = secrets.token_urlsafe(18)
    home = root() / token
    home.mkdir(parents=True, exist_ok=False)
    final = home / filename
    os.replace(built, final)
    now = datetime.now(UTC)
    row = Artifact(token=token, kind=kind, filename=filename, mime_type=mime_type,
                   size_bytes=final.stat().st_size, sha256=_sha256(final), path=str(final),
                   source=source, meta_json=json.dumps(meta or {}), client=client,
                   created_at=now, expires_at=now + TTL)
    with db.session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        s.expunge(row)
    logger.info("published %s artifact %s (%d bytes)", kind, row.id, row.size_bytes)
    return row


def get(artifact_id: int) -> Artifact:
    with db.session() as s:
        row = s.get(Artifact, artifact_id)
        if row is not None:
            s.expunge(row)
    if row is None or not _servable(row):
        raise RequestError(404, f"artifact {artifact_id} does not exist or has expired",
                           code="not_found", field="artifact_id")
    return row


def _servable(row: Artifact) -> bool:
    path = Path(row.path)
    try:
        path.resolve().relative_to(root().resolve())
    except ValueError:
        return False
    expired = row.expires_at is not None and row.expires_at.replace(tzinfo=UTC) < datetime.now(UTC)
    return path.is_file() and not expired


def view(row: Artifact) -> dict[str, Any]:
    return {"id": row.id, "kind": row.kind, "filename": row.filename,
            "mime_type": row.mime_type, "size_bytes": row.size_bytes, "sha256": row.sha256,
            "source": row.source, "meta": row.meta, "created_at": row.created_at,
            "expires_at": row.expires_at, "file_url": f"/api/artifacts/{row.id}/file"}


def delete(artifact_id: int) -> bool:
    with db.session() as s:
        row = s.get(Artifact, artifact_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
    shutil.rmtree(Path(row.path).parent, ignore_errors=True)
    return True


def sweep() -> int:
    """Remove expired artifacts and abandoned staging directories."""
    now = datetime.now(UTC)
    with db.session() as s:
        expired = list(s.exec(select(Artifact).where(col(Artifact.expires_at) < now)))
        for row in expired:
            shutil.rmtree(Path(row.path).parent, ignore_errors=True)
            s.delete(row)
        s.commit()
    staging_root = _staging_root()
    if staging_root.exists():
        cutoff = time.time() - STAGING_TTL_S
        for work in staging_root.iterdir():
            try:
                if work.stat().st_mtime < cutoff:
                    shutil.rmtree(work, ignore_errors=True)
            except OSError:
                continue
    return len(expired)
