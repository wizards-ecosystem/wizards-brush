from pathlib import Path

from backend.app import db
from backend.app.config import settings
from backend.app.models import JobStatus


def setup_module() -> None:
    db.init_db()


def test_job_lifecycle():
    job = db.create_job("image_local", {"prompt": "hi", "seed": 1})
    assert job.id is not None and job.status == JobStatus.queued.value

    db.mark_running(job.id)
    j = db.get_job(job.id)
    assert j.status == JobStatus.running.value and j.started_at is not None

    db.mark_done(job.id, {"asset_ids": [1, 2]})
    j = db.get_job(job.id)
    assert j.status == JobStatus.done.value
    assert j.result == {"asset_ids": [1, 2]}
    assert j.params == {"prompt": "hi", "seed": 1, "_v": 1}


def test_mark_error():
    job = db.create_job("t2v", {})
    db.mark_error(job.id, "boom")
    j = db.get_job(job.id)
    assert j.status == JobStatus.error.value and "boom" in j.error


def test_reconcile_orphans_cancels_stuck_jobs():
    job = db.create_job("image_local", {})
    db.mark_running(job.id)
    n = db.reconcile_orphans()
    assert n >= 1
    assert db.get_job(job.id).status == JobStatus.canceled.value


def test_remote_orphan_identity_is_available_before_reconciliation():
    job = db.create_job("t2v", {})
    db.mark_running(job.id)
    db.set_remote_identity(job.id, "client-1", "token-1")
    assert (job.id, "token-1", "client-1") in db.remote_orphans()
    db.reconcile_orphans()
    assert (job.id, "token-1", "client-1") not in db.remote_orphans()


def _touch(name: str) -> Path:
    p = settings.images_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_asset_crud_and_search():
    p = _touch("t_search_1.png")
    a = db.add_asset("image", p, width=64, height=64, generator="local_image:txt2img",
                     meta={"prompt": "sunset over water", "seed": 7})
    db.update_asset(a.id, favorite=True, rating=4, tags=["beach", "warm"])

    rows, total = db.search_assets(q="sunset")
    assert total >= 1 and any(r.id == a.id for r in rows)

    rows, _ = db.search_assets(favorite=True, min_rating=4)
    assert any(r.id == a.id for r in rows)

    rows, _ = db.search_assets(q="beach")  # tag search
    assert any(r.id == a.id for r in rows)

    rows, _ = db.search_assets(q="no-such-token-xyz")
    assert not any(r.id == a.id for r in rows)

    got = db.get_asset(a.id)
    assert got.tags == ["beach", "warm"] and got.favorite and got.rating == 4

    # Deletion is a soft delete now: the asset disappears from every normal
    # accessor, but the row and its files survive until the trash is emptied.
    before_trash = db.count_deleted()
    assert db.delete_asset(a.id)
    assert db.get_asset(a.id) is None
    assert db.get_asset(a.id, include_deleted=True) is not None
    assert db.count_deleted() == before_trash + 1
    assert db.restore_assets([a.id]) == 1
    assert db.get_asset(a.id) is not None
    assert db.delete_asset(a.id) and db.purge_deleted([a.id]) == 1
    assert db.get_asset(a.id, include_deleted=True) is None
    assert not p.exists()  # file removed with the asset


def test_history_dedups_consecutive():
    db.add_history("image_local", "same prompt", "", {})
    before = len(db.list_history())
    db.add_history("image_local", "same prompt", "", {})
    assert len(db.list_history()) == before  # deduped
    db.add_history("image_local", "", "", {})
    assert len(db.list_history()) == before  # blank prompts skipped


def test_user_presets_roundtrip():
    p = db.add_user_preset("prompt", "my look", {"text": "golden hour"}, scope="all")
    assert any(x.id == p.id for x in db.list_user_presets(type="prompt"))
    assert db.delete_user_preset(p.id)
    assert not any(x.id == p.id for x in db.list_user_presets())


def test_clear_all_preserves_history_and_presets():
    _touch("t_clear_1.png")
    a = db.add_asset("image", settings.images_dir / "t_clear_1.png", meta={"prompt": "x"})
    db.add_history("image_local", "keep me", "", {})
    keep = db.add_user_preset("prompt", "keeper", {"text": "t"})

    db.clear_all()

    assert db.get_asset(a.id) is None
    assert db.list_jobs() == []
    assert any(h.prompt == "keep me" for h in db.list_history())
    assert any(x.id == keep.id for x in db.list_user_presets())
