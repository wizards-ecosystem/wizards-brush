"""Durable ownership of work submitted to the remote A100 lane.

These values are job execution state, not generation parameters. Keeping them
out of params_json prevents rerun from inheriting an old remote-worker token.
"""
from __future__ import annotations

import sqlite3

from .ops import add_column, add_index


def run(cur: sqlite3.Cursor) -> None:
    add_column(cur, "job", "remote_client_id", "TEXT")
    add_column(cur, "job", "remote_token", "TEXT")
    add_index(cur, "ix_job_remote_client_id", "job", "remote_client_id")
    add_index(cur, "ix_job_remote_token", "job", "remote_token")
