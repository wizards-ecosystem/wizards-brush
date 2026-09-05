"""Deduplicate client generation submissions across lost/retried responses."""
from __future__ import annotations

import sqlite3

from .ops import add_column, add_unique_index


def run(cur: sqlite3.Cursor) -> None:
    add_column(cur, "job", "request_id", "TEXT")
    add_unique_index(cur, "ux_job_request_id", "job", "request_id")
