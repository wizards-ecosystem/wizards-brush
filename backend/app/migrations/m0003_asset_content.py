"""Split an asset's *content* from its *reference*.

Before: one row per file, so the same bytes saved twice were two unrelated rows
with no way to know they were identical, and a file that vanished left a row
that threw when you opened it.

After: `assetcontent` holds the bytes (identified by a content hash, unique), and
`asset` holds everywhere those bytes appear — path, metadata, ratings, tags,
collection membership.

**Asset ids are deliberately preserved.** `asset.id` is referenced by jobs,
collection membership, the frontend router, and every URL a user may have open.
Renaming the table and re-keying the rows would have meant remapping all of it,
so the split adds a table and a foreign key rather than rebuilding.

Existing rows get `content_id = NULL` and are picked up by the enrichment worker,
which hashes them in the background. Nothing is lost and nothing blocks: the
gallery is fully usable while the backlog drains.

File-state columns arrive here too. `is_missing` and `mtime_ns` turn "the file
is gone" from a row that raises into a row you can show and offer to clean up —
the mirror of `sweep_orphan_files()`, which handles files with no row.
"""
from __future__ import annotations

import sqlite3

from .ops import add_column, add_index, table_exists


def run(cur: sqlite3.Cursor) -> None:
    if not table_exists(cur, "assetcontent"):
        cur.execute(
            """CREATE TABLE assetcontent (
                   id          INTEGER PRIMARY KEY,
                   hash        TEXT    NOT NULL,
                   size_bytes  INTEGER NOT NULL DEFAULT 0,
                   mime_type   TEXT    NOT NULL DEFAULT '',
                   created_at  TIMESTAMP
               )"""
        )
    # UNIQUE is what makes the table content-addressed: two saves of identical
    # bytes resolve to the same row rather than racing to insert twice.
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_assetcontent_hash ON assetcontent (hash)")

    add_column(cur, "asset", "content_id", "INTEGER REFERENCES assetcontent(id)")
    add_index(cur, "ix_asset_content_id", "asset", "content_id")

    # Filesystem reconciliation state.
    add_column(cur, "asset", "mtime_ns", "INTEGER")
    add_column(cur, "asset", "is_missing", "INTEGER NOT NULL DEFAULT 0")
    add_column(cur, "asset", "needs_verify", "INTEGER NOT NULL DEFAULT 0")
    add_index(cur, "ix_asset_is_missing", "asset", "is_missing")

    # Enrichment as a resumable ladder rather than a boolean. Dimensions and the
    # hash are free at write time, so they are not rungs: 0 saved, 1 captioned,
    # 2 tagged, 3 reserved for the search embedding.
    add_column(cur, "asset", "enrich_level", "INTEGER NOT NULL DEFAULT 0")
    add_index(cur, "ix_asset_enrich_level", "asset", "enrich_level")

    if not table_exists(cur, "asset"):
        return

    # Rows that already carry a caption were enriched under the old boolean
    # scheme. Promote them so the worker does not redo work it already did.
    cur.execute(
        "UPDATE asset SET enrich_level = 2 "
        "WHERE enrich_level = 0 AND caption IS NOT NULL AND caption != ''"
    )
