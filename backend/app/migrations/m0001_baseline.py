"""Baseline: the columns and indexes the old additive `_migrate()` added.

Idempotent by construction, which is the whole point. A database created before
migrations existed already has these columns; running this against it changes
nothing and simply records the id. That removes the need for any "detect the old
schema and bootstrap the ledger" logic.
"""
from __future__ import annotations

import sqlite3

from .ops import add_column, add_index

# table -> [(column, ddl)]
_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "asset": [
        ("favorite", "INTEGER NOT NULL DEFAULT 0"),
        ("rating", "INTEGER NOT NULL DEFAULT 0"),
        ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("caption", "TEXT NOT NULL DEFAULT ''"),
        ("deleted_at", "TIMESTAMP"),
    ],
    "job": [
        ("priority", "INTEGER NOT NULL DEFAULT 0"),
        ("group_id", "TEXT"),
    ],
}

# SQLModel's create_all only builds indexes for tables IT creates, so a database
# upgraded by ALTER would run these filter/sort columns as table scans.
_INDEXES: dict[str, list[str]] = {
    "asset": ["favorite", "rating", "deleted_at"],
    "job": ["priority", "group_id"],
}


def run(cur: sqlite3.Cursor) -> None:
    for table, cols in _COLUMNS.items():
        for name, ddl in cols:
            add_column(cur, table, name, ddl)
    for table, index_cols in _INDEXES.items():
        for col in index_cols:
            # Match SQLModel's default index name so a later create_all is a no-op.
            add_index(cur, f"ix_{table}_{col}", table, col)
