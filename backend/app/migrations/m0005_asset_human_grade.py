"""Persist structured human review separately from generation metadata."""
from __future__ import annotations

from .ops import add_column


def run(cur) -> None:
    # Empty JSON means the pre-rubric asset is unreviewed. Existing star ratings
    # are intentionally preserved as the legacy fallback in scoring.py.
    add_column(cur, "asset", "grade_json", "TEXT NOT NULL DEFAULT '{}'")
