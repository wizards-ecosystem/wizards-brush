"""Portable exports retain the library record, not only opaque media bytes."""
from __future__ import annotations

import io
import json
import zipfile

from backend.app import db
from backend.app.config import settings
from backend.app.routers.common import persist_video_file


def test_export_zip_contains_manifest_and_video_sidecar(client, no_queue):
    settings.ensure_dirs()
    image = settings.images_dir / "export-manifest-image.png"
    image.write_bytes(b"png bytes")
    video = settings.videos_dir / "export-manifest-video.mp4"
    video.write_bytes(b"mp4 bytes")
    sidecar = video.with_suffix(".mp4.json")
    sidecar.write_text('{"schema":"wizards-brush-video/v1"}\n')

    image_asset = db.add_asset(
        "image", image, width=640, height=480, generator="local_image:txt2img",
        meta={"prompt": "a blue hour city", "seed": 17, "model": "org/model"},
        content_hash="image-hash", size_bytes=image.stat().st_size, mime_type="image/png",
    )
    video_asset = db.add_asset(
        "video", video, width=832, height=480, generator="colab_wan:t2v",
        meta={"prompt": "camera glides forward", "fps": 20, "num_frames": 49},
        content_hash="video-hash", size_bytes=video.stat().st_size, mime_type="video/mp4",
    )
    db.update_asset(image_asset.id, favorite=True, tags=["city", "blue"], caption="A city at dusk")
    db.set_asset_grade(image_asset.id, {"prompt_fidelity": 5, "visual_quality": 4}, 5)

    response = client.post("/api/assets/export", json={
        "ids": [video_asset.id, image_asset.id],
    })
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert image.name in names and video.name in names
        assert video.name + ".json" in names
        manifest = json.loads(archive.read("wizards-brush-manifest.json"))

    assert manifest["schema"] == "wizards-brush-export/v1"
    assert [item["asset_id"] for item in manifest["assets"]] == [video_asset.id, image_asset.id]
    exported_image = manifest["assets"][1]
    assert exported_image["generation"]["seed"] == 17
    assert exported_image["content"]["hash"] == "image-hash"
    assert exported_image["library"] == {
        "favorite": True,
        "rating": 5,
        "grade": {"prompt_fidelity": 5, "visual_quality": 4},
        "tags": ["city", "blue"],
        "caption": "A city at dusk",
        "used_count": 0,
    }


def test_persisted_video_gets_portable_sidecar_and_purge_removes_it(
    client, no_queue, monkeypatch,
):
    from backend.app.utils import io as io_utils

    monkeypatch.setattr(io_utils, "video_thumb", lambda _path, _name: (None, 640, 360))
    src = settings.temp_path / "sidecar-source.mp4"
    src.write_bytes(b"not a real mp4, but persistence is stream-safe")
    job = db.create_job("t2v", {"prompt": "waves"})
    aid = persist_video_file(
        src, job_id=job.id, generator="test-video", meta={"prompt": "waves", "seed": 3},
        tag="sidecar-test", post_interpolate=False, cb=lambda *_a, **_kw: None,
    )
    asset = db.get_asset(aid)
    sidecar = settings.videos_dir / f"{asset.filename}.json"
    payload = json.loads(sidecar.read_text())
    assert payload["schema"] == "wizards-brush-video/v1"
    assert payload["generation"] == {"prompt": "waves", "seed": 3}
    assert payload["content_hash"]

    assert db.delete_asset(aid)
    assert db.purge_deleted([aid]) == 1
    assert not sidecar.exists()


def test_metadata_opt_out_omits_new_and_stale_video_metadata(client, no_queue, monkeypatch):
    from backend.app.utils import io as io_utils

    monkeypatch.setattr(
        type(settings), "effective_bool",
        lambda _self, name: name != "embed_metadata",
    )
    monkeypatch.setattr(io_utils, "video_thumb", lambda _path, _name: (None, 640, 360))
    src = settings.temp_path / "private-sidecar-source.mp4"
    src.write_bytes(b"private video")
    job = db.create_job("t2v", {"prompt": "private prompt"})
    aid = persist_video_file(
        src, job_id=job.id, generator="test-video", meta={"prompt": "private prompt"},
        tag="private-sidecar-test", post_interpolate=False, cb=lambda *_a, **_kw: None,
    )
    asset = db.get_asset(aid)
    assert asset is not None
    sidecar = settings.videos_dir / f"{asset.filename}.json"
    assert not sidecar.exists()

    # A stale sidecar from an earlier setting must not leak through either the
    # static mount or a later export.
    sidecar.write_text('{"generation":{"prompt":"private prompt"}}\n')
    assert client.get(f"/files/videos/{sidecar.name}").status_code == 404
    response = client.post("/api/assets/export", json={"ids": [aid]})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert asset.filename + ".json" not in archive.namelist()
        manifest = json.loads(archive.read("wizards-brush-manifest.json"))
    assert manifest["assets"][0]["generation"] is None
    assert db.get_asset(aid).meta["prompt"] == "private prompt"
