"""Studio presets: schema-versioned definitions and immutable revisions.

A v2 preset is a `recipe` row with `schema_version` 2. The row is the head of
the catalogue entry (name, latest revision, archive flag); every revision is
an append-only `reciperevision` row, so a job that ran revision 3 can always
read revision 3, after edits and after the preset is archived. v1 rows (saved
generators) get `schema_version` 1 and run exactly as before.

Name uniqueness was one index over every row. It now applies to v1 rows only:
a shipped curated preset must not fail to install because a person named a
saved generator the same way. v2 entries are unique by `preset_id`.

Additive and idempotent. The DDL is written out rather than derived from the
live model, so this migration means the same thing forever.
"""
from __future__ import annotations

import sqlite3

from .ops import add_column, add_index, table_exists

_REVISIONS = """CREATE TABLE IF NOT EXISTS reciperevision (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id       INTEGER NOT NULL,
    preset_id       VARCHAR NOT NULL,
    revision        INTEGER NOT NULL,
    schema_version  INTEGER NOT NULL DEFAULT 2,
    origin          VARCHAR NOT NULL DEFAULT '',
    definition_json VARCHAR NOT NULL DEFAULT '{}',
    definition_hash VARCHAR NOT NULL DEFAULT '',
    based_on        INTEGER,
    client          VARCHAR NOT NULL DEFAULT '',
    created_at      DATETIME
)"""


def run(cur: sqlite3.Cursor) -> None:
    add_column(cur, "recipe", "schema_version", "INTEGER NOT NULL DEFAULT 1")
    add_column(cur, "recipe", "preset_id", "VARCHAR")
    add_column(cur, "recipe", "revision", "INTEGER NOT NULL DEFAULT 1")
    add_column(cur, "recipe", "origin", "VARCHAR NOT NULL DEFAULT ''")
    add_column(cur, "recipe", "definition_json", "VARCHAR NOT NULL DEFAULT '{}'")
    add_column(cur, "recipe", "definition_hash", "VARCHAR NOT NULL DEFAULT ''")
    add_column(cur, "recipe", "archived_at", "DATETIME")
    if table_exists(cur, "recipe"):
        cur.execute("DROP INDEX IF EXISTS ux_recipe_name_nocase")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_recipe_name_nocase_v1 "
                    "ON recipe (name COLLATE NOCASE) WHERE schema_version = 1")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_recipe_preset_id "
                    "ON recipe (preset_id) WHERE preset_id IS NOT NULL")
        add_index(cur, "ix_recipe_schema_version", "recipe", "schema_version")
    cur.execute(_REVISIONS)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_reciperevision_preset_revision "
                "ON reciperevision (preset_id, revision)")
    add_index(cur, "ix_reciperevision_recipe_id", "reciperevision", "recipe_id")
