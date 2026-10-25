"""Canvas projects: layered documents over gallery assets.

A canvas project is a document of ordered layers, each pointing at a gallery
asset (and optionally a mask asset) with a transform, opacity, visibility and
blend mode. Assets are immutable sources: the document stores references and
transforms only, so editing never touches a gallery file and deleting a
project never deletes one. The head row carries the name and latest revision;
every save is an append-only `canvasrevision` row behind a base-revision
compare-and-set.

Additive and idempotent. The DDL is written out rather than derived from the
live model, so this migration means the same thing forever.
"""
from __future__ import annotations

import sqlite3

from .ops import add_index

_PROJECTS = """CREATE TABLE IF NOT EXISTS canvasproject (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        VARCHAR NOT NULL DEFAULT '',
    revision    INTEGER NOT NULL DEFAULT 1,
    digest      VARCHAR NOT NULL DEFAULT '',
    width       INTEGER NOT NULL DEFAULT 0,
    height      INTEGER NOT NULL DEFAULT 0,
    client      VARCHAR NOT NULL DEFAULT '',
    archived_at DATETIME,
    created_at  DATETIME,
    updated_at  DATETIME
)"""

_REVISIONS = """CREATE TABLE IF NOT EXISTS canvasrevision (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES canvasproject (id),
    revision      INTEGER NOT NULL,
    document_json VARCHAR NOT NULL DEFAULT '{}',
    digest        VARCHAR NOT NULL DEFAULT '',
    based_on      INTEGER,
    client        VARCHAR NOT NULL DEFAULT '',
    created_at    DATETIME
)"""


def run(cur: sqlite3.Cursor) -> None:
    cur.execute(_PROJECTS)
    cur.execute(_REVISIONS)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_canvasrevision_project_revision "
                "ON canvasrevision (project_id, revision)")
    add_index(cur, "ix_canvasrevision_project_id", "canvasrevision", "project_id")
