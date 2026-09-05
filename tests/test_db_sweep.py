"""Orphan-file GC: referenced_paths() must protect every live input, and
sweep_orphan_files() must respect both the reference set and the mtime age gate —
a regression here silently deletes a rerun input mid-workflow."""
from __future__ import annotations

import os
import time

from backend.app import db
from backend.app.config import settings


def test_referenced_paths_keeps_asset_and_job_inputs(client, no_queue):
    settings.ensure_dirs()
    asset_path = settings.uploads_dir / "ref_asset.png"
    asset_path.write_bytes(b"x")
    db.add_asset("image", asset_path)

    ref = str(settings.uploads_dir / "ref_input.png")
    ref_list = str(settings.uploads_dir / "ref_multi.png")
    db.create_job("img2img", {"image_path": ref, "image_paths": [ref_list]})

    keep = db.referenced_paths()
    assert str(asset_path) in keep
    assert ref in keep
    assert ref_list in keep


def test_sweep_removes_unreferenced_but_keeps_referenced(client, no_queue):
    settings.ensure_dirs()
    orphan = settings.uploads_dir / "sweep_orphan.png"
    kept = settings.uploads_dir / "sweep_kept.png"
    orphan.write_bytes(b"x")
    kept.write_bytes(b"x")
    db.create_job("img2img", {"image_path": str(kept)})  # kept is referenced

    old = time.time() - 3600  # older than any age gate
    for f in (orphan, kept):
        os.utime(f, (old, old))

    db.sweep_orphan_files(max_age_min=0)
    assert not orphan.exists()  # unreferenced + old → removed
    assert kept.exists()  # referenced → always protected


def test_sweep_skips_recent_files(client, no_queue):
    settings.ensure_dirs()
    recent = settings.uploads_dir / "sweep_recent.png"
    recent.write_bytes(b"x")  # freshly written → mtime ~now
    db.sweep_orphan_files(max_age_min=30)
    assert recent.exists()  # too new to be an orphan, even unreferenced


def test_sweep_targets_leftover_video_segments(client, no_queue):
    settings.ensure_dirs()
    seg = settings.videos_dir / "_seg_leftover.mp4"
    seg.write_bytes(b"x")
    os.utime(seg, (time.time() - 3600, time.time() - 3600))
    db.sweep_orphan_files(max_age_min=0)
    assert not seg.exists()
