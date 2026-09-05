"""merge_asset_tags concurrency contract + cap, and caption full-text search."""
from __future__ import annotations

from backend.app import db
from backend.app.config import settings


def _asset(name="tag.png"):
    settings.ensure_dirs()
    return db.add_asset("image", settings.uploads_dir / name)


def test_merge_preserves_user_tags_set_after_read(client, no_queue):
    a = _asset("merge1.png")
    db.update_asset(a.id, tags=["user-added"])  # user tags between enrich read + write
    m = db.merge_asset_tags(a.id, ["auto1", "user-added", "auto2"])
    assert set(m.tags) == {"user-added", "auto1", "auto2"}  # union, no dup


def test_merge_caps_tag_count(client, no_queue):
    a = _asset("merge2.png")
    m = db.merge_asset_tags(a.id, [f"t{i}" for i in range(50)], limit=20)
    assert len(m.tags) == 20


def test_merge_writes_extra_fields(client, no_queue):
    a = _asset("merge3.png")
    m = db.merge_asset_tags(a.id, ["x"], caption="a majestic tiger")
    assert m.caption == "a majestic tiger"


def test_merge_missing_asset_returns_none(client, no_queue):
    assert db.merge_asset_tags(10_000_000, ["x"]) is None


def test_search_matches_caption(client, no_queue):
    a = _asset("cap.png")
    db.update_asset(a.id, caption="a rare snow leopard on a ridge")
    rows, _total = db.search_assets(q="snow leopard")
    assert a.id in [r.id for r in rows]
