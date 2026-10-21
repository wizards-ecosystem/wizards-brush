"""Names of recipe settings that were left at the kind's default.

0010 created `recipe`. Databases that already applied that migration do not
pick up a column added to its CREATE TABLE later, so this adds
`defaulted_json` on its own. Fresh databases get the column from the model
or from 0010; `add_column` is a no-op either way.
"""
from __future__ import annotations

import sqlite3

from .ops import add_column


def run(cur: sqlite3.Cursor) -> None:
    add_column(cur, "recipe", "defaulted_json", "VARCHAR NOT NULL DEFAULT '[]'")
