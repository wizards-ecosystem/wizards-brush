"""P05: preset requests through POST /api/jobs, strict submission and replay.

Through the real HTTP surface and the live resolver. The model is whatever this
environment configures for the local turbo slot, so the tests do not depend on
a developer's `.env`. The queue's enqueue is stubbed: a job is stored and its
handler is run by hand where a test needs the handler's actual inputs.
"""
from __future__ import annotations

import asyncio
import copy
import json
import uuid
from pathlib import Path

import pytest
from PIL import Image

from backend.app import db
from backend.app.config import settings
from backend.app.resources.catalogue import model_resource_id
from backend.app.routers import common

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "studio" / "presets.json").read_text())


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


def _turbo() -> str:
    return model_resource_id(settings.local_image_model)


def _preset(client, case="no_lora", **changes) -> dict:
    doc = copy.deepcopy(FIXTURES[case])
    doc.pop("id", None)
    doc.pop("revision", None)
    doc["name"] = f"{doc['name']} {uuid.uuid4().hex[:6]}"
    doc["model"] = {"resource": _turbo()}
    doc.update(changes)
    response = client.post("/api/studio/presets", json=doc)
    assert response.status_code == 200, response.text
    return response.json()


def _rid() -> str:
    return f"plan-test-{uuid.uuid4().hex}"


# ---- the request form ---------------------------------------------------------------------
def test_a_kind_and_a_preset_request_cannot_be_mixed(client):
    response = client.post("/api/jobs", json={"kind": "image_local", "preset_id": "custom/x"})
    assert response.status_code == 400 and response.json()["code"] == "ambiguous_request"
    assert response.json()["field"] == "preset_id"
    neither = client.post("/api/jobs", json={"params": {"prompt": "x"}})
    assert neither.status_code == 400 and neither.json()["field"] == "kind"
    both = client.post("/api/jobs", json={"operation": "text_to_image", "params": {"prompt": "x"},
                                          "model": _turbo(), "dry_run": True})
    assert both.status_code == 400 and both.json()["code"] == "ambiguous_request"


def test_a_preview_chooses_a_seed_once_and_creates_nothing(client, enqueued, local_ready):
    preset = _preset(client)
    before = len(db.list_jobs(500)) if hasattr(db, "list_jobs") else None
    first = client.post("/api/jobs", json={"preset_id": preset["id"], "prompt": "a fox",
                                           "dry_run": True})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["dry_run"] is True and body["plan"]["seed"]["origin"] == "runtime"
    again = client.post("/api/jobs", json={"preset_id": preset["id"], "prompt": "a fox",
                                           "dry_run": True, "seed": body["seed"]})
    assert again.json()["plan"]["hashes"]["creative_request_hash"] == \
        body["plan"]["hashes"]["creative_request_hash"]
    assert again.json()["plan"]["seed"]["origin"] == "override"
    assert enqueued == []
    if before is not None:
        assert len(db.list_jobs(500)) == before


# ---- submission ---------------------------------------------------------------------------
def test_a_plan_becomes_an_ordinary_job_with_exactly_the_planned_params(
        client, enqueued, local_ready):
    preset = _preset(client)
    request = {"preset_id": preset["id"], "prompt": "a fox", "seed": 7,
               "settings": {"quality": "High", "aspect": "3:4"}}
    plan = client.post("/api/jobs", json={**request, "dry_run": True}).json()["plan"]
    submitted = client.post("/api/jobs", json={**request, "request_id": _rid(),
                                               "expected_plan_hash":
                                                   plan["hashes"]["execution_plan_hash"]})
    assert submitted.status_code == 200, submitted.text
    job = db.get_job(submitted.json()["job_id"])
    assert job.kind == plan["kind"] == "image_local" and job.plan_id is not None
    stored = {k: v for k, v in job.params.items() if k != "_v"}
    assert stored == plan["effective_params"]
    row = db.get_plan(job.plan_id)
    assert row.creative_hash == plan["hashes"]["creative_request_hash"]
    assert row.preset_id == preset["id"] and row.preset_revision == 1
    assert client.get(f"/api/jobs/{job.id}").json()["plan_id"] == job.plan_id
    read = client.get(f"/api/plans/{job.plan_id}").json()
    assert read["plan"]["hashes"] == plan["hashes"] and read["request"]["prompt"] == "a fox"
    assert enqueued == [job.id]


def test_a_retried_request_id_is_the_same_job_and_a_different_request_conflicts(
        client, enqueued, local_ready):
    preset = _preset(client)
    rid = _rid()
    body = {"preset_id": preset["id"], "prompt": "a fox", "seed": 3, "request_id": rid}
    first = client.post("/api/jobs", json=body).json()
    again = client.post("/api/jobs", json=body).json()
    assert again["job_id"] == first["job_id"] and again["duplicate"] is True
    other = client.post("/api/jobs", json={**body, "prompt": "a wolf"})
    assert other.status_code == 409 and other.json()["code"] == "request_conflict"
    assert len(enqueued) == 1


def test_a_changed_setup_is_refused_as_stale_with_what_changed(client, enqueued, local_ready):
    preset = _preset(client)
    request = {"preset_id": preset["id"], "prompt": "a fox", "seed": 5}
    plan = client.post("/api/jobs", json={**request, "dry_run": True}).json()["plan"]
    edited = copy.deepcopy(preset["definition"])
    edited["settings"]["quality"] = "Draft"
    client.put(f"/api/studio/presets/{preset['id']}",
               json={"definition": edited, "base_revision": 1})
    stale = client.post("/api/jobs", json={**request, "revision": None, "request_id": _rid(),
                                           "expected_plan_hash":
                                               plan["hashes"]["execution_plan_hash"]})
    assert stale.status_code == 409 and stale.json()["code"] == "plan_stale"
    changed = {c["field"] for c in stale.json()["state"]["changes"]}
    assert "effective_params.quality" in changed and "effective_params.steps" in changed
    assert enqueued == []


def test_a_capability_change_that_changes_nothing_runnable_is_not_stale(
        client, enqueued, local_ready, monkeypatch):
    from backend.app import remote_gpu_client

    preset = _preset(client)
    request = {"preset_id": preset["id"], "prompt": "a fox", "seed": 5}
    plan = client.post("/api/jobs", json={**request, "dry_run": True}).json()["plan"]
    monkeypatch.setitem(remote_gpu_client._LAST_HEALTH, "features", ["some_new_feature"])
    ok = client.post("/api/jobs", json={**request, "request_id": _rid(),
                                        "expected_plan_hash": plan["hashes"]["execution_plan_hash"]})
    assert ok.status_code == 200, ok.text


def test_a_plan_that_is_not_ready_creates_no_job(client, enqueued, local_ready):
    preset = _preset(client, "two_adapter_stack")      # its adapters are not installed here
    response = client.post("/api/jobs", json={"preset_id": preset["id"], "prompt": "x",
                                              "request_id": _rid()})
    assert response.status_code == 503 and response.json()["code"] == "resource_not_ready"
    assert response.json()["retryable"] is True
    assert enqueued == []


# ---- what was previewed is what runs (S23) --------------------------------------------------
@pytest.mark.parametrize("settings_", [
    {"quality": "High", "steps": 20},                       # quality overrides explicit steps
    {"quality": "Custom", "steps": 13},
    {"aspect": "16:9", "width": 999, "height": 111},        # aspect overrides dimensions
    {"aspect": "Custom", "width": 1000, "height": 700},
    {"batch": 8, "quality": "Ultra", "aspect": "16:9"},     # batch capped by pixel budget
])
def test_the_previewed_plan_is_the_queued_params_and_the_handler_inputs(
        client, enqueued, local_ready, monkeypatch, settings_):
    from backend.app.generators import local_image
    from backend.app.routers import images

    preset = _preset(client)
    request = {"preset_id": preset["id"], "prompt": "a fox", "seed": 11, "settings": settings_}
    plan = client.post("/api/jobs", json={**request, "dry_run": True}).json()["plan"]
    job = db.get_job(client.post("/api/jobs", json={**request, "request_id": _rid()}).json()["job_id"])
    effective = plan["effective_params"]
    assert {k: v for k, v in job.params.items() if k != "_v"} == effective

    seen: list[dict] = []

    def fake_generate(**kwargs):
        seen.append(kwargs)
        return Image.new("RGB", (kwargs["width"], kwargs["height"]))

    monkeypatch.setattr(local_image, "generate", fake_generate)
    monkeypatch.setattr(local_image, "resolve_model", lambda variant: settings.local_image_model)
    monkeypatch.setattr(local_image, "effective_step_count", lambda *a: effective["steps"])
    images._local_handler("txt2img")(job.id, job.params, lambda *a, **k: None)
    assert len(seen) == effective["batch"]
    for index, call in enumerate(seen):
        assert (call["steps"], call["width"], call["height"]) == (
            effective["steps"], effective["width"], effective["height"])
        assert call["guidance"] == effective["guidance"]
        assert call["seed"] == plan["seed"]["items"][index]
        assert call["prompt"] == plan["prompt"]["positive"]


def test_speed_mode_on_the_remote_lane_previews_what_runs(client, enqueued, monkeypatch):
    from backend.app import remote_gpu_client

    monkeypatch.setitem(remote_gpu_client._LAST_HEALTH, "connected", True)
    model = model_resource_id(settings.a100_image_model)
    request = {"operation": "text_to_image", "model": model, "prompt": "a lighthouse",
               "target": "remote", "seed": 2, "settings": {"speed_mode": True}}
    plan = client.post("/api/jobs", json={**request, "dry_run": True}).json()["plan"]
    assert plan["kind"] == "image_colab", plan["readiness"]
    effective = plan["effective_params"]
    assert effective["speed_mode"] is True and effective["guidance"] == 1.0
    assert effective["negative_prompt"] == "" and effective["steps"] <= 8
    job = db.get_job(client.post("/api/jobs", json={**request, "request_id": _rid()}).json()["job_id"])
    assert {k: v for k, v in job.params.items() if k != "_v"} == effective


# ---- dispatch ------------------------------------------------------------------------------
def test_a_queued_plan_whose_slot_changed_fails_as_stale_and_never_runs(
        client, enqueued, local_ready, monkeypatch):
    from backend.app.queue import LANES

    preset = _preset(client)
    job_id = client.post("/api/jobs", json={"preset_id": preset["id"], "prompt": "x", "seed": 1,
                                            "request_id": _rid()}).json()["job_id"]
    monkeypatch.setattr(settings, "local_image_model", "someone/another-model")
    ran: list[int] = []

    def handler(job_id, params, cb):
        ran.append(job_id)
        return {"asset_ids": []}

    asyncio.run(LANES["local"]._execute(job_id, handler, "image_local"))
    job = db.get_job(job_id)
    assert ran == [] and job.status == "error"
    assert "plan_stale" in job.error and job.tip


def test_dispatch_records_the_runtime_that_executes(client, enqueued, local_ready):
    from backend.app.queue import LANES

    preset = _preset(client)
    job_id = client.post("/api/jobs", json={"preset_id": preset["id"], "prompt": "x", "seed": 1,
                                            "request_id": _rid()}).json()["job_id"]
    asyncio.run(LANES["local"]._execute(job_id, lambda *a: {"asset_ids": []}, "image_local"))
    job = db.get_job(job_id)
    assert job.status == "done"
    assert job.execution["target"] == "local" and job.execution["app"]
    assert "diffusers" in job.execution["libraries"]


# ---- replay --------------------------------------------------------------------------------
def test_exact_replay_reuses_the_plan(client, enqueued, local_ready):
    preset = _preset(client)
    job_id = client.post("/api/jobs", json={"preset_id": preset["id"], "prompt": "a fox",
                                            "seed": 21, "request_id": _rid()}).json()["job_id"]
    replayed = client.post(f"/api/jobs/{job_id}/replay", json={})
    assert replayed.status_code == 200, replayed.text
    original, copy_ = db.get_job(job_id), db.get_job(replayed.json()["job_id"])
    assert copy_.params == original.params and copy_.plan_id != original.plan_id
    assert db.get_plan(copy_.plan_id).replay_of == original.plan_id


def test_exact_replay_refuses_a_missing_resource_instead_of_substituting(
        client, enqueued, local_ready, monkeypatch):
    preset = _preset(client)
    job_id = client.post("/api/jobs", json={"preset_id": preset["id"], "prompt": "a fox",
                                            "seed": 21, "request_id": _rid()}).json()["job_id"]
    monkeypatch.setattr(settings, "local_image_model", "someone/another-model")
    refused = client.post(f"/api/jobs/{job_id}/replay", json={})
    assert refused.status_code == 409 and refused.json()["code"] == "replay_resource_unavailable"
    assert refused.json()["field"] == "model"


def test_a_legacy_job_cannot_claim_an_exact_replay(client, enqueued, local_ready):
    legacy = client.post("/api/jobs", json={"kind": "image_local", "params": {"prompt": "x"},
                                            "request_id": _rid()}).json()["job_id"]
    refused = client.post(f"/api/jobs/{legacy}/replay", json={})
    assert refused.status_code == 409 and refused.json()["code"] == "legacy_unverified"


def test_run_latest_preset_is_a_new_plan_from_the_new_revision(client, enqueued, local_ready):
    preset = _preset(client)
    job_id = client.post("/api/jobs", json={"preset_id": preset["id"], "prompt": "a fox",
                                            "seed": 21, "request_id": _rid()}).json()["job_id"]
    edited = copy.deepcopy(preset["definition"])
    edited["settings"]["quality"] = "Draft"
    client.put(f"/api/studio/presets/{preset['id']}",
               json={"definition": edited, "base_revision": 1})
    latest = client.post(f"/api/jobs/{job_id}/replay", json={"mode": "latest", "dry_run": True})
    assert latest.status_code == 200, latest.text
    plan = latest.json()["plan"]
    assert plan["preset"]["revision"] == 2 and plan["settings"]["quality"] == "Draft"
