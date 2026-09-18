"""API views never reveal where the app is installed.

Job params hold absolute input paths and failed jobs hold tracebacks. Clients
see paths relative to the app folder (and `~` for the owner's home); the
stored rows keep the real paths, so rerun and recovery are unaffected.
"""
from __future__ import annotations

import json

from backend.app import db, redaction
from backend.app.config import ROOT
from backend.app.models import JobRead


def test_paths_are_made_relative_to_the_app_folder():
    root = str(ROOT)
    assert redaction.redact_text(f"{root}/output/images/a.png") == "output/images/a.png"
    assert redaction.redact_text(f'File "{root}/backend/app/x.py", line 3') == \
        'File "backend/app/x.py", line 3'
    assert redaction.redact_text(f"cwd {root}") == "cwd ."
    home = redaction._HOME
    assert redaction.redact_text(f"{home}/elsewhere/b.png") == "~/elsewhere/b.png"
    nested = {"image_paths": [f"{root}/output/uploads/u.png"], "n": 3, "flag": None,
              "meta": {"src_path": f"{root}/output/images/s.png"}}
    assert redaction.redact(nested) == {
        "image_paths": ["output/uploads/u.png"], "n": 3, "flag": None,
        "meta": {"src_path": "output/images/s.png"}}


def test_job_views_are_redacted_but_the_stored_row_is_not(client):
    root = str(ROOT)
    params = {"prompt": "p", "src_path": f"{root}/output/images/src.png",
              "image_paths": [f"{root}/output/uploads/in.png"]}
    job = db.create_job("upscale", params)
    trace = f'Traceback (most recent call last):\n  File "{root}/backend/app/queue.py", line 1'
    db.mark_error(job.id, f"source image file no longer exists\n{trace}")

    shown = client.get(f"/api/jobs/{job.id}").json()
    text = json.dumps(shown)
    assert root not in text and redaction._HOME not in text
    assert shown["params"]["src_path"] == "output/images/src.png"
    assert 'File "backend/app/queue.py"' in shown["error"]
    listed = next(j for j in client.get("/api/jobs").json() if j["id"] == job.id)
    assert listed["params"] == shown["params"]
    assert db.get_job(job.id).params["src_path"] == f"{root}/output/images/src.png"
    assert JobRead.of(db.get_job(job.id)).params["image_paths"] == ["output/uploads/in.png"]


def test_prompt_history_is_redacted(client):
    root = str(ROOT)
    db.add_history("img2img", "a remembered prompt", "", {
        "prompt": "a remembered prompt", "image_path": f"{root}/output/uploads/x.png"})
    entry = next(h for h in client.get("/api/history").json()
                 if h["prompt"] == "a remembered prompt")
    assert entry["params"]["image_path"] == "output/uploads/x.png"
