"""Record which client submitted a job (ui, mcp, or the API token's name)."""
from __future__ import annotations

import sqlite3

from .ops import add_column


def run(cur: sqlite3.Cursor) -> None:
    add_column(cur, "job", "client", "TEXT NOT NULL DEFAULT ''")
