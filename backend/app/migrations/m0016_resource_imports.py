"""Provider imports: durable download tasks for Hugging Face and Civitai.

One row per explicit import a person (or an AI client) asked for: the exact
provider identity and file chosen at inspection, the hash the provider
published, progress, and the resource it produced. Credentials are never
stored here; a source URL is kept without query strings.

A task that was running when the process died is not resumed silently and is
never marked done: startup marks it `interrupted`, and a retry resumes its
partial file where the provider supports ranged requests.
"""
from __future__ import annotations

import sqlite3

from .ops import add_index

_TABLE = """CREATE TABLE IF NOT EXISTS resourceimport (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    provider         VARCHAR NOT NULL DEFAULT '',
    provider_ref     VARCHAR NOT NULL DEFAULT '',
    revision         VARCHAR NOT NULL DEFAULT '',
    source_url       VARCHAR NOT NULL DEFAULT '',
    selection_json   VARCHAR NOT NULL DEFAULT '{}',
    kind             VARCHAR NOT NULL DEFAULT '',
    filename         VARCHAR NOT NULL DEFAULT '',
    expected_size    INTEGER,
    expected_sha256  VARCHAR NOT NULL DEFAULT '',
    state            VARCHAR NOT NULL DEFAULT 'queued',
    progress         FLOAT NOT NULL DEFAULT 0,
    bytes_done       INTEGER NOT NULL DEFAULT 0,
    bytes_total      INTEGER,
    error            VARCHAR NOT NULL DEFAULT '',
    error_code       VARCHAR NOT NULL DEFAULT '',
    resource_id      VARCHAR,
    license_json     VARCHAR NOT NULL DEFAULT '{}',
    license_acknowledged BOOLEAN NOT NULL DEFAULT 0,
    client           VARCHAR NOT NULL DEFAULT '',
    created_at       DATETIME,
    updated_at       DATETIME,
    finished_at      DATETIME
)"""


def run(cur: sqlite3.Cursor) -> None:
    cur.execute(_TABLE)
    add_index(cur, "ix_resourceimport_state", "resourceimport", "state")
