"""P06: the resource catalogue and safe local intake.

Fixtures build real (tiny) safetensors files, so the structural checks, the
family probe and content hashing all run for real. Both managed roots point
at a per-test directory: nothing here may touch a real model library.
"""
from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from backend.app.config import settings
from backend.app.resources import catalogue, intake, store
from backend.app.studio.contracts import resource_id

ZIMAGE_LORA = {
    "diffusion_model.layers.0.attention.to_q.lora_A.weight": ("F16", [4, 8]),
    "diffusion_model.layers.0.attention.to_q.lora_B.weight": ("F16", [8, 4]),
    "diffusion_model.layers.0.adaLN_modulation.0.lora_A.weight": ("F16", [4, 8]),
}
SDXL_LORA = {
    "model.diffusion_model.input_blocks.1.0.lora_down.weight": ("F16", [4, 8]),
    "model.diffusion_model.middle_block.1.lora_up.weight": ("F16", [8, 4]),
    "model.diffusion_model.label_emb.0.lora_down.weight": ("F16", [4, 8]),
}
ZIMAGE_CHECKPOINT = {
    "transformer_blocks.0.attn.to_q.weight": ("F16", [8, 8]),
    "time_embed.linear.weight": ("F16", [8, 8]),
}


def safetensors(tensors: dict, *, metadata: dict | None = None, salt: int = 0) -> bytes:
    header: dict = {}
    offset = 0
    for name, (dtype, shape) in tensors.items():
        count = 1
        for dim in shape:
            count *= dim
        size = count * {"F16": 2, "F32": 4}[dtype]
        header[name] = {"dtype": dtype, "shape": shape, "data_offsets": [offset, offset + size]}
        offset += size
    if metadata:
        header["__metadata__"] = metadata
    blob = json.dumps(header).encode()
    data = bytearray(offset)
    if data:
        data[0] = salt % 256
    return struct.pack("<Q", len(blob)) + blob + bytes(data)


@pytest.fixture()
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lora_dir", str(tmp_path / "loras"))
    monkeypatch.setattr(settings, "weights_dir", str(tmp_path / "weights"))
    (tmp_path / "loras").mkdir()
    (tmp_path / "weights").mkdir()
    return tmp_path


def upload(client, name: str, data: bytes, kind: str = "adapter", chunk: int = 7, **extra):
    session = client.post("/api/resources/uploads",
                          json={"filename": name, "size": len(data), "kind": kind, **extra})
    if session.status_code != 200:
        return session
    upload_id = session.json()["upload_id"]
    for offset in range(0, len(data), chunk):
        response = client.put(f"/api/resources/uploads/{upload_id}", params={"offset": offset},
                              content=data[offset:offset + chunk])
        assert response.status_code == 200, response.text
    done = client.post(f"/api/resources/uploads/{upload_id}/complete")
    if done.status_code != 200:
        return done
    return client.post("/api/resources/import", json={"receipt": upload_id})


def _rid(data: bytes) -> str:
    return resource_id("local", hashlib.sha256(data).hexdigest())


# ---- import -------------------------------------------------------------------------------
def test_an_adapter_upload_imports_with_its_content_identity(client, roots):
    data = safetensors(ZIMAGE_LORA, metadata={"modelspec.license": "apache-2.0"})
    response = upload(client, "Ink wash.safetensors", data)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "imported"
    resource = body["resource"]
    assert resource["id"] == _rid(data) and resource["kind"] == "adapter"
    assert resource["family"] == "zimage" and resource["verification"] == "verified"
    assert resource["compat"]["evidence"] == "tensor-names"
    assert resource["license"] == {"id": "apache-2.0", "source": "file metadata",
                                   "reviewed": False}
    assert resource["installs"] == [{"target": "local", "root": "loras",
                                     "path": "imported/Ink wash.safetensors", "state": "verified",
                                     "size_bytes": len(data), "error": ""}]
    # Managed paths stay private: no absolute path in what a client sees.
    assert str(roots) not in response.text
    assert (roots / "loras" / "imported" / "Ink wash.safetensors").read_bytes() == data
    assert not list((roots / "loras" / ".incoming").iterdir())


def test_duplicate_content_is_one_resource(client, roots):
    data = safetensors(ZIMAGE_LORA)
    assert upload(client, "a.safetensors", data).json()["status"] == "imported"
    again = upload(client, "renamed copy.safetensors", data).json()
    assert again["status"] == "already_present" and again["resource"]["id"] == _rid(data)
    assert len(list((roots / "loras" / "imported").iterdir())) == 1
    # A second copy dropped into the library by hand is a second install.
    (roots / "loras" / "by hand.safetensors").write_bytes(data)
    catalogue.scan_library()
    installs = store.installs(_rid(data))
    assert sorted(i.location for i in installs) == ["loras:by hand.safetensors",
                                                    "loras:imported/a.safetensors"]
    assert len([r for r in store.listing("adapter") if r.content_hash.endswith(
        hashlib.sha256(data).hexdigest())]) == 1


@pytest.mark.parametrize("name,code", [
    ("model.ckpt", "unsafe_format"), ("model.pt", "unsafe_format"),
    ("pytorch_model.bin", "unsafe_format"), ("weights.pkl", "unsafe_format"),
    ("bundle.zip", "unsupported_format"), ("notes.txt", "unsupported_format"),
])
def test_unsafe_and_unsupported_formats_refuse_before_any_bytes(client, roots, name, code):
    response = client.post("/api/resources/uploads",
                           json={"filename": name, "size": 10, "kind": "adapter"})
    assert response.status_code == 400 and response.json()["code"] == code


def _header_not_an_object(data: bytes) -> bytes:
    length = int.from_bytes(data[:8], "little")
    return data[:8] + b"[1, 2, 3]".ljust(length) + data[8 + length:]


@pytest.mark.parametrize("mutate", [
    lambda b: b[:6],                                                    # truncated
    lambda b: struct.pack("<Q", 10**9) + b[8:],                         # absurd header length
    _header_not_an_object,
])
def test_structurally_invalid_files_are_refused_and_leave_nothing(client, roots, mutate):
    data = mutate(safetensors(ZIMAGE_LORA))
    response = upload(client, "broken.safetensors", data)
    assert response.status_code == 400 and response.json()["code"] == "invalid_safetensors"
    assert not (roots / "loras" / "imported").exists() or \
        not list((roots / "loras" / "imported").iterdir())
    assert not [p for p in (roots / "loras" / ".incoming").iterdir() if p.suffix == ".part"]


def test_offsets_outside_the_file_and_overlaps_are_refused(roots):
    def raw(header: dict, data_len: int) -> Path:
        blob = json.dumps(header).encode()
        path = roots / "loras" / "x.safetensors"
        path.write_bytes(struct.pack("<Q", len(blob)) + blob + bytes(data_len))
        return path

    outside = raw({"a.lora_A.weight": {"dtype": "F16", "shape": [2], "data_offsets": [0, 400]}}, 4)
    with pytest.raises(Exception, match="outside"):
        intake.validate_safetensors(outside)
    overlap = raw({"a.lora_A.weight": {"dtype": "F16", "shape": [2], "data_offsets": [0, 4]},
                   "b.lora_B.weight": {"dtype": "F16", "shape": [2], "data_offsets": [2, 6]}}, 8)
    with pytest.raises(Exception, match="overlap"):
        intake.validate_safetensors(overlap)
    wrong_size = raw({"a.lora_A.weight": {"dtype": "F32", "shape": [3], "data_offsets": [0, 4]}}, 4)
    with pytest.raises(Exception, match="size does not match"):
        intake.validate_safetensors(wrong_size)


def test_a_checkpoint_is_not_an_adapter_and_an_adapter_is_not_a_model(client, roots):
    checkpoint = upload(client, "base.safetensors", safetensors(ZIMAGE_CHECKPOINT))
    assert checkpoint.status_code == 400 and checkpoint.json()["code"] == "not_an_adapter"
    adapter = upload(client, "style.safetensors", safetensors(ZIMAGE_LORA), kind="model")
    assert adapter.status_code == 400 and adapter.json()["code"] == "not_a_model"


def test_a_checkpoint_imports_as_a_model_that_is_not_yet_loadable(client, roots):
    data = safetensors(ZIMAGE_CHECKPOINT)
    resource = upload(client, "base.safetensors", data, kind="model").json()["resource"]
    assert resource["kind"] == "model" and resource["format"] == "single_file"
    assert resource["installs"][0]["root"] == "weights"
    assert "not loadable" in resource["error"]
    assert (roots / "weights" / "checkpoints" / "base.safetensors").exists()


def test_a_declared_hash_that_does_not_match_discards_the_upload(client, roots):
    data = safetensors(ZIMAGE_LORA)
    response = upload(client, "a.safetensors", data, sha256="0" * 64)
    assert response.status_code == 400 and response.json()["code"] == "hash_mismatch"
    assert not list((roots / "loras" / ".incoming").iterdir())


def test_a_wrong_offset_says_where_to_resume(client, roots):
    data = safetensors(ZIMAGE_LORA)
    session = client.post("/api/resources/uploads", json={
        "filename": "a.safetensors", "size": len(data), "kind": "adapter"}).json()
    url = f"/api/resources/uploads/{session['upload_id']}"
    client.put(url, params={"offset": 0}, content=data[:5])
    stale = client.put(url, params={"offset": 0}, content=data[:5])
    assert stale.status_code == 409 and stale.json()["state"]["received"] == 5
    incomplete = client.post(f"{url}/complete")
    assert incomplete.status_code == 409 and incomplete.json()["code"] == "upload_incomplete"
    assert client.delete(url).json() == {"ok": True}
    assert client.put(url, params={"offset": 5}, content=b"x").status_code == 404


def test_insufficient_disk_refuses_before_receiving(client, roots, monkeypatch):
    class Usage:
        free = 10

    monkeypatch.setattr(intake.shutil, "disk_usage", lambda _p: Usage())
    response = client.post("/api/resources/uploads",
                           json={"filename": "a.safetensors", "size": 100, "kind": "adapter"})
    assert response.status_code == 507 and response.json()["code"] == "insufficient_disk"


# ---- the library --------------------------------------------------------------------------
def test_a_missing_adapter_stays_visible(client, roots):
    data = safetensors(SDXL_LORA)
    path = roots / "loras" / "sdxl style.safetensors"
    path.write_bytes(data)
    catalogue.scan_library()
    rid = _rid(data)
    assert store.get(rid).family == "sdxl"
    path.unlink()
    counts = catalogue.scan_library()
    assert counts["missing"] == 1
    shown = client.get(f"/api/resources/{rid}").json()
    assert shown["state"] == "unavailable" and shown["installs"][0]["state"] == "missing"
    assert any(r["id"] == rid for r in client.get("/api/resources?kind=adapter").json())


def test_the_lora_listing_carries_resource_ids(client, roots):
    data = safetensors(ZIMAGE_LORA)
    (roots / "loras" / "named.safetensors").write_bytes(data)
    items = client.get("/api/loras").json()["items"]
    assert items[0]["resource_id"] is None         # listed, not verified yet
    catalogue.scan_library()
    items = client.get("/api/loras").json()["items"]
    assert items[0]["resource_id"] == _rid(data)


def test_a_bounded_scan_defers_the_rest(roots):
    for index in range(3):
        (roots / "loras" / f"{index}.safetensors").write_bytes(safetensors(ZIMAGE_LORA, salt=index))
    size = (roots / "loras" / "0.safetensors").stat().st_size
    counts = catalogue.scan_library(budget_bytes=size)
    assert counts["hashed"] == 1 and counts["deferred"] == 2
    assert catalogue.scan_library()["hashed"] == 2
    assert catalogue.scan_library()["unchanged"] == 3


def test_configured_models_register_pinned_and_unverified(roots, monkeypatch):
    monkeypatch.setattr(settings, "local_image_model_sdxl", "example-org/custom-sdxl")
    catalogue.register_configured_models()
    turbo = store.get("hf:Tongyi-MAI/Z-Image-Turbo@f332072aa78be7aecdf3ee76d5c247082da564a6")
    assert turbo is not None and turbo.verification == "legacy_unverified"
    assert {"lane": "local", "role": "image", "variant": "turbo"} in turbo.constraints["slots"]
    custom = store.get("hf:example-org/custom-sdxl@unpinned")
    assert custom is not None and custom.revision == "" and custom.family == "sdxl"


# ---- paths --------------------------------------------------------------------------------
@pytest.mark.parametrize("given,expected", [
    ("C:\\Users\\me\\My LoRA.safetensors", "My LoRA.safetensors"),
    ("../../escape.safetensors", "escape.safetensors"),
    ("con.safetensors", "_con.safetensors"),
    ("NUL.safetensors", "_NUL.safetensors"),
    ("a<b>:c|d?.safetensors", "abcd.safetensors"),
    ("trailing dots... .safetensors", "trailing dots.safetensors"),
    ("名前 ✨ style.safetensors", "名前 ✨ style.safetensors"),
    ("x" * 300 + ".SAFETENSORS", "x" * 108 + ".safetensors"),
])
def test_filenames_are_safe_on_linux_and_windows(given, expected):
    assert intake.safe_filename(given) == expected


def test_locations_never_resolve_outside_their_root(roots, tmp_path):
    outside = tmp_path / "outside.safetensors"
    outside.write_bytes(b"x")
    assert catalogue.path_for("loras:../outside.safetensors") is None
    assert catalogue.path_for("loras:/etc/passwd") is None
    assert catalogue.path_for("elsewhere:x.safetensors") is None
    link = roots / "loras" / "link.safetensors"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available here")
    assert catalogue.path_for("loras:link.safetensors") is None


def test_unicode_and_spaces_survive_import_and_lookup(client, roots):
    data = safetensors(ZIMAGE_LORA, salt=9)
    body = upload(client, "Fusée d'été 🚀.safetensors", data).json()
    path = catalogue.local_file(body["resource"]["id"])
    assert path is not None and path.name == "Fusée d'été 🚀.safetensors"
    assert catalogue.library_path(body["resource"]["id"]) == "imported/Fusée d'été 🚀.safetensors"


def test_a_local_path_import_copies_and_leaves_the_source(roots, tmp_path):
    data = safetensors(ZIMAGE_LORA, salt=3)
    source = tmp_path / "downloads" / "mine.safetensors"
    source.parent.mkdir()
    source.write_bytes(data)
    result = intake.import_path(source, kind="adapter")
    assert result["status"] == "imported" and source.read_bytes() == data
