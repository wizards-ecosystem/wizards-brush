"""P16: sheet and atlas exports of a Variant Set, published as artifacts."""
# The Variant Set fixtures are imported by name, so parameters redefine them.
# ruff: noqa: F811
from __future__ import annotations

import io
import json
import zipfile

from PIL import Image
from test_variant_sets import (  # type: ignore[import-not-found]  # noqa: F401
    FAIL_REMOTE,
    _items,
    _two_by_two,
    enqueued,
    matte,
    remote,
    run_child,
    source,
)

from backend.app import db
from backend.app.config import settings


def _finished_set(client, source, *, fail_last=False) -> int:
    set_id = client.post("/api/variant-sets", json={
        "name": "Tone study", "recipe": _two_by_two(source.id)}).json()["id"]
    items = _items(set_id)
    if fail_last:
        db.update_job(items[3].job_id, params={**db.get_job(items[3].job_id).params,
                                                "prompt": f"{FAIL_REMOTE} x"})
    for item in items:
        run_child(int(item.job_id))
    return set_id


def test_preview_then_export_publishes_a_validated_artifact(client, enqueued, source, remote):
    set_id = _finished_set(client, source)
    profile = {"mode": "grid", "columns": 2, "padding": 2, "name": "tones"}
    preview = client.post(f"/api/variant-sets/{set_id}/sprites/preview", json=profile)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["pages"] == [{"width": 2 + 2 * 98, "height": 2 + 2 * 66}]
    assert [f["key"] for f in body["frames"]] == [i.key for i in _items(set_id)]

    made = client.post(f"/api/variant-sets/{set_id}/sprites", json=profile)
    assert made.status_code == 200, made.text
    artifact = made.json()
    assert artifact["mime_type"] == "application/zip" and len(artifact["sha256"]) == 64
    assert artifact["source"] == f"variant_set:{set_id}"
    download = client.get(artifact["file_url"])
    assert download.status_code == 200
    import hashlib

    assert hashlib.sha256(download.content).hexdigest() == artifact["sha256"]
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    assert sorted(archive.namelist()) == ["manifest.json", "tones.png"]
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["format"] == "wizards-brush/atlas" and manifest["format_version"] == 1
    page = Image.open(io.BytesIO(archive.read("tones.png")))
    assert page.size == (manifest["pages"][0]["width"], manifest["pages"][0]["height"])
    assert str(settings.output_path) not in json.dumps(manifest)
    # A second download still works: the file outlives the first response.
    assert client.get(artifact["file_url"]).status_code == 200
    assert client.get(f"/api/artifacts/{artifact['id']}").json()["sha256"] == artifact["sha256"]


def test_a_missing_frame_refuses_strict_and_keeps_its_index_when_partial(
        client, enqueued, source, remote):
    set_id = _finished_set(client, source, fail_last=True)
    strict = client.post(f"/api/variant-sets/{set_id}/sprites", json={"mode": "atlas"})
    assert strict.status_code == 409 and "partial" in strict.json()["detail"]
    partial = client.post(f"/api/variant-sets/{set_id}/sprites",
                          json={"mode": "atlas", "partial": True})
    assert partial.status_code == 200, partial.text
    manifest = partial.json()["manifest"]
    assert [f["index"] for f in manifest["frames"]] == [0, 1, 2]
    assert manifest["missing"][0]["index"] == 3


def test_labels_follow_the_metadata_privacy_setting(client, enqueued, source, remote):
    set_id = _finished_set(client, source)
    shared = client.post(f"/api/variant-sets/{set_id}/sprites", json={"mode": "atlas"}).json()
    assert shared["manifest"]["frames"][0]["labels"]["values"]
    previous = settings.load_overrides()
    settings.save_overrides({**previous, "embed_metadata": False})
    try:
        private = client.post(f"/api/variant-sets/{set_id}/sprites",
                              json={"mode": "atlas"}).json()
    finally:
        settings.save_overrides(previous)
    assert "labels" not in private["manifest"]["frames"][0]
    assert "variant_set" not in private["manifest"]


def test_an_artifact_can_be_deleted_and_is_then_gone(client, enqueued, source, remote):
    set_id = _finished_set(client, source)
    artifact = client.post(f"/api/variant-sets/{set_id}/sprites", json={"mode": "atlas"}).json()
    assert client.delete(f"/api/artifacts/{artifact['id']}").json() == {"ok": True}
    assert client.get(artifact["file_url"]).status_code == 404
    # The frames it was made from are untouched.
    assert all(db.get_asset(f["asset_id"]) for f in artifact["manifest"]["frames"])


def test_mcp_previews_exports_and_fetches_the_same_artifact(client, enqueued, source, remote):
    import base64
    import hashlib

    import anyio
    from mcp import Client

    from backend.app.agent import mcp_server

    def call(name, args):
        async def run():
            async with Client(mcp_server.mcp) as mcp:
                return await mcp.call_tool(name, args)

        return anyio.run(run)

    set_id = _finished_set(client, source)
    profile = {"mode": "atlas", "trim": True}
    preview = call("preview_sprite_export", {"variant_set": set_id, "profile": profile})
    http = client.post(f"/api/variant-sets/{set_id}/sprites/preview", json=profile).json()
    assert preview.structured_content == http
    made = call("create_sprite_export", {"variant_set": set_id, "profile": profile})
    artifact = made.structured_content
    fetched = call("fetch_artifact", {"artifact": artifact["id"]})
    blob = base64.b64decode(fetched.content[1].resource.blob)
    assert hashlib.sha256(blob).hexdigest() == artifact["sha256"]
    bad = call("create_sprite_export", {"variant_set": set_id, "profile": {"mode": "hexagon"}})
    assert bad.is_error
