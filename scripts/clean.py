"""Wipe all generated media: deletes every image/video/thumb/upload file and the
asset + job rows in the DB. Prompt history and saved user presets are preserved.

Run via `make clean-gens`. Safe to run while the server is stopped or running.
"""
from __future__ import annotations

import pathlib
import sys

# Make `backend.app` importable no matter the cwd.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.app import db

if __name__ == "__main__":
    db.init_db()
    res = db.clear_all()
    print(f"Cleaned: {res['files_deleted']} files, {res['assets_deleted']} assets, "
          f"{res['jobs_deleted']} jobs deleted.")
    print("Prompt history and user presets were preserved.")
