"""Usage signals, semantic-search storage, and preset thumbnails.

`favorite` is intent: you said you liked it. `used_count` is behaviour: you
actually exported it, remixed it, or fed it back in as an input. The second is
the more honest signal and the two disagree often enough to be worth keeping
apart.

`embedding` is rung 3 of the enrichment ladder — a CLIP vector stored as a raw
blob. At the size of a personal gallery a brute-force dot product over every
vector is comfortably fast, so there is no vector index and no new service.

`preview_asset_id` on a preset points at any image already in the gallery, so a
style becomes something you can recognise rather than a name in a list. Nothing
is generated for it and nothing new is stored.
"""
from __future__ import annotations

import sqlite3

from .ops import add_column, add_index


def run(cur: sqlite3.Cursor) -> None:
    add_column(cur, "asset", "used_count", "INTEGER NOT NULL DEFAULT 0")
    add_column(cur, "asset", "last_used_at", "TIMESTAMP")
    add_index(cur, "ix_asset_used_count", "asset", "used_count")

    # float32 vector, raw bytes. Nullable: most rows will not have one for a
    # while, and a NULL is the flag the enrichment worker looks for.
    add_column(cur, "asset", "embedding", "BLOB")

    add_column(cur, "userpreset", "preview_asset_id", "INTEGER")

    # Job failures carry a human-readable tip alongside the traceback, so the UI
    # can lead with what to do about it and keep the stack behind a disclosure.
    add_column(cur, "job", "tip", "TEXT NOT NULL DEFAULT ''")
