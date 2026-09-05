#!/usr/bin/env python
"""Start over with a clean database and an empty gallery.

**This deletes generated work. It cannot be undone by anything except the backup
it writes first.**

Why this exists: the asset schema changed shape when content addressing landed.
The migration path preserves everything and is what runs by default — you do not
need this script. It is here for the case where you would rather begin from a
clean slate than carry history forward.

    python scripts/cutover.py            # show what would go, change nothing
    python scripts/cutover.py --apply    # do it, after a typed confirmation

Deleted:
    output/gen.db          the database
    output/images/*        generated images
    output/videos/*        generated videos
    output/thumbs/*        thumbnails
    output/uploads/*       uploaded inputs

Kept:
    models/                weights and LoRAs — the expensive things
    wildcards/             prompt wildcards
    .env                   configuration
    output/runtime_settings.json   the Remote GPU URL and shared secret

A timestamped copy of the database is written beside it before anything is
removed, regardless of flags. The files are not backed up: they are large, and
copying them would mean needing twice the disk to free some.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONFIRM_PHRASE = "delete my gallery"


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n = int(n / 1024)
    return f"{n} GB"


def survey(output_dir: Path) -> tuple[list[tuple[Path, int, int]], int, int]:
    """(per-directory rows, total files, total bytes) for what would be deleted."""
    rows: list[tuple[Path, int, int]] = []
    total_files = total_bytes = 0
    for name in ("images", "videos", "thumbs", "uploads"):
        d = output_dir / name
        if not d.exists():
            continue
        files = [f for f in d.iterdir() if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        rows.append((d, len(files), size))
        total_files += len(files)
        total_bytes += size
    return rows, total_files, total_bytes


def backup_db(db_path: Path) -> Path | None:
    """Copy the database aside using sqlite's own backup API.

    Not a file copy: in WAL mode a copy taken while anything else holds the
    database can be inconsistent, and the one moment this matters is the moment
    before everything is deleted.
    """
    import sqlite3
    from contextlib import closing

    if not db_path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = db_path.parent / f"{db_path.stem}_precutover_{stamp}{db_path.suffix}"
    with closing(sqlite3.connect(db_path)) as src, closing(sqlite3.connect(dest)) as dst:
        src.backup(dst)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Wipe the database and generated files for a clean start.")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (without this, nothing is changed)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the typed confirmation (for scripted use only)")
    args = ap.parse_args()

    from backend.app.config import settings

    out = Path(settings.output_dir)
    db_path = Path(settings.db_path)
    rows, files, size = survey(out)

    print(f"\noutput directory: {out}")
    print(f"database:         {db_path}"
          f"{'' if db_path.exists() else '  (does not exist)'}\n")
    print("would delete:")
    for d, n, s in rows:
        print(f"  {d.name:<10} {n:>6} files   {_human(s):>10}")
    print(f"  {'total':<10} {files:>6} files   {_human(size):>10}\n")
    print("would keep:  models/  wildcards/  .env  output/runtime_settings.json\n")

    if not args.apply:
        print("Dry run. Nothing was changed. Re-run with --apply to proceed.")
        return 0

    if not args.yes:
        print(f"This cannot be undone. Type '{CONFIRM_PHRASE}' to continue.")
        try:
            typed = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if typed != CONFIRM_PHRASE:
            print("Aborted — phrase did not match.")
            return 1

    backup = backup_db(db_path)
    if backup:
        print(f"backed up database to {backup.name}")

    removed = 0
    for d, _, _ in rows:
        for f in d.iterdir():
            if f.is_file():
                f.unlink()
                removed += 1
    # The WAL and shared-memory sidecars go too, or sqlite reconstructs state
    # from them and the "clean" database is not clean.
    for suffix in ("", "-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        if p.exists():
            p.unlink()

    print(f"deleted {removed} file(s) and the database.")
    print("Start the app; the schema will be rebuilt from scratch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
