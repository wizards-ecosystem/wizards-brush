"""Provisioning contract: the only feature here that can spend money.

The tests are weighted accordingly. Most of them are about *not* spending: that
the default cannot, that a misconfiguration falls back to the one that cannot,
that a second start is refused rather than doubling the bill, and that a
credential never reaches a log line or a pod's environment.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from backend.app import provisioners
from backend.app.config import settings
from backend.app.provisioners import ProvisionerError, Session, SessionStore
from backend.app.provisioners import runpod as runpod_mod


@pytest.fixture(autouse=True)
def _clean_session():
    SessionStore().clear()
    yield
    SessionStore().clear()


@pytest.fixture()
def runpod_env(monkeypatch):
    monkeypatch.setattr(settings, "remote_gpu_provisioner", "runpod")
    monkeypatch.setattr(settings, "runpod_api_key", "rpa_SECRETKEYVALUE")
    monkeypatch.setattr(settings, "runpod_image", "example.invalid/wb-worker:abc123")
    monkeypatch.setattr(settings, "remote_gpu_shared_secret", "a-real-shared-secret")
    monkeypatch.setattr(settings, "hf_token", "hf_TOKENVALUE")


# Captured before any patching. `runpod_mod.httpx` is the global httpx module,
# so monkeypatching `.Client` on it patches httpx everywhere — including inside
# this factory, which then calls itself forever.
_REAL_CLIENT = httpx.Client


def _transport(handler):
    """Install a mock HTTP transport, keeping the provisioner's real request code."""
    def factory(*_args, **_kwargs):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler))
    return factory


# ---- the default cannot spend ---------------------------------------------
def test_default_provisioner_is_manual_and_cannot_provision():
    assert settings.remote_gpu_provisioner == "manual"
    provisioner = provisioners.get_provisioner()
    assert provisioner.id == "manual"
    with pytest.raises(ProvisionerError) as excinfo:
        provisioner.start()
    # The refusal has to tell you what to do, or it is just a wall.
    assert "REMOTE_GPU_PROVISIONER=runpod" in str(excinfo.value)


def test_unknown_provisioner_falls_back_rather_than_failing(monkeypatch):
    """A local-first app must start with a broken optional setting."""
    monkeypatch.setattr(settings, "remote_gpu_provisioner", "not-a-provider")
    assert provisioners.get_provisioner().id == "manual"


def test_manual_owns_nothing():
    manual = provisioners.get_provisioner()
    assert manual.status() is None and manual.adopt() is None
    manual.stop()       # must be safe with nothing running


# ---- session bookkeeping ---------------------------------------------------
def test_session_round_trips_through_runtime_settings():
    store = SessionStore()
    store.save(Session(id="pod1", provisioner="runpod", base_url="https://x", gpu="A100"))
    loaded = store.load()
    assert loaded is not None and loaded.id == "pod1" and loaded.gpu == "A100"
    store.clear()
    assert store.load() is None


def test_corrupt_session_record_does_not_block_startup():
    data = settings.load_overrides()
    data["remote_gpu_session"] = {"id": "pod1", "started_at": "not-a-number"}
    settings.save_overrides(data)
    assert SessionStore().load() is None


def test_cost_estimate_is_absent_rather_than_guessed():
    started = time.time() - 1800                    # half an hour
    assert Session(id="p", provisioner="runpod", started_at=started).cost_estimate_usd() is None
    priced = Session(id="p", provisioner="runpod", started_at=started, hourly_usd=1.39)
    assert priced.cost_estimate_usd() == pytest.approx(0.695, abs=0.01)


# ---- runpod: what actually goes over the wire ------------------------------
def test_start_sends_the_worker_image_and_never_the_api_key(runpod_env, monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "pod-abc", "cost": 1.39,
                                         "gpu": {"id": "NVIDIA A100-SXM4-80GB"}})

    monkeypatch.setattr(runpod_mod.httpx, "Client", _transport(handler))
    session = provisioners.get_provisioner().start()

    assert seen["url"] == "https://api.runpod.io/v2/pods"
    assert seen["auth"] == "Bearer rpa_SECRETKEYVALUE"
    body = seen["body"]
    assert body["image"] == "example.invalid/wb-worker:abc123"
    assert body["gpu"] == {"id": "NVIDIA A100-SXM4-80GB", "count": 1}
    assert body["ports"] == ["8000/http"]
    # The worker needs the shared secret to authenticate callers. It has no use
    # for the provisioning key, and a rented container is the last place to put
    # a credential that can create more rented containers.
    assert body["env"]["REMOTE_GPU_SHARED_SECRET"] == "a-real-shared-secret"
    assert "rpa_SECRETKEYVALUE" not in json.dumps(body)
    assert session.id == "pod-abc" and session.hourly_usd == 1.39
    assert session.base_url == "https://pod-abc-8000.proxy.runpod.net"
    # Recorded before returning, so a crash here still leaves it stoppable.
    assert SessionStore().load().id == "pod-abc"


def test_a_second_start_is_refused_instead_of_doubling_the_bill(runpod_env, monkeypatch):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json={"id": "pod-abc"})
        return httpx.Response(200, json={"id": "pod-abc", "status": "RUNNING",
                                         "runtime": {"ports": []}})

    monkeypatch.setattr(runpod_mod.httpx, "Client", _transport(handler))
    provisioner = provisioners.get_provisioner()
    provisioner.start()
    with pytest.raises(ProvisionerError, match="already running"):
        provisioner.start()


def test_network_volume_relocates_the_model_cache(runpod_env, monkeypatch):
    seen: dict = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "pod-abc"})

    monkeypatch.setattr(runpod_mod.httpx, "Client", _transport(handler))
    monkeypatch.setattr(settings, "runpod_network_volume_id", "vol-xyz")
    provisioners.get_provisioner().start()
    body = seen["body"]
    assert body["mounts"]["network"][0]["volumeId"] == "vol-xyz"
    # Without this the weights land on a disk that is discarded on stop, and
    # every session re-downloads tens of GB.
    assert body["env"]["REMOTE_GPU_ROOT"].startswith("/workspace")


# ---- failures the user has to be able to act on ----------------------------
def test_errors_never_leak_the_api_key(runpod_env, monkeypatch):
    def handler(request):
        return httpx.Response(500, text="upstream exploded for key rpa_SECRETKEYVALUE")

    monkeypatch.setattr(runpod_mod.httpx, "Client", _transport(handler))
    with pytest.raises(ProvisionerError) as excinfo:
        provisioners.get_provisioner().start()
    assert "rpa_SECRETKEYVALUE" not in str(excinfo.value)
    assert "***" in str(excinfo.value)


@pytest.mark.parametrize("code,needle", [(401, "RUNPOD_API_KEY"), (403, "permission")])
def test_auth_failures_say_which_one_it_is(runpod_env, monkeypatch, code, needle):
    monkeypatch.setattr(runpod_mod.httpx, "Client",
                        _transport(lambda request: httpx.Response(code, json={})))
    with pytest.raises(ProvisionerError, match=needle):
        provisioners.get_provisioner().start()


def test_missing_image_refuses_before_spending(runpod_env, monkeypatch):
    monkeypatch.setattr(settings, "runpod_image", "")
    def handler(request):
        raise AssertionError("must not reach the API without an image")
    monkeypatch.setattr(runpod_mod.httpx, "Client", _transport(handler))
    with pytest.raises(ProvisionerError, match="RUNPOD_IMAGE"):
        provisioners.get_provisioner().start()


# ---- stop and adopt --------------------------------------------------------
def test_stop_terminates_and_forgets(runpod_env, monkeypatch):
    calls: list = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(201, json={"id": "pod-abc"})
        return httpx.Response(204)

    monkeypatch.setattr(runpod_mod.httpx, "Client", _transport(handler))
    provisioner = provisioners.get_provisioner()
    provisioner.start()
    provisioner.stop()
    # Terminate, not stop: a stopped pod releases the GPU without promising it
    # back and keeps billing an attached volume.
    assert ("DELETE", "/v2/pods/pod-abc") in calls
    assert SessionStore().load() is None


def test_adopt_only_claims_sessions_this_app_recorded(runpod_env, monkeypatch):
    SessionStore().save(Session(id="someone-elses", provisioner="lambda-labs"))
    monkeypatch.setattr(runpod_mod.httpx, "Client",
                        _transport(lambda r: httpx.Response(200, json={})))
    assert provisioners.get_provisioner().adopt() is None


def test_adopt_keeps_an_unreachable_session_because_it_still_bills(runpod_env, monkeypatch):
    SessionStore().save(Session(id="pod-abc", provisioner="runpod", started_at=time.time()))

    def handler(request):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(runpod_mod.httpx, "Client", _transport(handler))
    adopted = provisioners.get_provisioner().adopt()
    assert adopted is not None and adopted.id == "pod-abc"


def test_a_vanished_pod_is_forgotten(runpod_env, monkeypatch):
    SessionStore().save(Session(id="pod-gone", provisioner="runpod"))
    monkeypatch.setattr(runpod_mod.httpx, "Client",
                        _transport(lambda r: httpx.Response(404, json={})))
    session = provisioners.get_provisioner().status()
    assert session is not None and session.state == provisioners.ERROR
    assert SessionStore().load() is None


# ---- the HTTP surface ------------------------------------------------------
def test_session_route_reports_off_by_default(client):
    body = client.get("/api/remote-gpu/session").json()
    assert body["provisioner"] == "manual"
    assert body["running"] is False and body["state"] == "off"
    assert body["can_provision"] is False


def test_start_route_on_manual_explains_rather_than_500s(client):
    response = client.post("/api/remote-gpu/session")
    assert response.status_code == 400
    assert "REMOTE_GPU_PROVISIONER" in response.json()["detail"]


def test_stop_route_is_safe_with_nothing_running(client):
    assert client.request("DELETE", "/api/remote-gpu/session").status_code == 200
