"""Variant recipes, variant sets, and their per-combination items.

Three new tables and no change to any existing one. Child work is ordinary
`job` rows of existing kinds, grouped by the existing `job.group_id`; what a
combination *is* (its axis values, its state while it waits on a parent, its
validation) lives on `variantitem`, so `JobStatus` keeps its five values.

The DDL is a frozen snapshot written out here rather than derived from the live
SQLModel classes. A migration must mean the same thing forever: generating it
from the model would silently change what this id does the day a column is
added, and that column belongs in a migration of its own.

In a normal start `SQLModel.create_all()` has already built these tables, so the
CREATE statements are no-ops and only the indexes are checked. Everything here
is idempotent, which is what lets the runner adopt either shape.

Ids are AUTOINCREMENT, never reused: deleting a set keeps its jobs and assets,
whose provenance names the set and item ids, so a recycled id would silently
re-point that provenance at a different run.
"""
from __future__ import annotations

import sqlite3

from .ops import add_index, add_unique_index

_TABLES = (
    """CREATE TABLE IF NOT EXISTS variantrecipe (
           id           INTEGER PRIMARY KEY AUTOINCREMENT,
           name         VARCHAR NOT NULL DEFAULT '',
           description  VARCHAR NOT NULL DEFAULT '',
           config_json  VARCHAR NOT NULL DEFAULT '{}',
           created_at   DATETIME,
           updated_at   DATETIME
       )""",
    """CREATE TABLE IF NOT EXISTS variantset (
           id                     INTEGER PRIMARY KEY AUTOINCREMENT,
           name                   VARCHAR NOT NULL DEFAULT '',
           recipe_id              INTEGER,
           recipe_json            VARCHAR NOT NULL DEFAULT '{}',
           operation              VARCHAR NOT NULL DEFAULT '',
           source_asset_ids_json  VARCHAR NOT NULL DEFAULT '[]',
           group_id               VARCHAR NOT NULL,
           request_id             VARCHAR,
           status                 VARCHAR NOT NULL DEFAULT 'active',
           expected               INTEGER NOT NULL DEFAULT 0,
           counts_json            VARCHAR NOT NULL DEFAULT '{}',
           collection_id          INTEGER,
           replay_json            VARCHAR NOT NULL DEFAULT '{}',
           canceled_at            DATETIME,
           created_at             DATETIME,
           updated_at             DATETIME
       )""",
    """CREATE TABLE IF NOT EXISTS variantitem (
           id                     INTEGER PRIMARY KEY AUTOINCREMENT,
           set_id                 INTEGER NOT NULL,
           stage                  INTEGER NOT NULL DEFAULT 0,
           ordinal                INTEGER NOT NULL DEFAULT 0,
           "key"                  VARCHAR NOT NULL DEFAULT '',
           values_json            VARCHAR NOT NULL DEFAULT '{}',
           parent_item_id         INTEGER,
           source_asset_ids_json  VARCHAR NOT NULL DEFAULT '[]',
           params_json            VARCHAR NOT NULL DEFAULT '{}',
           job_id                 INTEGER,
           attempts               INTEGER NOT NULL DEFAULT 0,
           history_json           VARCHAR NOT NULL DEFAULT '[]',
           asset_ids_json         VARCHAR NOT NULL DEFAULT '[]',
           state                  VARCHAR NOT NULL DEFAULT 'pending',
           state_reason           VARCHAR NOT NULL DEFAULT '',
           validation_state       VARCHAR NOT NULL DEFAULT 'pending',
           validation_json        VARCHAR NOT NULL DEFAULT '[]',
           output_name            VARCHAR NOT NULL DEFAULT '',
           created_at             DATETIME,
           updated_at             DATETIME
       )""",
)

# Names match SQLModel's `ix_<table>_<column>` so a later create_all() is a no-op.
_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_variantrecipe_created_at", "variantrecipe", "created_at"),
    ("ix_variantset_recipe_id", "variantset", "recipe_id"),
    ("ix_variantset_status", "variantset", "status"),
    ("ix_variantset_created_at", "variantset", "created_at"),
    ("ix_variantitem_set_id", "variantitem", "set_id"),
    ("ix_variantitem_parent_item_id", "variantitem", "parent_item_id"),
    # Completion processing finds the item that owns a finished job by this.
    ("ix_variantitem_job_id", "variantitem", "job_id"),
    ("ix_variantitem_state", "variantitem", "state"),
)
_UNIQUE_INDEXES: tuple[tuple[str, str, str], ...] = (
    # Every child job carries this group id; two sets must never share one.
    ("ix_variantset_group_id", "variantset", "group_id"),
    # A retried create resolves to the set the first request made.
    ("ix_variantset_request_id", "variantset", "request_id"),
    # A combination is materialized once per stage of a set.
    ("ux_variantitem_set_stage_key", "variantitem", 'set_id, stage, "key"'),
)


def run(cur: sqlite3.Cursor) -> None:
    for ddl in _TABLES:
        cur.execute(ddl)
    for name, table, cols in _INDEXES:
        add_index(cur, name, table, cols)
    for name, table, cols in _UNIQUE_INDEXES:
        add_unique_index(cur, name, table, cols)
