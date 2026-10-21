"""Saved generation recipes.

One new table and no change to any existing one. A recipe freezes a job kind's
settings and the few fields a later run may change. It is not a Variant Set
recipe (`variantrecipe`): running it submits one ordinary job, not a fan-out.

The DDL is written out here rather than derived from the live SQLModel class,
for the same reason as `0008_variant_sets`: a migration must mean the same
thing forever.

In a normal start `SQLModel.create_all()` has already built the table, so the
CREATE is a no-op and only the indexes are checked. Everything here is
idempotent.
"""
from __future__ import annotations

import sqlite3

from .ops import add_index, add_unique_index

_TABLE = """CREATE TABLE IF NOT EXISTS recipe (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         VARCHAR NOT NULL DEFAULT '',
    slug         VARCHAR NOT NULL DEFAULT '',
    description  VARCHAR NOT NULL DEFAULT '',
    kind         VARCHAR NOT NULL DEFAULT '',
    params_json  VARCHAR NOT NULL DEFAULT '{}',
    fields_json  VARCHAR NOT NULL DEFAULT '[]',
    inputs_json  VARCHAR NOT NULL DEFAULT '{}',
    client       VARCHAR NOT NULL DEFAULT '',
    created_at   DATETIME,
    updated_at   DATETIME
)"""

# `ix_recipe_slug` matches SQLModel's name for Field(index=True, unique=True).
_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_recipe_created_at", "recipe", "created_at"),
    ("ix_recipe_updated_at", "recipe", "updated_at"),
)
_UNIQUE_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_recipe_slug", "recipe", "slug"),
    # ASCII case-fold. "Cup" and "cup" are one recipe, so run-by-name is unique.
    ("ux_recipe_name_nocase", "recipe", "name COLLATE NOCASE"),
)


def run(cur: sqlite3.Cursor) -> None:
    cur.execute(_TABLE)
    for name, table, cols in _INDEXES:
        add_index(cur, name, table, cols)
    for name, table, cols in _UNIQUE_INDEXES:
        add_unique_index(cur, name, table, cols)
