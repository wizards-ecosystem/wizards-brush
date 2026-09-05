"""_migrate against a hand-built legacy table: adds the missing columns, is
idempotent, and skips tables that don't exist yet."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text

from backend.app import db


def _legacy_engine(tmp_path: Path):
    tmp = tmp_path / "legacy.db"
    eng = create_engine(f"sqlite:///{tmp}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE asset (id INTEGER PRIMARY KEY, kind TEXT, path TEXT, "
            "filename TEXT, meta_json TEXT DEFAULT '{}')"
        ))
    return eng


def _cols(eng, table: str) -> set[str]:
    with eng.begin() as conn:
        return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}


def test_migrate_adds_missing_columns_and_is_idempotent(tmp_path):
    eng = _legacy_engine(tmp_path)
    assert "favorite" not in _cols(eng, "asset")
    db._migrate(engine=eng)
    got = _cols(eng, "asset")
    assert {"favorite", "rating", "tags_json"} <= got
    db._migrate(engine=eng)  # second run is a no-op
    assert _cols(eng, "asset") == got


def test_migrate_skips_missing_tables(tmp_path):
    tmp = tmp_path / "empty.db"
    eng = create_engine(f"sqlite:///{tmp}")
    db._migrate(engine=eng)  # must not raise on a DB with no tables


def _indexes(eng, table: str) -> set[str]:
    with eng.begin() as conn:
        return {row[1] for row in conn.execute(text(f"PRAGMA index_list({table})"))}


def test_migrate_creates_indexes_on_migrated_columns(tmp_path):
    # A DB upgraded via ALTER (not create_all) would otherwise run queue-ordering
    # and gallery filter/sort columns as full table scans.
    eng = _legacy_engine(tmp_path)
    assert "ix_asset_favorite" not in _indexes(eng, "asset")
    db._migrate(engine=eng)
    idx = _indexes(eng, "asset")
    assert {"ix_asset_favorite", "ix_asset_rating"} <= idx
    db._migrate(engine=eng)  # CREATE INDEX IF NOT EXISTS — idempotent
    assert {"ix_asset_favorite", "ix_asset_rating"} <= _indexes(eng, "asset")


def test_migrate_adds_unique_submission_identity(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'legacy-job.db'}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE job (id INTEGER PRIMARY KEY, kind TEXT, status TEXT, "
            "params_json TEXT DEFAULT '{}', result_json TEXT DEFAULT '{}')"
        ))
    db._migrate(engine=eng)
    assert "request_id" in _cols(eng, "job")
    assert "ux_job_request_id" in _indexes(eng, "job")
