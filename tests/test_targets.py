"""P08: capability reports, the routing policy, and deterministic routing.

Routing never substitutes a model, never sends inputs to a remote worker
without permission, never rents hardware, and reports unknown hardware as
unknown. The policy is the user's and is visible over HTTP.
"""
from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest

from backend.app import db
from backend.app.config import settings
from backend.app.resources.catalogue import model_resource_id
from backend.app.routers import common
from backend.app.studio import resolver, targets
from backend.app.studio.resolver import ResolveRequest

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "studio" / "presets.json").read_text())


@pytest.fixture(autouse=True)
def _default_policy():
    data = settings.load_overrides()
    data.pop(targets.POLICY_KEY, None)
    settings.save_overrides(data)
    yield
    data = settings.load_overrides()
    data.pop(targets.POLICY_KEY, None)
    settings.save_overrides(data)


@pytest.fixture()
def enqueued(monkeypatch):
    calls: list[int] = []

    async def record(kind, job_id, handler):
        calls.append(job_id)

    monkeypatch.setattr(common, "enqueue", record)
    return calls


@pytest.fixture()
def ctx(tmp_path):
    """A live context with the remote worker connected and the hardware measured."""
    from PIL import Image

    from backend.app import remote_gpu_client

    path = tmp_path / "ref.png"
    Image.new("RGB", (64, 64)).save(path)
    asset = db.add_asset("image", path, width=64, height=64, generator="import", meta={})
    context = resolver.live()
    context.remote_state = lambda: {**remote_gpu_client.last_health(), "connected": True}
    context.hardware = lambda: {"vram_gb": 16.0}
    context.extra["asset"] = asset.id
    return context


def _resolve(ctx, **fields):
    fields.setdefault("seed", 1)
    return resolver.resolve(ResolveRequest(**fields), ctx)


# ---- reports and policy over HTTP ----------------------------------------------------------
def test_targets_report_both_places_and_the_policy(client):
    body = client.get("/api/targets").json()
    assert body["local"]["target"] == "local" and body["remote"]["target"] == "remote"
    assert body["policy"] == targets.DEFAULT_POLICY
    assert len(body["capability_revision"]) == 16
    assert "text_to_image" in body["local"]["operations"]
    assert body["local"]["confidence"] in {"measured", "unknown"}
    assert any(m["variant"] == "turbo" for m in body["local"]["models"]) or body["local"]["models"]
    assert client.get("/api/system").json()["capability_revision"] == body["capability_revision"]


def test_the_policy_is_validated_saved_and_changes_the_revision(client):
    before = client.get("/api/targets").json()["capability_revision"]
    bad = client.put("/api/targets/policy", json={"rent_freely": True})
    assert bad.status_code == 422 or bad.json().get("code") == "unknown_setting"
    saved = client.put("/api/targets/policy", json={"auto_may_use_remote": True}).json()
    assert saved["auto_may_use_remote"] is True and saved["remote_enabled"] is True
    assert client.get("/api/targets").json()["capability_revision"] != before


def test_a_worker_with_different_capabilities_changes_the_revision(monkeypatch):
    from backend.app import remote_gpu_client

    first = targets.reports(connected=True)["capability_revision"]
    monkeypatch.setitem(remote_gpu_client._LAST_HEALTH, "features", ["image_alt", "flf2v"])
    monkeypatch.setitem(remote_gpu_client._LAST_HEALTH, "build", "abc123")
    assert targets.reports(connected=True)["capability_revision"] != first


# ---- routing -------------------------------------------------------------------------------
def test_auto_routes_remote_only_with_permission(ctx):
    remote_model = model_resource_id(settings.a100_image_model)
    plain = _resolve(ctx, operation="text_to_image", model=remote_model, prompt="x")
    assert plain["target"]["resolved"] == "remote"
    assert {b["code"] for b in plain["readiness"]["blockers"]} == {"remote_not_allowed"}
    ctx.policy = lambda: {**targets.DEFAULT_POLICY, "auto_may_use_remote": True}
    allowed = _resolve(ctx, operation="text_to_image", model=remote_model, prompt="x")
    assert allowed["readiness"]["submittable"] is True
    assert "remote use is allowed" in allowed["target"]["reason"]


def test_turning_remote_off_blocks_even_an_explicit_choice(ctx):
    ctx.policy = lambda: {**targets.DEFAULT_POLICY, "remote_enabled": False}
    remote_model = model_resource_id(settings.a100_image_model)
    plan = _resolve(ctx, operation="text_to_image", model=remote_model, prompt="x",
                    target="remote", allow_remote=True)
    assert "remote_disabled" in {b["code"] for b in plan["readiness"]["blockers"]}


def test_inputs_never_go_to_a_remote_worker_without_permission(ctx):
    edit_model = model_resource_id(settings.qwen_edit_model)
    images = {"images": [ctx.extra["asset"]]}
    refused = _resolve(ctx, operation="edit", model=edit_model, prompt="x", target="remote",
                       inputs=images)
    assert "input_transfer_not_allowed" in {b["code"] for b in refused["readiness"]["blockers"]}
    for_this_request = _resolve(ctx, operation="edit", model=edit_model, prompt="x",
                                target="remote", inputs=images, allow_remote=True)
    assert for_this_request["readiness"]["submittable"] is True
    ctx.policy = lambda: {**targets.DEFAULT_POLICY, "send_inputs_to_remote": True}
    by_policy = _resolve(ctx, operation="edit", model=edit_model, prompt="x", target="remote",
                         inputs=images)
    assert by_policy["readiness"]["submittable"] is True


def test_routing_never_substitutes_a_model(ctx):
    plan = _resolve(ctx, operation="text_to_image", prompt="x",
                    model="hf:example/not-configured@0123abcd")
    assert plan["kind"] is None and plan["effective_params"] == {}
    assert plan["readiness"]["state"] == "blocked"


def test_unknown_hardware_is_needs_check_not_ready(ctx):
    ctx.hardware = lambda: {"vram_gb": None}
    ctx.local_model_status = lambda repo: "ready"
    plan = _resolve(ctx, operation="text_to_image", prompt="x",
                    model=model_resource_id(settings.local_image_model))
    assert plan["readiness"]["state"] == "needs_check"
    assert plan["readiness"]["ready"] is False and plan["readiness"]["submittable"] is True
    ctx.hardware = lambda: {"vram_gb": 16.0}
    measured = _resolve(ctx, operation="text_to_image", prompt="x",
                        model=model_resource_id(settings.local_image_model))
    assert measured["readiness"]["state"] in {"ready", "needs_check"}
    assert not any(w["code"] == "needs_check" and w.get("field") == "target"
                   for w in measured["warnings"])


def test_a_memory_estimate_warns_and_never_shrinks_the_request(ctx):
    ctx.local_model_status = lambda repo: "ready"
    ctx.memory_shortfall = lambda repo: (20 * 1024 ** 3, 4 * 1024 ** 3)
    plan = _resolve(ctx, operation="text_to_image", prompt="x",
                    model=model_resource_id(settings.local_image_model),
                    settings={"quality": "High", "aspect": "16:9"})
    warning = next(w for w in plan["warnings"] if w["code"] == "insufficient_memory")
    assert "20.0 GB" in warning["message"] and warning["blocking"] is False
    assert plan["settings"]["quality"] == "High" and plan["readiness"]["submittable"] is True
    ctx.resident = lambda: settings.local_image_model
    loaded = _resolve(ctx, operation="text_to_image", prompt="x",
                      model=model_resource_id(settings.local_image_model))
    assert not any(w["code"] == "insufficient_memory" for w in loaded["warnings"])


def test_resolving_and_submitting_never_rent_hardware(client, enqueued, monkeypatch):
    from backend.app import provisioners

    def refuse(*_a, **_k):
        raise AssertionError("nothing on the generate path may start a rental")

    monkeypatch.setattr(provisioners.ManualProvisioner, "start", refuse)
    from backend.app.provisioners import runpod

    monkeypatch.setattr(runpod.RunpodProvisioner, "start", refuse)
    remote_model = model_resource_id(settings.a100_image_model)
    for body in ({"operation": "text_to_image", "model": remote_model, "prompt": "x",
                  "dry_run": True},
                 {"operation": "text_to_image", "model": remote_model, "prompt": "x",
                  "target": "remote", "allow_remote": True,
                  "request_id": f"rent-{uuid.uuid4().hex}"}):
        client.post("/api/jobs", json=body)


# ---- affinity and lanes --------------------------------------------------------------------
def test_planned_jobs_use_their_runtime_identity_for_affinity_and_lane(
        client, enqueued, monkeypatch):
    from backend.app.backends import local
    from backend.app.queue import lane_for_job

    monkeypatch.setattr(local, "unavailable_reason", lambda: "")
    doc = copy.deepcopy(FIXTURES["no_lora"])
    doc.pop("id")
    doc["name"] = f"Affinity {uuid.uuid4().hex[:6]}"
    doc["model"] = {"resource": model_resource_id(settings.local_image_model)}
    preset = client.post("/api/studio/presets", json=doc).json()
    job_id = client.post("/api/jobs", json={"preset_id": preset["id"], "prompt": "x", "seed": 2,
                                            "request_id": f"aff-{uuid.uuid4().hex}"}).json()["job_id"]
    job = db.get_job(job_id)
    assert job.model_key == settings.local_image_model and job.backend_id == "local"
    assert lane_for_job(job) == "local"


def test_reading_targets_never_asks_a_provider(client, monkeypatch):
    from backend.app import provisioners
    from backend.app.provisioners import runpod

    def refuse(*_a, **_k):
        raise AssertionError("a capability report must not call a provisioner")

    for name in ("start", "stop", "status", "adopt"):
        monkeypatch.setattr(runpod.RunpodProvisioner, name, refuse)
        monkeypatch.setattr(provisioners.ManualProvisioner, name, refuse)
    assert client.get("/api/targets").status_code == 200
    assert client.get("/api/system").status_code == 200
