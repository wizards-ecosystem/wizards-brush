"""P13: MCP parity for presets, plans, targets, cancellation, masks and bytes.

Through the SDK's in-process client. The tools wrap the HTTP route functions,
so the checks here are that they agree with HTTP (the same hashes for the same
request), keep the structured error shape, and never overstate an outcome.
"""
from __future__ import annotations

import base64
import copy
import io
import json
import uuid
from pathlib import Path

import anyio
import pytest
from mcp import Client
from mcp.types import ImageContent
from PIL import Image

from backend.app import db, queue
from backend.app.agent import mcp_server, studio_tools
from backend.app.config import ROOT, settings
from backend.app.resources.catalogue import model_resource_id
from backend.app.routers import common

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "studio" / "presets.json").read_text())


def _error(result) -> dict:
    text = result.content[0].text
    return json.loads(text[text.find("{"):])


def _call(name: str, args: dict):
    async def run():
        async with Client(mcp_server.mcp) as mcp:
            return await mcp.call_tool(name, args)

    return anyio.run(run)


def _definition(case: str = "no_lora") -> dict:
    doc = copy.deepcopy(FIXTURES[case])
    doc.pop("id", None)
    doc.pop("revision", None)
    doc["name"] = f"{doc['name']} {uuid.uuid4().hex[:6]}"
    doc["model"] = {"resource": model_resource_id(settings.local_image_model)}
    return doc


@pytest.fixture()
def enqueued(monkeypatch):
    calls: list[int] = []

    async def record(kind, job_id, handler):
        calls.append(job_id)

    monkeypatch.setattr(common, "enqueue", record)
    return calls


@pytest.fixture()
def local_ready(monkeypatch):
    from backend.app.backends import local

    monkeypatch.setattr(local, "unavailable_reason", lambda: "")


def _png(size=(32, 24), mode="L", color=255) -> str:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---- discovery -------------------------------------------------------------------------------
def test_targets_and_processors_match_http_and_hide_the_install_path(client):
    result = _call("list_targets", {})
    assert result.is_error is False
    body = result.structured_content
    assert body["capability_revision"] == client.get("/api/targets").json()["capability_revision"]
    assert body["policy"]["auto_may_use_remote"] is False
    assert str(ROOT) not in json.dumps(body)
    processors = _call("list_processors", {}).structured_content
    assert any(p["name"] == "background_removal" for p in processors["finishing"])
    assert "validators" in processors


def test_unknown_presets_and_bad_options_keep_the_error_shape(client):
    missing = _call("get_preset", {"preset": "custom/does-not-exist"})
    assert missing.is_error and _error(missing)["code"] == "unknown_preset"
    bad = _call("list_presets", {"origin": "shipped"})
    assert bad.is_error and _error(bad)["field"] == "origin"


# ---- presets ---------------------------------------------------------------------------------
def test_an_ai_saved_preset_is_custom_with_its_author_and_conflicts_lose_nothing(client):
    saved = _call("save_preset", {"definition": _definition()}).structured_content
    assert saved["origin"] == "custom" and saved["client"] == "mcp"
    curated = _call("save_preset", {"definition": {**_definition(), "origin": "curated"}})
    assert curated.is_error and _error(curated)["code"] == "curated_reserved"

    first = copy.deepcopy(saved["definition"])
    first["settings"]["aspect"] = "16:9"
    second = copy.deepcopy(saved["definition"])
    second["settings"]["aspect"] = "9:16"
    ok = _call("update_preset", {"preset": saved["id"], "definition": first, "base_revision": 1})
    assert ok.structured_content["revision"] == 2
    stale = _call("update_preset", {"preset": saved["id"], "definition": second,
                                    "base_revision": 1})
    assert stale.is_error and _error(stale)["code"] == "revision_conflict"
    kept = _call("update_preset", {"preset": saved["id"], "definition": second,
                                   "base_revision": 1, "on_conflict": "copy"}).structured_content
    assert kept["cloned_from"] == {"id": saved["id"], "revision": 1}
    revisions = _call("preset_revisions", {"preset": saved["id"]}).structured_content
    assert [r["revision"] for r in revisions["revisions"]] == [1, 2]
    archived = _call("archive_preset", {"preset": saved["id"]}).structured_content
    assert archived["archived"] is True
    assert _call("get_preset", {"preset": saved["id"], "revision": 1}).is_error is False


# ---- plans -----------------------------------------------------------------------------------
def test_mcp_and_http_resolve_the_same_request_to_the_same_hashes(client, local_ready):
    preset = client.post("/api/studio/presets", json=_definition("style_template")).json()
    request = {"preset_id": preset["id"], "prompt": "a lantern festival", "seed": 5,
               "styles": ["warm"], "settings": {"aspect": "16:9"}}
    http = client.post("/api/jobs", json={**request, "dry_run": True}).json()
    mcp = _call("resolve_preset", request)
    assert mcp.is_error is False, mcp.content
    via_mcp = mcp.structured_content["plan"]["hashes"]
    assert via_mcp["creative_request_hash"] == http["plan"]["hashes"]["creative_request_hash"]
    # Identical target state here, so what would run is identical too.
    assert via_mcp["material_hash"] == http["plan"]["hashes"]["material_hash"]
    assert via_mcp["execution_plan_hash"] == http["plan"]["hashes"]["execution_plan_hash"]


def test_run_preset_returns_a_durable_job_and_its_plan(client, enqueued, local_ready):
    preset = client.post("/api/studio/presets", json=_definition()).json()
    request = {"preset_id": preset["id"], "prompt": "a fox", "seed": 9}
    plan = _call("resolve_preset", request).structured_content["plan"]
    rid = f"mcp-run-{uuid.uuid4().hex}"
    ran = _call("run_preset", {**request, "wait": False, "request_id": rid,
                               "expected_plan_hash": plan["hashes"]["execution_plan_hash"]})
    assert ran.is_error is False, ran.content
    job_id = ran.structured_content["job_id"]
    assert enqueued == [job_id]
    job = db.get_job(job_id)
    assert job.client == "mcp" and job.plan_id is not None
    stored = _call("get_plan", {"plan_id": job.plan_id}).structured_content
    assert stored["plan"]["hashes"] == plan["hashes"]
    again = _call("run_preset", {**request, "wait": False, "request_id": rid})
    assert again.structured_content["duplicate"] is True
    conflict = _call("run_preset", {**request, "prompt": "a wolf", "wait": False,
                                    "request_id": rid})
    assert conflict.is_error and _error(conflict)["code"] == "request_conflict"
    # Reconnecting later and asking by id needs no resubmission.
    assert _call("get_job", {"job_id": job_id}).structured_content["job"]["id"] == job_id


def test_replay_of_a_legacy_job_is_refused_honestly(client, enqueued, local_ready):
    legacy = client.post("/api/jobs", json={"kind": "image_local", "params": {"prompt": "x"},
                                            "request_id": f"legacy-{uuid.uuid4().hex}"})
    result = _call("replay_job", {"job_id": legacy.json()["job_id"]})
    assert result.is_error and _error(result)["code"] == "legacy_unverified"


# ---- cancellation ----------------------------------------------------------------------------
def test_cancel_distinguishes_queued_running_and_settled(client, enqueued, local_ready):
    def new_job() -> int:
        return client.post("/api/jobs", json={
            "kind": "image_local", "params": {"prompt": "x"},
            "request_id": f"cancel-{uuid.uuid4().hex}"}).json()["job_id"]

    queued = new_job()
    removed = _call("cancel_job", {"job_id": queued}).structured_content
    assert removed["state"] == "removed_from_queue" and removed["settled"] is True

    running = new_job()
    db.mark_running(running)
    lane = queue.LANES["local"]
    lane._inflight.add(running)
    try:
        asked = _call("cancel_job", {"job_id": running}).structured_content
        assert asked["state"] == "cancellation_requested" and asked["settled"] is False
        assert db.get_job(running).status == "running"      # a flag is not a cancellation
        assert "not canceled until" in asked["note"]
    finally:
        lane._inflight.discard(running)
        queue._CANCELLED.discard(running)
        db.mark_canceled(running, "test cleanup", {})

    settled = _call("cancel_job", {"job_id": queued}).structured_content
    assert settled["state"] == "already_settled" and settled["changed"] is False
    missing = _call("cancel_job", {"job_id": 10**9})
    assert missing.is_error and _error(missing)["code"] == "not_found"


# ---- masks and bytes -------------------------------------------------------------------------
def _source(size=(32, 24)) -> int:
    settings.ensure_dirs()
    path = settings.images_dir / f"mcp-src-{uuid.uuid4().hex[:6]}.png"
    Image.new("RGB", size, (1, 2, 3)).save(path)
    return db.add_asset("image", path, width=size[0], height=size[1],
                        generator="import", meta={}).id or 0


def test_a_mask_is_stored_only_when_it_fits_its_source(client):
    source = _source()
    wrong = _call("import_mask", {"mask": _png(size=(10, 10)), "source_asset_id": source})
    assert wrong.is_error and _error(wrong)["code"] == "invalid_mask"
    stored = _call("import_mask", {"mask": _png(), "source_asset_id": source})
    assert stored.is_error is False
    asset = stored.structured_content
    assert asset["generator"] == "mask" and (asset["width"], asset["height"]) == (32, 24)
    assert asset["mime_type"] == "image/png" and asset["file_url"].startswith("/api/assets/")
    remote = _call("import_mask", {"mask": "https://example.com/mask.png"})
    assert remote.is_error


def test_a_mask_over_the_size_cap_is_refused(client, monkeypatch):
    monkeypatch.setattr(studio_tools, "MASK_MAX_BYTES", 10)
    result = _call("import_mask", {"mask": _png()})
    assert result.is_error and _error(result)["code"] == "invalid_mask"


def test_fetch_asset_returns_full_resolution_bytes_within_a_cap(client, monkeypatch):
    source = _source(size=(40, 30))
    result = _call("fetch_asset", {"asset_id": source})
    assert result.is_error is False
    image = next(c for c in result.content if isinstance(c, ImageContent))
    data = base64.b64decode(image.data)
    with Image.open(io.BytesIO(data)) as opened:
        assert opened.size == (40, 30)                       # not a preview
    assert result.structured_content["bytes"] == len(data)
    monkeypatch.setattr(studio_tools, "FETCH_MAX_BYTES", 8)
    capped = _call("fetch_asset", {"asset_id": source})
    assert capped.is_error and _error(capped)["code"] == "too_large"
    assert "/api/assets/" in _error(capped)["suggestions"][0]


# ---- reference sets (P14) --------------------------------------------------------------------
def test_reference_sets_through_mcp_match_http_and_conflicts_lose_nothing(client):
    from PIL import Image

    from backend.app import db

    settings.ensure_dirs()
    ids = []
    for n in range(2):
        path = settings.images_dir / f"mcp-ref-{uuid.uuid4().hex[:6]}-{n}.png"
        Image.new("RGB", (32, 32), (n * 90, 40, 40)).save(path)
        ids.append(int(db.add_asset("image", path, width=32, height=32).id or 0))
    definition = {"roles": [{"role": "product", "entries": [{"asset_id": i} for i in ids]}]}
    saved = _call("save_reference_set", {"name": f"Mug {uuid.uuid4().hex[:6]}",
                                         "definition": definition}).structured_content
    assert saved["revision"] == 1 and saved["client"] == "mcp"
    http = client.get(f"/api/reference-sets/{saved['id']}").json()
    assert http["digest"] == saved["digest"]
    reordered = {"roles": [{"role": "product", "entries": [{"asset_id": i} for i in ids[::-1]]}]}
    ok = _call("update_reference_set", {"reference_set": saved["id"], "definition": reordered,
                                        "base_revision": 1})
    assert ok.structured_content["revision"] == 2
    stale = _call("update_reference_set", {"reference_set": saved["id"],
                                           "definition": definition, "base_revision": 1})
    assert stale.is_error and _error(stale)["code"] == "revision_conflict"
    first = _call("get_reference_set", {"reference_set": saved["id"], "revision": 1})
    assert [e["asset_id"] for e in first.structured_content["definition"]["roles"][0]
            ["entries"]] == ids
    listed = _call("list_reference_sets", {}).structured_content["reference_sets"]
    assert saved["id"] in [s["id"] for s in listed]
    _call("archive_reference_set", {"reference_set": saved["id"]})
    listed = _call("list_reference_sets", {}).structured_content["reference_sets"]
    assert saved["id"] not in [s["id"] for s in listed]
