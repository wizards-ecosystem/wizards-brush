"""Reference sets: named, revisioned, ordered collections of gallery assets.

A reference set describes a character, a product, a style or another subject
as asset ids grouped by role, in order, each with an optional mask and note.
It is reference material, not training data and not a promise that a model
preserves identity. The head row carries the name and the latest revision;
every revision is an append-only `referencesetrevision` row, so a plan that
used revision 2 can always read revision 2 after later edits or archiving.

Additive and idempotent. The DDL is written out rather than derived from the
live model, so this migration means the same thing forever.
"""
from __future__ import annotations

import sqlite3

from .ops import add_index

_SETS = """CREATE TABLE IF NOT EXISTS referenceset (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        VARCHAR NOT NULL DEFAULT '',
    revision    INTEGER NOT NULL DEFAULT 1,
    digest      VARCHAR NOT NULL DEFAULT '',
    client      VARCHAR NOT NULL DEFAULT '',
    archived_at DATETIME,
    created_at  DATETIME,
    updated_at  DATETIME
)"""

_REVISIONS = """CREATE TABLE IF NOT EXISTS referencesetrevision (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id          INTEGER NOT NULL REFERENCES referenceset (id),
    revision        INTEGER NOT NULL,
    definition_json VARCHAR NOT NULL DEFAULT '{}',
    digest          VARCHAR NOT NULL DEFAULT '',
    based_on        INTEGER,
    client          VARCHAR NOT NULL DEFAULT '',
    created_at      DATETIME
)"""


def run(cur: sqlite3.Cursor) -> None:
    cur.execute(_SETS)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_referenceset_name_nocase "
                "ON referenceset (name COLLATE NOCASE) WHERE archived_at IS NULL")
    cur.execute(_REVISIONS)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_referencesetrevision_set_revision "
                "ON referencesetrevision (set_id, revision)")
    add_index(cur, "ix_referencesetrevision_set_id", "referencesetrevision", "set_id")
