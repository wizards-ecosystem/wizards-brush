"""Artifacts: typed export outputs (sprite sheets, atlases, archives).

An export used to exist only as a temporary file behind one HTTP response, so
a link handed to an MCP client pointed at a ZIP that was deleted after its
first download. An artifact is written to managed storage, validated, then
published under a row that carries its MIME type, size, SHA-256, where it came
from and when it may be removed. Artifacts are not gallery assets: they never
appear in search, stats or the remix pickers.

Additive and idempotent. The DDL is written out rather than derived from the
live model, so this migration means the same thing forever.
"""
from __future__ import annotations

import sqlite3

from .ops import add_index

_TABLE = """CREATE TABLE IF NOT EXISTS artifact (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       VARCHAR NOT NULL DEFAULT '',
    kind        VARCHAR NOT NULL DEFAULT '',
    filename    VARCHAR NOT NULL DEFAULT '',
    mime_type   VARCHAR NOT NULL DEFAULT '',
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    sha256      VARCHAR NOT NULL DEFAULT '',
    path        VARCHAR NOT NULL DEFAULT '',
    source      VARCHAR NOT NULL DEFAULT '',
    meta_json   VARCHAR NOT NULL DEFAULT '{}',
    client      VARCHAR NOT NULL DEFAULT '',
    created_at  DATETIME,
    expires_at  DATETIME
)"""


def run(cur: sqlite3.Cursor) -> None:
    cur.execute(_TABLE)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_artifact_token ON artifact (token)")
    add_index(cur, "ix_artifact_expires_at", "artifact", "expires_at")
