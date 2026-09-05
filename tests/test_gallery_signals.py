"""Usage tracking, duplicate counts and semantic search fallback."""
from __future__ import annotations

import pytest

from backend.app import db
from backend.app.config import settings


@pytest.fixture
def two_identical(client):
    for f in settings.images_dir.glob("sig_*"):
        f.unlink(missing_ok=True)
    settings.images_dir.mkdir(parents=True, exist_ok=True)
    a_path = settings.images_dir / "sig_a.png"
    b_path = settings.images_dir / "sig_b.png"
    a_path.write_bytes(b"same bytes")
    b_path.write_bytes(b"same bytes")
    from backend.app.utils.hashing import hash_file

    a = db.add_asset("image", a_path, generator="local",
                     content_hash=hash_file(a_path), size_bytes=10)
    b = db.add_asset("image", b_path, generator="local",
                     content_hash=hash_file(b_path), size_bytes=10)
    yield a, b
    ids = [a.id, b.id]
    db.delete_assets(ids)
    db.purge_deleted(ids)


def test_using_an_asset_is_recorded_separately_from_favouriting(client, two_identical):
    a, _ = two_identical
    assert a.used_count == 0
    db.mark_used(a.id)
    db.mark_used(a.id)
    got = db.get_asset(a.id)
    assert got.used_count == 2
    assert got.last_used_at is not None
    assert got.favorite is False, "using is not the same as starring"


def test_the_used_route_reports_the_new_count(client, two_identical):
    a, _ = two_identical
    r = client.post(f"/api/assets/{a.id}/used")
    assert r.status_code == 200
    assert r.json()["used_count"] == 1


def test_marking_an_unknown_asset_is_a_404(client):
    assert client.post("/api/assets/999999/used").status_code == 404


def test_gallery_reports_how_many_copies_share_the_bytes(client, two_identical):
    r = client.get("/api/assets?limit=200")
    items = {i["id"]: i for i in r.json()["items"]}
    a, b = two_identical
    assert items[a.id]["copies"] == 2
    assert items[b.id]["copies"] == 2


def test_a_unique_asset_reports_one_copy(client):
    p = settings.images_dir / "sig_unique.png"
    p.write_bytes(b"nothing else looks like this")
    from backend.app.utils.hashing import hash_file

    a = db.add_asset("image", p, generator="local", content_hash=hash_file(p))
    try:
        r = client.get("/api/assets?limit=200")
        item = next(i for i in r.json()["items"] if i["id"] == a.id)
        assert item["copies"] == 1
    finally:
        db.delete_assets([a.id])
        db.purge_deleted([a.id])


def test_sorting_by_usage_is_available(client, two_identical):
    a, b = two_identical
    db.mark_used(b.id)
    rows, _ = db.search_assets(sort="used", limit=200)
    ordered = [r.id for r in rows if r.id in (a.id, b.id)]
    assert ordered[0] == b.id


def test_semantic_search_falls_back_when_embeddings_are_unavailable(client, monkeypatch):
    """A search that returns keyword matches beats one that returns an error."""
    from backend.app import enrichment

    monkeypatch.setattr(enrichment, "embed_text", lambda q: None)
    r = client.get("/api/assets?q=castle&semantic=true")
    assert r.status_code == 200
    assert "ranked_by" not in r.json(), "fell back to the ordinary text search"


def test_semantic_search_is_opt_in(client):
    """Without the flag the gallery does the ordinary text search, so the heavy
    embedding stack is never touched by an ordinary browse."""
    r = client.get("/api/assets?q=castle")
    assert r.status_code == 200
    assert "ranked_by" not in r.json()


# NOTE: the vector ranking itself (db.search_by_vector) needs numpy, which this
# suite forbids so that CI can run without the diffusion stack. That is the
# right trade — semantic search is gated behind transformers and torch anyway,
# so a test here could only ever exercise a stub. What IS covered above is
# everything a user without embeddings hits: the opt-in flag, and the fallback
# to keyword search when embedding is unavailable.


# ---- paging stability ------------------------------------------------------
def test_tied_sorts_page_without_repeating_or_dropping_rows(client):
    """`rating`, `favorite` and `used` have enormous tie groups — most of a
    library shares rating 0 and used_count 0. SQLite may return tied rows in any
    order, including a different one for the same query, so without a unique
    tiebreak an asset can appear on two consecutive pages while another never
    appears at all."""
    from backend.app import db
    from backend.app.config import settings

    generator = "paging-stability-fixture"
    made: list[int] = []
    try:
        for i in range(12):
            f = settings.images_dir / f"page_{i}.png"
            f.write_bytes(f"img{i}".encode())
            created = db.add_asset("image", f, generator=generator)
            assert created.id is not None
            made.append(created.id)

        for sort in ("rating", "favorite", "used", "recently_used", "newest"):
            seen: list[int] = []
            for offset in range(0, 12, 4):
                rows, _total = db.search_assets(
                    generator=generator, sort=sort, limit=4, offset=offset,
                )
                seen += [a.id for a in rows if a.id is not None]
            assert len(seen) == len(set(seen)), f"{sort}: a row appeared on two pages"
            assert set(seen) == set(made), f"{sort}: rows repeated or fell through the pages"
    finally:
        db.delete_assets(made)
        db.purge_deleted(made)


def test_using_an_asset_is_recorded_and_ranks_it(client):
    """`favorite` is intent; `used_count` is behaviour. Nothing wrote the column
    until the gallery actions started reporting it, which left both 'used' sorts
    ranking an entire library of zeroes."""
    from backend.app import db
    from backend.app.config import settings

    f = settings.images_dir / "used_signal.png"
    f.write_bytes(b"used")
    a = db.add_asset("image", f, generator="local")
    assert db.get_asset(a.id).used_count == 0

    r = client.post(f"/api/assets/{a.id}/used")
    assert r.status_code == 200 and r.json()["used_count"] == 1

    client.post(f"/api/assets/{a.id}/used")
    fresh = db.get_asset(a.id)
    assert fresh.used_count == 2
    assert fresh.last_used_at is not None

    top = db.search_assets(sort="used", limit=1)[0]
    assert top[0].id == a.id, "the most-used asset should lead the 'used' sort"
