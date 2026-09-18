"""scripts/api_example.py, run for real against the app in-process.

The example is documentation that executes: if the API it demonstrates drifts,
this fails. Only the Remote GPU is stood in for — the jobs go through the real
queue, the real edit handler, finishing, validation, the Variant Set listener
and the ZIP export.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import zipfile
from pathlib import Path

from PIL import Image

from backend.app import remote_gpu_client

ROOT = Path(__file__).resolve().parent.parent


def _example():
    spec = importlib.util.spec_from_file_location("api_example", ROOT / "scripts" / "api_example.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_remote(path, payload, progress_cb=None, **_kwargs):
    assert path == "/edit"
    source = Image.open(io.BytesIO(base64.b64decode(payload["images_b64"][0]))).convert("RGB")
    shade = hashlib.sha256(payload["prompt"].encode()).digest()
    out = Image.new("RGB", source.size, tuple(shade[:3]))
    if progress_cb:
        progress_cb(1.0, "done")
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return {"image_b64": base64.b64encode(buf.getvalue()).decode()}


def test_the_documented_round_trip_works(client, monkeypatch, tmp_path):
    monkeypatch.setattr(remote_gpu_client, "run_remote", _fake_remote)
    photo = tmp_path / "photo.png"
    Image.new("RGBA", (96, 64), (200, 120, 40, 255)).save(photo)

    result = _example().run(client, photo, tmp_path / "out", tag="pytest-0001", wait_limit=60)

    assert result["status"] == "complete"
    assert [p.name for p in result["edited"]]
    names = sorted(str(p.relative_to(tmp_path / "out" / "set")) for p in result["outputs"])
    assert names == ["moss-green/front.png", "moss-green/side.png",
                     "slate-blue/front.png", "slate-blue/side.png"]
    for path in result["outputs"]:
        with Image.open(path) as img:
            assert img.size == (512, 512), "the stage's resize ran on every variant"
    with zipfile.ZipFile(result["archive"]) as archive:
        members = set(archive.namelist())
    assert "wizards-brush-manifest.json" in members
    assert {"slate-blue/front.png", "moss-green/side.png"} <= members


def test_rerunning_with_the_same_tag_does_not_duplicate_work(client, monkeypatch, tmp_path):
    monkeypatch.setattr(remote_gpu_client, "run_remote", _fake_remote)
    photo = tmp_path / "photo.png"
    Image.new("RGB", (64, 64), (10, 200, 90)).save(photo)
    example = _example()
    first = example.run(client, photo, tmp_path / "a", tag="pytest-0002", wait_limit=60)
    again = example.run(client, photo, tmp_path / "b", tag="pytest-0002", wait_limit=60)
    assert again["edit_job"] == first["edit_job"] and again["set"] == first["set"]
