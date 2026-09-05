"""Ordered schema migrations.

Each migration is a module exposing ``run(cursor)``. They execute in the order
listed in :data:`MIGRATIONS`, once each, and are recorded by id in the
``applied_migrations`` table.

Four rules, and every one of them exists because of a specific failure mode:

* **State is which ids ran, not a version number.** A migration added later can
  never collide with one someone else numbered the same, and there is no
  renumbering when the order of authorship differs from the order of merge.
* **Each migration owns a transaction, and the runner owns the commit.** The
  sqlite3 connection context manager commits on clean exit and rolls back on any
  exception, so a migration that dies halfway leaves the database exactly as it
  was. ``run()`` must therefore never commit.
* **An unknown applied id is fatal.** If the database records a migration this
  build has never heard of, it was written by a newer build; continuing would
  silently operate on a schema we do not understand. Refuse to start instead.
* **A backup is taken before the first unapplied migration.** Once, not per
  migration, and never when there is nothing to do.

Migrations are idempotent wherever it is cheap to make them so. That is what
lets the baseline migration adopt a database created by the old additive
``_migrate()`` without any bootstrap detection: re-running it is a no-op.
"""
from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .. import log
from . import (
    m0001_baseline,
    m0002_job_model_key,
    m0003_asset_content,
    m0004_asset_signals,
    m0005_asset_human_grade,
    m0006_job_remote_identity,
    m0007_job_request_id,
)

logger = log.get("migrate")

Migration = Callable[[sqlite3.Cursor], None]

# Append only. Never reorder, never edit one that has shipped — write a new one.
MIGRATIONS: list[tuple[str, Migration]] = [
    ("0001_baseline", m0001_baseline.run),
    ("0002_job_model_key", m0002_job_model_key.run),
    ("0003_asset_content", m0003_asset_content.run),
    ("0004_asset_signals", m0004_asset_signals.run),
    ("0005_asset_human_grade", m0005_asset_human_grade.run),
    ("0006_job_remote_identity", m0006_job_remote_identity.run),
    ("0007_job_request_id", m0007_job_request_id.run),
]


class MigrationError(RuntimeError):
    """A migration failed, or the database is in a state we refuse to touch."""


def _ensure_table(cur: sqlite3.Cursor) -> None:
    cur.execute(
        """CREATE TABLE IF NOT EXISTS applied_migrations (
               id          TEXT PRIMARY KEY,
               applied_at  TIMESTAMP NOT NULL DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f','NOW'))
           )"""
    )


def _applied(cur: sqlite3.Cursor) -> set[str]:
    try:
        cur.execute("SELECT id FROM applied_migrations")
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return set()
        raise
    return {row[0] for row in cur.fetchall()}


def backup(db_path: Path) -> Path | None:
    """Timestamped copy of the database beside it. None for an in-memory DB.

    Uses sqlite's own backup API rather than a file copy: it is safe against a
    concurrent writer, which a copy in WAL mode is not.
    """
    if not db_path or not db_path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = db_path.parent / f"{db_path.stem}_backup_{stamp}{db_path.suffix}"
    with closing(sqlite3.connect(db_path)) as src, closing(sqlite3.connect(dest)) as dst:
        src.backup(dst)
    return dest


def run_all(conn: sqlite3.Connection, db_path: Path | None = None) -> list[str]:
    """Apply every unapplied migration in order. Returns the ids that ran.

    `conn` must be a raw sqlite3 connection — the runner drives transactions
    itself and cannot do that through the ORM's session handling.

    **On transaction control.** Python's sqlite3 module, in its default legacy
    mode, only opens a transaction implicitly before DML (INSERT/UPDATE/DELETE).
    DDL runs in autocommit, so an `ALTER TABLE` followed by a failure would be
    left half-applied with nothing to roll back. SQLite itself is perfectly
    happy to run DDL inside a transaction; it is the driver that opts out. So we
    take isolation control ourselves and issue BEGIN/COMMIT/ROLLBACK explicitly,
    which is what actually makes "each migration is atomic" true.
    """
    known = {mid for mid, _ in MIGRATIONS}
    if len(known) != len(MIGRATIONS):
        raise MigrationError("duplicate migration id in MIGRATIONS")

    prior_isolation = conn.isolation_level
    conn.isolation_level = None  # explicit transactions; see docstring
    try:
        cur = conn.cursor()
        _ensure_table(cur)

        applied = _applied(cur)
        unknown = applied - known
        if unknown:
            raise MigrationError(
                f"database records migrations this build does not know: {sorted(unknown)}. "
                "It was created by a newer version — upgrade rather than downgrade."
            )

        pending = [(mid, fn) for mid, fn in MIGRATIONS if mid not in applied]
        if not pending:
            return []

        if db_path is not None:
            dest = backup(db_path)
            if dest:
                logger.info("backed up database to %s before %d migration(s)",
                            dest.name, len(pending))

        ran: list[str] = []
        for mid, fn in pending:
            cur = conn.cursor()
            cur.execute("BEGIN")
            try:
                fn(cur)
                cur.execute("INSERT INTO applied_migrations (id) VALUES (?)", (mid,))
                cur.execute("COMMIT")
            except Exception as e:
                with contextlib.suppress(sqlite3.Error):
                    cur.execute("ROLLBACK")
                raise MigrationError(f"migration {mid} failed: {e}") from e
            logger.info("applied migration %s", mid)
            ran.append(mid)
        return ran
    finally:
        conn.isolation_level = prior_isolation
