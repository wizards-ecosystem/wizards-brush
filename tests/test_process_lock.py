"""The single-process guard on the database.

SQLite's WAL keeps the file consistent under concurrent writers. It does nothing
about two schedulers, which is the actual failure: two lanes draining one queue,
both claiming the same rows.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.app import db


@pytest.fixture(autouse=True)
def released():
    db.release_process_lock()
    yield
    db.release_process_lock()


def test_a_lock_is_taken_and_a_sidecar_appears(tmp_path: Path):
    target = tmp_path / "gen.db"
    assert db.acquire_process_lock(target) is True
    assert (tmp_path / "gen.db.lock").exists()


def test_a_second_process_is_refused_with_an_actionable_message(tmp_path: Path):
    """Fail immediately, and say what to do — a second instance is a mistake to
    report, not a queue to join."""
    from filelock import FileLock

    target = tmp_path / "gen.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    other = FileLock(str(target) + ".lock")
    other.acquire(timeout=0)
    try:
        with pytest.raises(db.DatabaseInUseError) as e:
            db.acquire_process_lock(target)
        msg = str(e.value)
        assert "already using" in msg
        assert "OUTPUT_DIR" in msg, "tell the user how to run a second instance"
        assert "two job queues" in msg, "say why it matters"
    finally:
        other.release()


def test_acquiring_twice_in_one_process_is_idempotent(tmp_path: Path):
    target = tmp_path / "gen.db"
    assert db.acquire_process_lock(target) is True
    assert db.acquire_process_lock(target) is True, "re-entrant, not a deadlock"


def test_releasing_frees_it_for_the_next_holder(tmp_path: Path):
    target = tmp_path / "gen.db"
    db.acquire_process_lock(target)
    db.release_process_lock()
    assert db.acquire_process_lock(target) is True


def test_releasing_when_nothing_is_held_is_harmless():
    db.release_process_lock()
    db.release_process_lock()


def test_an_in_memory_database_has_nothing_to_lock():
    assert db.acquire_process_lock(Path(":memory:")) is False


def test_a_missing_filelock_degrades_rather_than_refusing_to_start(tmp_path, monkeypatch):
    """Refusing to start over a missing optional dependency would be worse than
    the race it prevents."""
    import builtins

    real = builtins.__import__

    def no_filelock(name, *a, **kw):
        if name == "filelock":
            raise ImportError("no filelock")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_filelock)
    assert db.acquire_process_lock(tmp_path / "gen.db") is False
