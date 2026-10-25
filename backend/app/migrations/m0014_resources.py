"""Model and adapter resources: identities and where each is installed.

A `resource` row is an immutable identity (provider identity plus an exact
version, or a content hash for a local import) with what is known about it:
family, format, file inventory, licence and compatibility observations. A
`resourceinstall` row is one copy of it on one target. Storage location is
never the identity, so two copies of the same LoRA are one resource with two
installs, and a moved file is a changed install, not a different adapter.

Nothing is hashed or downloaded here. Existing LoRA files and configured
models are registered by the app afterwards, as `legacy_unverified`, and
verified in bounded background work.
"""
from __future__ import annotations

import sqlite3

from .ops import add_index

_RESOURCE = """CREATE TABLE IF NOT EXISTS resource (
    id              VARCHAR PRIMARY KEY,
    kind            VARCHAR NOT NULL DEFAULT 'model',
    family          VARCHAR NOT NULL DEFAULT 'unknown',
    provider        VARCHAR NOT NULL DEFAULT '',
    provider_ref    VARCHAR NOT NULL DEFAULT '',
    revision        VARCHAR NOT NULL DEFAULT '',
    content_hash    VARCHAR NOT NULL DEFAULT '',
    format          VARCHAR NOT NULL DEFAULT '',
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    label           VARCHAR NOT NULL DEFAULT '',
    files_json      VARCHAR NOT NULL DEFAULT '[]',
    license_json    VARCHAR NOT NULL DEFAULT '{}',
    access          VARCHAR NOT NULL DEFAULT 'unknown',
    state           VARCHAR NOT NULL DEFAULT 'known',
    verification    VARCHAR NOT NULL DEFAULT 'unverified',
    compat_json     VARCHAR NOT NULL DEFAULT '{}',
    constraints_json VARCHAR NOT NULL DEFAULT '{}',
    error           VARCHAR NOT NULL DEFAULT '',
    created_at      DATETIME,
    updated_at      DATETIME
)"""

_INSTALL = """CREATE TABLE IF NOT EXISTS resourceinstall (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id     VARCHAR NOT NULL,
    target          VARCHAR NOT NULL DEFAULT 'local',
    location        VARCHAR NOT NULL DEFAULT '',
    state           VARCHAR NOT NULL DEFAULT 'installed',
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    mtime_ns        INTEGER,
    verified_hash   VARCHAR NOT NULL DEFAULT '',
    verified_at     DATETIME,
    error           VARCHAR NOT NULL DEFAULT '',
    created_at      DATETIME,
    updated_at      DATETIME
)"""


def run(cur: sqlite3.Cursor) -> None:
    cur.execute(_RESOURCE)
    cur.execute(_INSTALL)
    add_index(cur, "ix_resource_kind", "resource", "kind")
    add_index(cur, "ix_resource_content_hash", "resource", "content_hash")
    add_index(cur, "ix_resourceinstall_resource_id", "resourceinstall", "resource_id")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_resourceinstall_target_location "
                "ON resourceinstall (target, location)")
