"""Enrichment as a resumable ladder, and the content-hash backfill."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.app import db, enrichment
from backend.app.config import settings


@pytest.fixture
def asset(client, tmp_path: Path):
    """One asset, and a clean slate. Content addressing makes these tests
    order-dependent otherwise: a twin left behind by an earlier test shares
    bytes with this one and shows up in its duplicate list."""
    _purge_ladder_assets()
    f = settings.images_dir / "ladder_test.png"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"pretend png bytes")
    a = db.add_asset("image", f, generator="local")
    yield a
    _purge_ladder_assets()


def _purge_ladder_assets() -> None:
    rows, _ = db.search_assets(limit=500)
    doomed = [r.id for r in rows if r.filename.startswith("ladder_")]
    if doomed:
        db.delete_assets(doomed)
        db.purge_deleted(doomed)
    for f in settings.images_dir.glob("ladder_*"):
        f.unlink(missing_ok=True)


def test_a_new_asset_starts_at_the_bottom(asset):
    assert asset.enrich_level == 0


def test_levels_are_ordered_and_named():
    assert (enrichment.LEVEL_SAVED
            < enrichment.LEVEL_CAPTIONED
            < enrichment.LEVEL_TAGGED
            < enrichment.LEVEL_EMBEDDED)


def test_backfill_finds_rows_without_a_content_hash(asset):
    """Rows written before content addressing get picked up in the background,
    so the gallery stays usable instead of blocking a migration on hashing."""
    assert asset.content_id is None
    assert asset.id in db.assets_needing_hash(100)


def test_attaching_a_hash_removes_it_from_the_backlog(asset):
    enrichment._attach_hash(asset.id, Path(asset.path))
    assert asset.id not in db.assets_needing_hash(100)
    assert db.get_asset(asset.id).content_id is not None


def test_identical_bytes_share_one_content_row(asset, client):
    """Deduplication: two references, one stored identity."""
    twin_path = settings.images_dir / "ladder_twin.png"
    twin_path.write_bytes(Path(asset.path).read_bytes())
    twin = db.add_asset("image", twin_path, generator="local")
    enrichment._attach_hash(asset.id, Path(asset.path))
    enrichment._attach_hash(twin.id, twin_path)
    assert db.get_asset(asset.id).content_id == db.get_asset(twin.id).content_id
    assert db.duplicate_ids(asset.id) == [twin.id]


def test_duplicate_counts_are_answered_in_one_query(asset, client):
    twin_path = settings.images_dir / "ladder_twin2.png"
    twin_path.write_bytes(Path(asset.path).read_bytes())
    twin = db.add_asset("image", twin_path, generator="local")
    for a in (asset.id, twin.id):
        enrichment._attach_hash(a, Path(db.get_asset(a).path))
    counts = db.duplicate_counts([asset.id, twin.id])
    assert counts[asset.id] == 2
    assert counts[twin.id] == 2


def test_a_missing_file_is_recorded_not_ignored(asset):
    """The mirror of sweep_orphan_files: a row whose file went away becomes a
    state the UI can show, rather than something that raises when opened."""
    Path(asset.path).unlink()
    enrichment._enrich_one(asset.id)
    assert db.get_asset(asset.id).is_missing is True


def test_purging_one_duplicate_removes_its_own_file_and_spares_the_other(asset, client):
    """Duplicates have identical bytes in SEPARATE files, so each owns its own.

    Purging one must take that one's file with it. Skipping the unlink because a
    duplicate exists elsewhere would strand the full-size image forever — nothing
    sweeps `images/` — while the trash reported itself emptied.
    """
    twin_path = settings.images_dir / "ladder_twin3.png"
    twin_path.write_bytes(Path(asset.path).read_bytes())
    twin = db.add_asset("image", twin_path, generator="local")
    for a in (asset.id, twin.id):
        enrichment._attach_hash(a, Path(db.get_asset(a).path))
    assert db.get_asset(asset.id).content_id == db.get_asset(twin.id).content_id

    db.delete_assets([twin.id])
    db.purge_deleted([twin.id])
    assert not twin_path.exists(), "the purged copy's own file must go with it"
    assert Path(asset.path).exists(), "the other copy has its own file and keeps it"


def test_a_second_row_on_the_same_path_holds_the_file(asset, client):
    """The unlink guard, on the key that actually matters: the path.

    Nothing registers two rows against one file today, but that is the only
    situation in which skipping the unlink is correct, so it is the one the guard
    is written for.
    """
    twin = db.add_asset("image", Path(asset.path), generator="local")
    db.delete_assets([twin.id])
    db.purge_deleted([twin.id])
    assert Path(asset.path).exists(), "the live row still points at this file"

    db.delete_assets([asset.id])
    db.purge_deleted([asset.id])
    assert not Path(asset.path).exists(), "the last holder took it with it"


def test_purging_the_last_reference_does_remove_the_file(client, asset):
    lone = settings.images_dir / "ladder_lone.png"
    lone.write_bytes(b"only copy")
    a = db.add_asset("image", lone, generator="local")
    enrichment._attach_hash(a.id, lone)
    db.delete_assets([a.id])
    db.purge_deleted([a.id])
    assert not lone.exists()


def test_orphan_content_rows_can_be_pruned(client, asset):
    before = db.prune_orphan_content()
    db.get_or_create_content("deadbeef" * 5, size_bytes=1)
    assert db.prune_orphan_content() >= 1
    assert isinstance(before, int)


def test_content_lookup_is_idempotent(client):
    a = db.get_or_create_content("abc123", size_bytes=10)
    b = db.get_or_create_content("abc123", size_bytes=10)
    assert a.id == b.id, "the same bytes must resolve to the same row"
