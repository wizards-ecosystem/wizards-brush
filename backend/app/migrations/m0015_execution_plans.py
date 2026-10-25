"""Execution plans: the immutable effective request behind a preset job.

A plan row is written once, before its job, and never changed: it is what a
preview showed and what a replay reproduces. A job made from one records the
plan id, the digest of the request that produced it (so a retried submission
with the same request id resolves to the same job, and a different request
under that id is a conflict), the target that runs it, and, once it starts,
a snapshot of the runtime that actually executed it.

Every new column on `job` is nullable or has an empty default, so historical
jobs keep their kind-based meaning untouched.
"""
from __future__ import annotations

import sqlite3

from .ops import add_column, add_index

_PLAN = """CREATE TABLE IF NOT EXISTS executionplan (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id         VARCHAR,
    request_digest     VARCHAR NOT NULL DEFAULT '',
    creative_hash      VARCHAR NOT NULL DEFAULT '',
    execution_hash     VARCHAR NOT NULL DEFAULT '',
    material_hash      VARCHAR NOT NULL DEFAULT '',
    schema_version     INTEGER NOT NULL DEFAULT 1,
    resolver_version   VARCHAR NOT NULL DEFAULT '',
    preset_id          VARCHAR,
    preset_revision    INTEGER,
    kind               VARCHAR NOT NULL DEFAULT '',
    target             VARCHAR NOT NULL DEFAULT '',
    plan_json          VARCHAR NOT NULL DEFAULT '{}',
    request_json       VARCHAR NOT NULL DEFAULT '{}',
    replay_of          INTEGER,
    created_at         DATETIME
)"""


def run(cur: sqlite3.Cursor) -> None:
    cur.execute(_PLAN)
    add_index(cur, "ix_executionplan_creative_hash", "executionplan", "creative_hash")
    add_index(cur, "ix_executionplan_preset_id", "executionplan", "preset_id")
    add_column(cur, "job", "plan_id", "INTEGER")
    add_column(cur, "job", "request_digest", "VARCHAR")
    add_column(cur, "job", "backend_id", "VARCHAR")
    add_column(cur, "job", "execution_json", "VARCHAR NOT NULL DEFAULT '{}'")
    add_index(cur, "ix_job_plan_id", "job", "plan_id")
