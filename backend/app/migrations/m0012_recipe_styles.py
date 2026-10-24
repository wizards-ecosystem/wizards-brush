"""Style fragments frozen onto a saved generator.

The form composes the prompt from the subject the person typed and the style
chips they selected. A saved generator kept only the subject, so running it
produced a different prompt from Generate with the same subject. `styles_json`
holds the ordered fragments ({"id", "text"}) captured at save time; a run
composes them onto its subject the way the form does. Existing rows get '[]'
and run exactly as before.
"""
from __future__ import annotations

import sqlite3

from .ops import add_column


def run(cur: sqlite3.Cursor) -> None:
    add_column(cur, "recipe", "styles_json", "VARCHAR NOT NULL DEFAULT '[]'")
