"""P09: preparing a resource on the Remote GPU worker.

The worker side is the real route code (worker.resources.install_routes) on a
bare FastAPI app, with no torch; the app side is resources.remote, talking to
it through a TestClient as its transport. Only bytes and a hash cross between
them: the tests check that no local path is ever sent.
"""
from __future__ import annotations

import hashlib
import json
import struct

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app import remote_gpu_client
from backend.app.config import settings
from backend.app.resources import catalogue, remote, store
from worker import resources as worker_store

SECRET = "worker-test-secret-1234567890"


def _lora(salt: int) -> bytes:
    header = {
        "diffusion_model.layers.0.attention.to_q.lora_A.weight":
            {"dtype": "F16", "shape": [64, 64], "data_offsets": [0, 8192]},
        "diffusion_model.layers.0.adaLN_modulation.0.lora_B.weight":
            {"dtype": "F16", "shape": [64, 64], "data_offsets": [8192, 16384]},
    }
    blob = json.dumps(header).encode()
    data = bytearray(16384)
    data[0] = salt
    return struct.pack("<Q", len(blob)) + blob + bytes(data)


@pytest.fixture()
def worker(tmp_path):
    app = FastAPI()
    seen: list[httpx.Request] = []

    def auth(secret):
        if secret != SECRET:
            raise HTTPException(status_code=401, detail="bad or missing X-Gen-Secret")

    worker_store.install_routes(app, worker_store.Store(tmp_path / "worker" / "resources",
                                                       disk_margin=0), auth)
    return app, seen


@pytest.fixture()
def wired(client, worker, tmp_path, monkeypatch):
    """The app pointed at the in-process worker, with an imported adapter."""
    app, seen = worker
    monkeypatch.setattr(settings, "lora_dir", str(tmp_path / "loras"))
    (tmp_path / "loras").mkdir()
    data = _lora(1)
    (tmp_path / "loras" / "ink.safetensors").write_bytes(data)
    catalogue.scan_library()
    rid = f"local:sha256:{hashlib.sha256(data).hexdigest()}"
    monkeypatch.setattr(remote_gpu_client, "connection", lambda: ("http://worker.test", SECRET))
    monkeypatch.setitem(remote_gpu_client._LAST_HEALTH, "connected", True)
    monkeypatch.setitem(remote_gpu_client._LAST_HEALTH, "features", ["resources"])
    monkeypatch.setitem(remote_gpu_client._LAST_HEALTH, "build", "abc123def456")

    class Recording(TestClient):
        def request(self, method, url, **kwargs):
            seen.append(httpx.Request(method, f"http://worker.test{url}",
                                      params=kwargs.get("params"),
                                      content=kwargs.get("content") or (
                                          json.dumps(kwargs["json"]).encode()
                                          if kwargs.get("json") else b"")))
            return super().request(method, url, **kwargs)

    def factory(*, base_url, headers, timeout):
        return Recording(app, base_url=base_url, headers=headers)

    monkeypatch.setattr(remote.httpx, "Client", factory)
    return {"rid": rid, "data": data, "seen": seen, "app": app, "root": tmp_path}


def test_an_adapter_is_transferred_by_content_and_recorded(wired):
    result = remote.prepare(wired["rid"])
    assert result["state"] == "ready"
    stored = wired["root"] / "worker" / "resources"
    digest = wired["rid"].rsplit(":", 1)[1]
    assert (stored / digest[:2] / f"{digest}.safetensors").read_bytes() == wired["data"]
    install = next(i for i in store.installs(wired["rid"]) if i.target.startswith("remote"))
    assert install.location == f"remote:sha256:{digest}" and install.state == "verified"
    assert install.target == "remote:abc123def456"
    # Only the hash and the bytes crossed: never where the file lives here.
    local = str(settings.loras_dir)
    for request in wired["seen"]:
        assert local not in str(request.url) and local.encode() not in request.content


def test_a_second_preparation_sends_no_bytes(wired):
    remote.prepare(wired["rid"])
    wired["seen"].clear()
    assert remote.prepare(wired["rid"])["state"] == "already_present"
    assert [r.method for r in wired["seen"]] == ["POST"]


def test_an_interrupted_transfer_resumes_at_the_workers_offset(wired, monkeypatch):
    monkeypatch.setattr(remote, "CHUNK_BYTES", 4096)
    calls = {"n": 0}
    real = remote._send

    def flaky(client, sha256, offset, data):
        calls["n"] += 1
        if calls["n"] == 3:
            raise remote.RequestError(503, "dropped", code="target_unavailable",
                                      retryable=True)
        return real(client, sha256, offset, data)

    monkeypatch.setattr(remote, "_send", flaky)
    with pytest.raises(remote.RequestError):
        remote.prepare(wired["rid"])
    monkeypatch.setattr(remote, "_send", real)
    wired["seen"].clear()
    assert remote.prepare(wired["rid"])["state"] == "ready"
    first_chunk = next(r for r in wired["seen"] if r.method == "PUT")
    assert int(first_chunk.url.params["offset"]) == 8192         # resumed, not restarted


def test_bytes_that_do_not_match_their_hash_are_discarded(wired):
    # Its own adapter: the module shares one database, and an earlier test
    # may already have prepared the fixture's.
    data = _lora(77)
    path = wired["root"] / "loras" / "corrupted.safetensors"
    path.write_bytes(data)
    catalogue.scan_library()
    rid = f"local:sha256:{hashlib.sha256(data).hexdigest()}"
    path.write_bytes(data[:-1] + b"\x07")                        # changed after the scan
    with pytest.raises(remote.RequestError) as excinfo:
        remote.prepare(rid)
    assert excinfo.value.code == "hash_mismatch"
    assert not any(i.target.startswith("remote") for i in store.installs(rid))


def test_cancel_removes_the_partial(wired, monkeypatch):
    monkeypatch.setattr(remote, "CHUNK_BYTES", 4096)
    with pytest.raises(remote.RequestError) as excinfo:
        remote.prepare(wired["rid"], cancel=lambda: True)
    assert excinfo.value.code == "canceled"
    assert not list((wired["root"] / "worker" / "resources" / ".partial").glob("*.part"))


def test_an_old_worker_or_a_hub_model_is_refused_with_a_reason(wired, monkeypatch):
    monkeypatch.setitem(remote_gpu_client._LAST_HEALTH, "features", ["preview"])
    with pytest.raises(remote.RequestError) as excinfo:
        remote.prepare(wired["rid"])
    assert excinfo.value.code == "unsupported_protocol"
    store.upsert("hf:example/model@0123", kind="model", provider="hf")
    with pytest.raises(remote.RequestError) as excinfo:
        remote.prepare("hf:example/model@0123")
    assert excinfo.value.code == "unsupported_source"


def test_the_shared_secret_never_reaches_an_error(wired, monkeypatch):
    def leaky(*_a, **_k):
        raise httpx.ConnectError(f"failed with header {SECRET}")

    monkeypatch.setattr(remote_gpu_client, "_base_and_secret", lambda: ("http://w", SECRET))

    class Broken:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        request = staticmethod(leaky)

    monkeypatch.setattr(remote.httpx, "Client", Broken)
    with pytest.raises(remote.RequestError) as excinfo:
        remote.prepare(wired["rid"])
    assert SECRET not in str(excinfo.value.detail)


# ---- the worker store on its own -------------------------------------------------------------
def test_the_worker_store_refuses_bad_hashes_kinds_sizes_and_disk(tmp_path, monkeypatch):
    s = worker_store.Store(tmp_path / "r", max_bytes=1000)
    for bad in ("../../etc/passwd", "A" * 64, "f" * 63):
        with pytest.raises(worker_store.StoreError) as excinfo:
            s.ensure(bad, 10, "adapter")
        assert excinfo.value.code == "invalid_identifier"
    with pytest.raises(worker_store.StoreError) as excinfo:
        s.ensure("a" * 64, 10, "checkpoint")
    assert excinfo.value.code == "unsupported_kind"
    with pytest.raises(worker_store.StoreError) as excinfo:
        s.ensure("a" * 64, 5000, "adapter")
    assert excinfo.value.code == "too_large"

    class Usage:
        free = 1

    monkeypatch.setattr(worker_store.shutil, "disk_usage", lambda _p: Usage())
    with pytest.raises(worker_store.StoreError) as excinfo:
        s.ensure("a" * 64, 10, "adapter")
    assert excinfo.value.status == 507


def test_the_worker_store_reports_where_to_resume(tmp_path):
    data = b"x" * 300
    sha = hashlib.sha256(data).hexdigest()
    s = worker_store.Store(tmp_path / "r", disk_margin=0)
    assert s.ensure(sha, len(data), "adapter")["received"] == 0
    s.append(sha, 0, data[:100])
    with pytest.raises(worker_store.StoreError) as excinfo:
        s.append(sha, 0, data[:100])
    assert excinfo.value.state == {"received": 100}
    assert s.ensure(sha, len(data), "adapter")["received"] == 100
    s.append(sha, 100, data[100:])
    assert s.complete(sha)["state"] == "ready"
    assert s.manifest()[0]["sha256"] == sha and s.path_for(sha).read_bytes() == data


def test_the_worker_routes_need_the_secret(worker):
    app, _seen = worker
    client = TestClient(app)
    assert client.get("/resources/manifest").status_code == 401
    assert client.get("/resources/manifest", headers={"X-Gen-Secret": SECRET}).json() == {
        "resources": []}
