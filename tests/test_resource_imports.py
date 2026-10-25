"""P07: provider imports from Hugging Face and Civitai, with fake transports.

No test reaches a real provider. Host names resolve through a fake table so
the address checks run for real, and downloads go through the same intake as
a local upload, so the bytes are real (tiny) safetensors files.
"""
from __future__ import annotations

import hashlib
import json
import logging
import struct
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend.app import db
from backend.app.config import settings
from backend.app.models import ResourceImport
from backend.app.problems import RequestError
from backend.app.resources import imports, providers, store

HF_TOKEN = "hf_SECRETTOKEN0123456789"
CIVITAI_TOKEN = "civitai_SECRETTOKEN0123"
COMMIT = "c0ffee" + "0" * 34
PUBLIC = "93.184.216.34"
ADDRESSES = {
    "huggingface.co": [PUBLIC], "cdn-lfs.huggingface.co": [PUBLIC], "civitai.com": [PUBLIC],
    "cdn.example-storage.com": [PUBLIC], "evil-internal.example": ["10.0.0.5"],
    "rebind.example": [PUBLIC, "127.0.0.1"], "cgnat.example": ["100.64.1.1"],
}


def lora(salt: int = 0, size: int = 64) -> bytes:
    header = {
        "diffusion_model.layers.0.attention.to_q.lora_A.weight":
            {"dtype": "F16", "shape": [size // 4], "data_offsets": [0, size // 2]},
        "diffusion_model.layers.0.adaLN_modulation.0.lora_B.weight":
            {"dtype": "F16", "shape": [size // 4], "data_offsets": [size // 2, size]},
    }
    blob = json.dumps(header).encode()
    data = bytearray(size)
    data[0] = salt
    return struct.pack("<Q", len(blob)) + blob + bytes(data)


def checkpoint() -> bytes:
    header = {"transformer_blocks.0.attn.to_q.weight": {"dtype": "F16", "shape": [4],
                                                         "data_offsets": [0, 8]},
              "time_embed.linear.weight": {"dtype": "F16", "shape": [4], "data_offsets": [8, 16]}}
    blob = json.dumps(header).encode()
    return struct.pack("<Q", len(blob)) + blob + bytes(16)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Provider:
    """A fake Hugging Face + Civitai + CDN, recording every request."""

    def __init__(self) -> None:
        self.routes: dict[str, httpx.Response | object] = {}
        self.requests: list[httpx.Request] = []
        self.files: dict[str, bytes] = {}
        self.honor_range = True
        self.executor: Any = None

    def json(self, url: str, body) -> None:
        self.routes[url] = httpx.Response(200, json=body)

    def redirect(self, url: str, to: str) -> None:
        self.routes[url] = httpx.Response(302, headers={"location": to})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in self.files:
            data = self.files[url]
            wanted = request.headers.get("range", "")
            if wanted and self.honor_range:
                start = int(wanted.removeprefix("bytes=").rstrip("-"))
                if start >= len(data):
                    return httpx.Response(416)
                return httpx.Response(206, content=data[start:], headers={
                    "content-range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
            return httpx.Response(200, content=data)
        route = self.routes.get(url) or self.routes.get(url.split("?")[0])
        if isinstance(route, httpx.Response):
            return route
        return httpx.Response(404, json={"error": "not here"})


class Deferred:
    """An executor the test drives: queued work runs when the test says so."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))

    def run_all(self) -> None:
        while self.calls:
            fn, args = self.calls.pop(0)
            fn(*args)


@pytest.fixture()
def provider(client, tmp_path, monkeypatch):
    fake = Provider()
    monkeypatch.setattr(providers, "transport", httpx.MockTransport(fake))
    monkeypatch.setattr(providers, "resolve_addresses", lambda host: ADDRESSES.get(host, []))
    monkeypatch.setattr(settings, "lora_dir", str(tmp_path / "loras"))
    monkeypatch.setattr(settings, "weights_dir", str(tmp_path / "weights"))
    monkeypatch.setattr(settings, "hf_home", str(tmp_path / "hf"))
    monkeypatch.setattr(settings, "hf_token", HF_TOKEN)
    monkeypatch.setattr(settings, "civitai_api_token", CIVITAI_TOKEN)
    executor = Deferred()
    monkeypatch.setattr(imports, "_executor", lambda: executor)
    fake.executor = executor
    return fake


def hf_repo(fake: Provider, repo: str, files: dict[str, bytes], *, license="apache-2.0",
            gated=False, wrong_sha: bool = False, extra: list | None = None) -> None:
    siblings = []
    for name, data in files.items():
        digest = "0" * 64 if wrong_sha else sha(data)
        siblings.append({"rfilename": name, "size": len(data),
                         "lfs": {"sha256": digest, "size": len(data)}})
        resolve = f"https://huggingface.co/{repo}/resolve/{COMMIT}/{name}"
        cdn = f"https://cdn-lfs.huggingface.co/{repo}/{name}"
        fake.redirect(resolve, f"{cdn}?X-Amz-Signature=SIGNED")
        fake.files[f"{cdn}?X-Amz-Signature=SIGNED"] = data
    siblings.extend(extra or [])
    body = {"id": repo, "sha": COMMIT, "gated": gated, "private": False,
            "cardData": {"license": license} if license else {}, "siblings": siblings}
    fake.json(f"https://huggingface.co/api/models/{repo}/revision/main?blobs=true", body)


def civitai_model(fake: Provider, *, data: bytes, model_type="LORA", pickle=False,
                  base="ZImageTurbo") -> None:
    files = [{"id": 501, "name": "style.safetensors", "sizeKB": len(data) / 1024,
              "type": "Model", "primary": True, "metadata": {"format": "SafeTensor"},
              "hashes": {"SHA256": sha(data).upper()},
              "downloadUrl": "https://civitai.com/api/download/models/41?type=Model&format=SafeTensor"}]
    if pickle:
        files.append({"id": 502, "name": "style.pt", "sizeKB": 1, "type": "Model",
                      "metadata": {"format": "PickleTensor"}, "hashes": {},
                      "downloadUrl": "https://civitai.com/api/download/models/41?type=Model&format=PickleTensor"})
    fake.json("https://civitai.com/api/v1/models/7", {
        "id": 7, "name": "Ink style", "type": model_type, "allowCommercialUse": ["Image"],
        "allowDerivatives": True, "allowNoCredit": False, "allowDifferentLicense": False,
        "modelVersions": [{"id": 41, "name": "v2", "baseModel": base, "files": files},
                          {"id": 40, "name": "v1", "baseModel": base, "files": []}]})
    fake.json("https://civitai.com/api/v1/model-versions/41", {"id": 41, "modelId": 7})
    fake.redirect("https://civitai.com/api/download/models/41?type=Model&format=SafeTensor",
                  "https://cdn.example-storage.com/blob/abc?token=SIGNED")
    fake.files["https://cdn.example-storage.com/blob/abc?token=SIGNED"] = data


def _row(task_id: int) -> ResourceImport:
    with db.session() as s:
        row = s.get(ResourceImport, task_id)
        assert row is not None
        s.expunge(row)
        return row


# ---- inspect ----------------------------------------------------------------------------------
def test_inspect_reads_metadata_only_and_pins_the_commit(client, provider):
    data = lora()
    hf_repo(provider, "org/ink", {"ink.safetensors": data}, extra=[
        {"rfilename": "old.ckpt", "size": 9}, {"rfilename": "README.md", "size": 3}])
    response = client.post("/api/resource-imports/inspect",
                           json={"link": "https://huggingface.co/org/ink"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == COMMIT and body["license"]["id"] == "apache-2.0"
    by_id = {c["id"]: c for c in body["candidates"]}
    assert by_id["file:ink.safetensors"]["sha256"] == sha(data)
    assert by_id["file:old.ckpt"]["supported"] is False and "pickle" in by_id["file:old.ckpt"]["reason"]
    assert body["default_selection"] == "file:ink.safetensors"
    assert all("/api/models/" in str(r.url) for r in provider.requests)   # no weight fetched


def test_ambiguity_is_presented_not_resolved(client, provider):
    hf_repo(provider, "org/two", {"a.safetensors": lora(1), "b.safetensors": lora(2)})
    info = client.post("/api/resource-imports/inspect", json={"link": "hf:org/two"}).json()
    assert info["selection_required"] is True and info["default_selection"] is None
    refused = client.post("/api/resource-imports", json={"link": "hf:org/two", "kind": "adapter"})
    assert refused.status_code == 400 and refused.json()["code"] == "selection_required"
    assert set(refused.json()["valid_options"]) == {"file:a.safetensors", "file:b.safetensors"}


# ---- Hugging Face file ------------------------------------------------------------------------
def test_a_hub_file_imports_through_intake_with_its_provenance(client, provider, caplog):
    caplog.set_level(logging.DEBUG)
    data = lora(3)
    hf_repo(provider, "org/ink", {"ink.safetensors": data})
    need_kind = client.post("/api/resource-imports", json={"link": "hf:org/ink"})
    assert need_kind.status_code == 400 and need_kind.json()["field"] == "kind"
    task = client.post("/api/resource-imports", json={"link": "hf:org/ink", "kind": "adapter"}).json()
    assert task["state"] == "queued"
    provider.executor.run_all()
    done = client.get(f"/api/resource-imports/{task['id']}").json()
    assert done["state"] == "done", (done["error_code"], done["error"])
    resource = store.get(done["resource_id"])
    assert resource.id == f"local:sha256:{sha(data)}" and resource.family == "zimage"
    assert resource.provider == "hf" and resource.revision == COMMIT
    assert resource.provider_ref == "org/ink/ink.safetensors"
    assert resource.license["id"] == "apache-2.0"
    # The token went to huggingface.co only, never to the CDN a redirect named.
    for request in provider.requests:
        sent = request.headers.get("authorization")
        if request.url.host == "huggingface.co":
            assert sent == f"Bearer {HF_TOKEN}"
        else:
            assert sent is None
    # Nothing a client or a log sees holds a token or a signed query.
    stored = json.dumps(_row(task["id"]).model_dump(mode="json"))
    shown = json.dumps(client.get("/api/resource-imports").json())
    for text in (stored, shown, caplog.text):
        assert HF_TOKEN not in text and "SIGNED" not in text


def test_a_checksum_mismatch_discards_the_file(client, provider):
    hf_repo(provider, "org/ink", {"ink.safetensors": lora(4)}, wrong_sha=True)
    task = client.post("/api/resource-imports", json={"link": "hf:org/ink", "kind": "adapter"}).json()
    provider.executor.run_all()
    failed = imports.get(task["id"])
    assert failed["state"] == "failed" and failed["error_code"] == "hash_mismatch", failed["error"]
    assert failed["resource_id"] is None
    assert not list(Path(settings.loras_dir).rglob("*.safetensors"))
    assert not list((Path(settings.loras_dir) / ".incoming").glob("*.part"))


def test_a_checkpoint_offered_as_an_adapter_is_refused_by_intake(client, provider):
    hf_repo(provider, "org/base", {"base.safetensors": checkpoint()})
    task = client.post("/api/resource-imports", json={"link": "hf:org/base", "kind": "adapter"}).json()
    provider.executor.run_all()
    assert imports.get(task["id"])["error_code"] == "not_an_adapter"


def test_pickle_and_unsupported_selections_are_refused(client, provider):
    hf_repo(provider, "org/ink", {"ink.safetensors": lora(5)},
            extra=[{"rfilename": "model.ckpt", "size": 9}])
    refused = client.post("/api/resource-imports", json={
        "link": "hf:org/ink", "selection": "file:model.ckpt", "kind": "model"})
    assert refused.status_code == 400 and refused.json()["code"] == "unsafe_format"


def test_gated_access_needs_a_token(client, provider, monkeypatch):
    hf_repo(provider, "org/gated", {"g.safetensors": lora(6)}, gated="manual")
    monkeypatch.setattr(settings, "hf_token", "")
    refused = client.post("/api/resource-imports", json={"link": "hf:org/gated", "kind": "adapter"})
    assert refused.status_code == 403 and refused.json()["code"] == "access_required"
    provider.routes["https://huggingface.co/api/models/org/locked/revision/main?blobs=true"] = \
        httpx.Response(401, json={})
    locked = client.post("/api/resource-imports/inspect", json={"link": "hf:org/locked"})
    assert locked.status_code == 403 and locked.json()["code"] == "access_required"


def test_insufficient_disk_refuses_before_queueing(client, provider, monkeypatch):
    hf_repo(provider, "org/ink", {"ink.safetensors": lora(7)})

    class Usage:
        free = 10

    monkeypatch.setattr(imports.shutil, "disk_usage", lambda _p: Usage())
    refused = client.post("/api/resource-imports", json={"link": "hf:org/ink", "kind": "adapter"})
    assert refused.status_code == 507 and refused.json()["code"] == "insufficient_disk"
    assert provider.executor.calls == []


def test_content_already_installed_is_not_fetched_again(client, provider):
    data = lora(8)
    hf_repo(provider, "org/ink", {"ink.safetensors": data})
    first = client.post("/api/resource-imports", json={"link": "hf:org/ink", "kind": "adapter"}).json()
    provider.executor.run_all()
    fetched = len(provider.requests)
    again = client.post("/api/resource-imports", json={"link": "hf:org/ink", "kind": "adapter"}).json()
    assert again["state"] == "done" and again["error_code"] == "already_present"
    assert again["resource_id"] == imports.get(first["id"])["resource_id"]
    assert len(provider.requests) == fetched + 1          # one more inspect, no download


# ---- cancel, resume, restart ------------------------------------------------------------------
def test_a_queued_import_cancels_without_fetching(client, provider):
    hf_repo(provider, "org/ink", {"ink.safetensors": lora(9)})
    task = client.post("/api/resource-imports", json={"link": "hf:org/ink", "kind": "adapter"}).json()
    assert client.post(f"/api/resource-imports/{task['id']}/cancel").json()["state"] == "canceled"
    provider.executor.run_all()
    assert not any("cdn-lfs" in str(r.url) for r in provider.requests)


def test_a_running_import_cancels_and_leaves_no_partial(client, provider, monkeypatch):
    data = lora(10, size=4096)
    hf_repo(provider, "org/big", {"big.safetensors": data})
    monkeypatch.setattr(providers, "CHUNK", 256)
    task = client.post("/api/resource-imports", json={"link": "hf:org/big", "kind": "adapter"}).json()

    def cancel_on_first_chunk(task_id, total):
        def report(done):
            imports._CANCEL.add(task_id)
        return report

    monkeypatch.setattr(imports, "_progress", cancel_on_first_chunk)
    provider.executor.run_all()
    assert imports.get(task["id"])["state"] == "canceled"
    assert not list((Path(settings.loras_dir) / ".incoming").glob("*.part"))


@pytest.mark.parametrize("honor_range", [True, False])
def test_an_interrupted_import_resumes_or_restarts_cleanly(client, provider, honor_range):
    data = lora(11, size=2048)
    hf_repo(provider, "org/ink", {"ink.safetensors": data})
    provider.honor_range = honor_range
    task = client.post("/api/resource-imports", json={"link": "hf:org/ink", "kind": "adapter"}).json()
    provider.executor.calls.clear()
    # The process died mid-download: part of the file is on disk.
    imports._update(task["id"], state="running")
    part = Path(settings.loras_dir) / ".incoming" / f"import-{task['id']}.part"
    part.write_bytes(data[:700])
    assert imports.recover_on_startup() >= 1
    assert imports.get(task["id"])["state"] == "interrupted"
    client.post(f"/api/resource-imports/{task['id']}/retry")
    provider.executor.run_all()
    done = imports.get(task["id"])
    assert done["state"] == "done", done
    ranged = [r for r in provider.requests if r.headers.get("range")]
    assert ranged and ranged[-1].headers["range"] == "bytes=700-"
    assert store.get(done["resource_id"]).content_hash == f"sha256:{sha(data)}"


def test_a_task_running_at_shutdown_is_never_silently_done(client, provider):
    hf_repo(provider, "org/ink", {"ink.safetensors": lora(12)})
    task = client.post("/api/resource-imports", json={"link": "hf:org/ink", "kind": "adapter"}).json()
    imports._update(task["id"], state="running")
    imports.recover_on_startup()
    view = imports.get(task["id"])
    assert view["state"] == "interrupted" and view["retryable"] is True


# ---- Civitai ----------------------------------------------------------------------------------
def test_civitai_needs_a_licence_acknowledgement_and_records_the_permissions(client, provider):
    data = lora(13)
    civitai_model(provider, data=data, pickle=True)
    info = client.post("/api/resource-imports/inspect",
                       json={"link": "https://civitai.com/models/7/ink?modelVersionId=41"}).json()
    by_id = {c["id"]: c for c in info["candidates"]}
    assert by_id["version:41/file:501"]["family"] == "zimage"
    assert by_id["version:41/file:502"]["supported"] is False
    assert info["license_needs_acknowledgement"] is True
    link = "https://civitai.com/models/7?modelVersionId=41"
    refused = client.post("/api/resource-imports", json={"link": link,
                                                         "selection": "version:41/file:501"})
    assert refused.status_code == 409 and refused.json()["code"] == "license_unreviewed"
    task = client.post("/api/resource-imports", json={
        "link": link, "selection": "version:41/file:501", "acknowledge_license": True}).json()
    provider.executor.run_all()
    done = imports.get(task["id"])
    assert done["state"] == "done", done
    resource = store.get(done["resource_id"])
    assert resource.provider == "civitai" and resource.provider_ref == "model/7/version/41/file/501"
    assert resource.license["commercial_use"] == ["Image"] and resource.license["acknowledged_at"]
    cdn = [r for r in provider.requests if r.url.host == "cdn.example-storage.com"]
    assert cdn and all(r.headers.get("authorization") is None for r in cdn)
    assert any(r.headers.get("authorization") == f"Bearer {CIVITAI_TOKEN}"
               for r in provider.requests if r.url.host == "civitai.com")
    shown = json.dumps(done, default=str)
    assert CIVITAI_TOKEN not in shown and "SIGNED" not in shown


def test_civitai_pickle_files_and_other_types_are_refused(client, provider):
    civitai_model(provider, data=lora(14), pickle=True)
    pickled = client.post("/api/resource-imports", json={
        "link": "civitai:model/7/version/41/file/502", "acknowledge_license": True})
    assert pickled.status_code == 400 and pickled.json()["code"] == "unsafe_format"


def test_civitai_base_models_map_to_families():
    assert providers.civitai_family("SDXL 1.0") == "sdxl"
    assert providers.civitai_family("Pony") == "sdxl"
    assert providers.civitai_family("Flux.1 D") == "flux"
    assert providers.civitai_family("Wan Video 14B t2v") == "wan"
    assert providers.civitai_family("Something New") == "unknown"


# ---- network safety ---------------------------------------------------------------------------
@pytest.mark.parametrize("target,code", [
    ("http://cdn-lfs.huggingface.co/x", "insecure_url"),
    ("https://127.0.0.1/x", "unsafe_destination"),
    ("https://169.254.169.254/latest/meta-data/", "unsafe_destination"),
    ("https://metadata.google.internal/computeMetadata/v1/", "unsafe_destination"),
    ("https://evil-internal.example/x", "unsafe_destination"),
    ("https://rebind.example/x", "unsafe_destination"),
    ("https://cgnat.example/x", "unsafe_destination"),
    ("https://[::1]/x", "unsafe_destination"),
])
def test_every_redirect_hop_is_checked(client, provider, target, code):
    url = "https://huggingface.co/api/models/org/evil/revision/main?blobs=true"
    provider.redirect(url, target)
    with pytest.raises(RequestError) as excinfo:
        providers.inspect("hf:org/evil")
    assert excinfo.value.code == code
    assert all(r.url.host == "huggingface.co" for r in provider.requests)


def test_http_links_and_unknown_hosts_are_refused_up_front():
    with pytest.raises(RequestError) as excinfo:
        providers.parse("http://huggingface.co/org/ink")
    assert excinfo.value.code == "insecure_url"
    with pytest.raises(RequestError) as excinfo:
        providers.parse("https://example.com/model.safetensors")
    assert excinfo.value.code == "unsupported_link"
    with pytest.raises(RequestError):
        providers.parse("https://huggingface.co/org/ink/blob/main/../../etc/passwd")


def test_oversized_answers_are_refused(client, provider, monkeypatch):
    monkeypatch.setattr(providers, "MAX_JSON_BYTES", 64)
    hf_repo(provider, "org/ink", {"ink.safetensors": lora(15)})
    with pytest.raises(RequestError) as excinfo:
        providers.inspect("hf:org/ink")
    assert excinfo.value.code == "provider_error"


def test_a_download_larger_than_published_is_refused(client, provider):
    data = lora(16, size=512)
    hf_repo(provider, "org/ink", {"ink.safetensors": data})
    task = client.post("/api/resource-imports", json={"link": "hf:org/ink", "kind": "adapter"}).json()
    cdn = "https://cdn-lfs.huggingface.co/org/ink/ink.safetensors?X-Amz-Signature=SIGNED"
    provider.files[cdn] = data + b"x" * 4096
    provider.executor.run_all()
    assert imports.get(task["id"])["error_code"] == "too_large"


# ---- Diffusers repositories -------------------------------------------------------------------
def test_a_diffusers_repository_imports_at_its_commit(client, provider, monkeypatch, tmp_path):
    unet, encoder = lora(17), lora(18)
    files = {"model_index.json": b"{}", "unet/config.json": b"{}",
             "unet/diffusion_pytorch_model.safetensors": unet,
             "text_encoder/model.safetensors": encoder}
    hf_repo(provider, "org/pipe", files, license="")
    info = providers.inspect("hf:org/pipe")
    repo = next(c for c in info["candidates"] if c["id"] == "repo")
    assert repo["supported"] is True and repo["kind"] == "diffusers"
    fetched: list[tuple] = []

    def fake_download(repo_id, filename, *, revision, cache_dir, token):
        fetched.append((repo_id, filename, revision, token))
        path = tmp_path / "hub" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[filename])
        return str(path)

    monkeypatch.setattr(imports, "hf_hub_download", fake_download)
    task = client.post("/api/resource-imports", json={
        "link": "hf:org/pipe", "selection": "repo", "acknowledge_license": True}).json()
    provider.executor.run_all()
    done = imports.get(task["id"])
    assert done["state"] == "done", done
    assert done["resource_id"] == f"hf:org/pipe@{COMMIT}"
    assert {f[2] for f in fetched} == {COMMIT}
    resource = store.get(done["resource_id"])
    assert resource.verification == "verified" and resource.format == "diffusers"
    assert store.install_at("local", "hf:org/pipe") is not None
    assert all(f[3] == HF_TOKEN for f in fetched) and HF_TOKEN not in json.dumps(done, default=str)


def test_a_repository_with_pickle_only_components_is_unsupported(client, provider):
    hf_repo(provider, "org/old", {"model_index.json": b"{}", "unet/config.json": b"{}"},
            extra=[{"rfilename": "unet/diffusion_pytorch_model.bin", "size": 9}])
    repo = next(c for c in providers.inspect("hf:org/old")["candidates"] if c["id"] == "repo")
    assert repo["supported"] is False and "unet" in repo["reason"]


def test_a_lost_insert_race_retries_as_an_update(client, monkeypatch):
    """A library scan and an import can register the same new file at once."""
    from sqlalchemy.exc import IntegrityError

    real = store._upsert
    calls = {"n": 0}

    def racing(resource_id, facts):
        calls["n"] += 1
        if calls["n"] == 1:
            real(resource_id, {"label": "the scan got here first"})
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
        return real(resource_id, facts)

    monkeypatch.setattr(store, "_upsert", racing)
    row = store.upsert(f"local:sha256:{'e' * 64}", kind="adapter", label="the import")
    assert calls["n"] == 2 and row.label == "the import" and row.kind == "adapter"
