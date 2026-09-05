"""Idempotent schema operations shared by migration modules.

Separate from the package __init__ so migrations can import them without a
circular import: __init__ imports every migration module to build the ordered
list, and each migration imports these.

Every operation here is safe to run twice. That is what lets a migration adopt a
database whose schema partly predates the migration ledger.
"""
from __future__ import annotations

import sqlite3


def columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    """Column names of `table`, or an empty set if it does not exist."""
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def add_column(cur: sqlite3.Cursor, table: str, name: str, ddl: str) -> bool:
    """Add a column if the table exists and the column does not. True if added.

    A missing table is not an error: SQLModel.create_all() runs first and builds
    it with the column already present, so there is nothing to alter.
    """
    existing = columns(cur, table)
    if not existing or name in existing:
        return False
    cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    return True


def add_index(cur: sqlite3.Cursor, name: str, table: str, cols: str) -> None:
    if not table_exists(cur, table):
        return
    cur.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})")


def add_unique_index(cur: sqlite3.Cursor, name: str, table: str, cols: str) -> None:
    if not table_exists(cur, table):
        return
    cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} ({cols})")
