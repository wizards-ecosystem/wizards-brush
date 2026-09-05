"""Job history retention: bounded, but never at the cost of a gallery image's
ability to be reran or inspected."""
from __future__ import annotations

from pathlib import Path

from backend.app import db
from backend.app.models import JobStatus


def _finished(kind="image_local", status=JobStatus.done.value):
    j = db.create_job(kind, {"prompt": "p"})
    db.update_job(j.id, status=status)
    return j.id


def test_recent_jobs_are_kept(client):
    ids = [_finished() for _ in range(5)]
    db.prune_job_history(keep=10)
    assert all(db.get_job(i) is not None for i in ids)


def test_old_jobs_beyond_the_window_go(client):
    ids = [_finished() for _ in range(6)]
    db.prune_job_history(keep=2)
    survivors = [i for i in ids if db.get_job(i) is not None]
    assert len(survivors) <= 2, "only the retention window should remain"


def test_queued_and_running_jobs_are_never_pruned(client):
    q = db.create_job("image_local", {"prompt": "q"})
    r = db.create_job("image_local", {"prompt": "r"})
    db.update_job(r.id, status=JobStatus.running.value)
    for _ in range(5):
        _finished()
    db.prune_job_history(keep=1)
    assert db.get_job(q.id) is not None, "a queued job is not history"
    assert db.get_job(r.id) is not None, "a running job is not history"


def test_a_job_a_live_asset_points_at_survives(client, tmp_path: Path):
    """Rerun reads the job's params. Pruning it would silently break that image."""
    old = _finished()
    f = tmp_path / "keep.png"
    f.write_bytes(b"x")
    db.add_asset("image", f, job_id=old, generator="local")
    for _ in range(5):
        _finished()
    db.prune_job_history(keep=1)
    assert db.get_job(old) is not None, "reachable from the gallery, so retained"


def test_a_job_whose_asset_was_trashed_can_be_pruned(client, tmp_path: Path):
    old = _finished()
    f = tmp_path / "gone.png"
    f.write_bytes(b"x")
    a = db.add_asset("image", f, job_id=old, generator="local")
    db.delete_assets([a.id])          # soft delete
    for _ in range(5):
        _finished()
    db.prune_job_history(keep=1)
    assert db.get_job(old) is None


def test_pruning_nothing_reports_zero(client):
    db.prune_job_history(keep=10_000)
    assert db.prune_job_history(keep=10_000) == 0
