"""The migration runner: ordering, the applied ledger, transactions, backups,
and the guards that stop a newer database being opened by older code."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from backend.app import db
from backend.app import migrations as mig


def _legacy_engine(tmp_path: Path):
    """A database shaped like one created before migrations existed."""
    eng = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE asset (id INTEGER PRIMARY KEY, kind TEXT, path TEXT, "
            "filename TEXT, meta_json TEXT DEFAULT '{}', caption TEXT DEFAULT '')"
        ))
        conn.execute(text(
            "CREATE TABLE job (id INTEGER PRIMARY KEY, kind TEXT, status TEXT, "
            "params_json TEXT DEFAULT '{}')"
        ))
    return eng


def _cols(eng, table: str) -> set[str]:
    with eng.begin() as conn:
        return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}


def _applied(eng) -> set[str]:
    with eng.begin() as conn:
        return {r[0] for r in conn.execute(text("SELECT id FROM applied_migrations"))}


def test_every_migration_runs_and_is_recorded(tmp_path):
    eng = _legacy_engine(tmp_path)
    db._migrate(engine=eng)
    assert _applied(eng) == {mid for mid, _ in mig.MIGRATIONS}


def test_second_run_is_a_no_op(tmp_path):
    eng = _legacy_engine(tmp_path)
    db._migrate(engine=eng)
    before = _cols(eng, "asset"), _applied(eng)
    db._migrate(engine=eng)
    assert (_cols(eng, "asset"), _applied(eng)) == before


def test_migration_ids_are_unique_and_sorted():
    ids = [mid for mid, _ in mig.MIGRATIONS]
    assert len(ids) == len(set(ids)), "duplicate migration id"
    # The numeric prefix must agree with list position, or "ordered" is a lie.
    assert ids == sorted(ids), "MIGRATIONS is not in id order"


def test_unknown_applied_id_is_refused(tmp_path):
    """A database written by a newer build must not be silently operated on."""
    eng = _legacy_engine(tmp_path)
    db._migrate(engine=eng)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO applied_migrations (id) VALUES ('9999_from_the_future')"))
    with pytest.raises(mig.MigrationError, match="does not know"):
        db._migrate(engine=eng)


def test_a_failing_migration_rolls_back_and_is_not_recorded(tmp_path):
    eng = _legacy_engine(tmp_path)
    calls = []

    def good(cur):
        calls.append("good")
        cur.execute("ALTER TABLE asset ADD COLUMN probe_ok INTEGER DEFAULT 0")

    def bad(cur):
        calls.append("bad")
        cur.execute("ALTER TABLE asset ADD COLUMN probe_bad INTEGER DEFAULT 0")
        raise RuntimeError("boom")

    original = mig.MIGRATIONS[:]
    mig.MIGRATIONS[:] = [("0001_good", good), ("0002_bad", bad)]
    try:
        with pytest.raises(mig.MigrationError, match="0002_bad"):
            db._migrate(engine=eng)
        cols = _cols(eng, "asset")
        assert "probe_ok" in cols, "the migration before the failure should have committed"
        assert "probe_bad" not in cols, "the failing migration must roll back entirely"
        assert _applied(eng) == {"0001_good"}
    finally:
        mig.MIGRATIONS[:] = original


def test_backup_written_before_migrating_an_existing_file(tmp_path):
    eng = _legacy_engine(tmp_path)
    assert not list(tmp_path.glob("legacy_backup_*.db"))
    db._migrate(engine=eng)
    backups = list(tmp_path.glob("legacy_backup_*.db"))
    assert len(backups) == 1, "exactly one backup for a run with pending migrations"
    # The backup must be a real, readable database — not a truncated file.
    with sqlite3.connect(backups[0]) as c:
        assert c.execute("SELECT count(*) FROM asset").fetchone()[0] == 0


def test_no_backup_when_there_is_nothing_to_do(tmp_path):
    eng = _legacy_engine(tmp_path)
    db._migrate(engine=eng)
    for b in tmp_path.glob("legacy_backup_*.db"):
        b.unlink()
    db._migrate(engine=eng)
    assert not list(tmp_path.glob("legacy_backup_*.db"))


def test_empty_database_does_not_raise(tmp_path):
    """Fresh install: create_all has not run, so no table exists yet."""
    eng = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    db._migrate(engine=eng)
    assert _applied(eng) == {mid for mid, _ in mig.MIGRATIONS}


def test_legacy_columns_and_indexes_arrive(tmp_path):
    eng = _legacy_engine(tmp_path)
    db._migrate(engine=eng)
    assert {"favorite", "rating", "tags_json", "deleted_at"} <= _cols(eng, "asset")
    with eng.begin() as conn:
        idx = {r[1] for r in conn.execute(text("PRAGMA index_list(asset)"))}
    assert {"ix_asset_favorite", "ix_asset_rating"} <= idx
    assert {"remote_client_id", "remote_token"} <= _cols(eng, "job")


def test_model_key_backfilled_from_params(tmp_path):
    eng = _legacy_engine(tmp_path)
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO job (id, kind, status, params_json) VALUES "
            "(1, 'image_local', 'done', '{\"model_variant\": \"quality\"}'),"
            "(2, 'image_local', 'done', '{\"model_variant\": \"turbo\"}'),"
            "(3, 'image_colab',  'done', '{\"model_variant\": \"quality\"}'),"
            "(4, 'image_local', 'done', 'not valid json at all')"
        ))
    db._migrate(engine=eng)
    with eng.begin() as conn:
        got = dict(conn.execute(text("SELECT id, model_key FROM job")).all())
    assert got[1] == "quality"
    assert got[2] == "turbo"
    assert got[3] is None, "remote jobs never get a local model key"
    assert got[4] is None, "a malformed params blob costs that row its key, not the migration"


def test_captioned_rows_promoted_on_the_enrichment_ladder(tmp_path):
    eng = _legacy_engine(tmp_path)
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO asset (id, kind, path, filename, caption) VALUES "
            "(1, 'image', '/a.png', 'a.png', 'a cat on a mat'),"
            "(2, 'image', '/b.png', 'b.png', '')"
        ))
    db._migrate(engine=eng)
    with eng.begin() as conn:
        got = dict(conn.execute(text("SELECT id, enrich_level FROM asset")).all())
    assert got[1] == 2, "an existing caption means captioning and tagging already ran"
    assert got[2] == 0


def test_content_table_hash_is_unique(tmp_path):
    eng = _legacy_engine(tmp_path)
    db._migrate(engine=eng)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO assetcontent (hash, size_bytes) VALUES ('abc', 1)"))
    with pytest.raises(Exception, match="UNIQUE"), eng.begin() as conn:
        conn.execute(text("INSERT INTO assetcontent (hash, size_bytes) VALUES ('abc', 2)"))
