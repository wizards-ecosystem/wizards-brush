"""The LoRA catalogue reports architecture and compatibility, and never hides a
file the user put there."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from backend.app import loras
from backend.app.config import settings


def _lora(name: str, keys: dict) -> None:
    settings.loras_dir.mkdir(parents=True, exist_ok=True)
    blob = json.dumps({"__metadata__": {}, **keys}).encode()
    (settings.loras_dir / name).write_bytes(struct.pack("<Q", len(blob)) + blob + b"\x00" * 8)


def _tensor(shape=(8, 8)):
    return {"dtype": "F16", "shape": list(shape), "data_offsets": [0, 16]}


@pytest.fixture(autouse=True)
def clean_lora_dir():
    loras._PROBE_CACHE.clear()
    loras._SIDECAR_CACHE.clear()
    d = settings.loras_dir
    if d.exists():
        for f in d.iterdir():
            if f.is_file():
                f.unlink()
    yield
    if d.exists():
        for f in d.iterdir():
            if f.is_file():
                f.unlink()


def test_reports_architecture_from_the_file_header():
    _lora("fluxy.safetensors", {
        "double_blocks.0.img_attn.lora_down.weight": _tensor((16, 64)),
        "vector_in.weight": _tensor(),
    })
    (item,) = loras.list_loras()
    assert item["family"] == "flux"
    assert item["arch"] == "FLUX.1"
    assert item["rank"] == 16


def test_incompatible_adapters_are_listed_not_hidden():
    """Hiding a file someone downloaded five minutes ago produces bug reports."""
    _lora("fluxy.safetensors", {
        "double_blocks.0.img_attn.lora_down.weight": _tensor(),
        "vector_in.weight": _tensor(),
    })
    items = loras.list_loras(model="Tongyi-MAI/Z-Image-Turbo")
    assert len(items) == 1, "the file must still appear"
    assert items[0]["compatible"] is False


def test_matching_adapters_are_marked_compatible():
    _lora("zzz.safetensors", {
        "transformer_blocks.0.attn.lora_down.weight": _tensor(),
        "time_embed.weight": _tensor(),
    })
    (item,) = loras.list_loras(model="Tongyi-MAI/Z-Image-Turbo")
    assert item["family"] == "zimage"
    assert item["compatible"] is True


def test_unrecognised_files_are_compatible_by_default():
    """We do not warn on a guess."""
    _lora("mystery.safetensors", {"totally.novel.key": _tensor()})
    (item,) = loras.list_loras(model="Tongyi-MAI/Z-Image-Turbo")
    assert item["family"] == "unknown"
    assert item["compatible"] is True


def test_no_model_means_no_judgement():
    _lora("fluxy.safetensors", {
        "double_blocks.0.img_attn.lora_down.weight": _tensor(),
        "vector_in.weight": _tensor(),
    })
    (item,) = loras.list_loras()
    assert item["compatible"] is True


def test_pickle_formats_are_never_catalogued_or_resolved():
    """A filename placed by an operator must not become an in-process pickle load."""
    settings.loras_dir.mkdir(parents=True, exist_ok=True)
    (settings.loras_dir / "old.pt").write_bytes(b"\x80\x04not really a pickle")
    (settings.loras_dir / "old.bin").write_bytes(b"not really a pickle")
    assert loras.list_loras() == []
    assert loras.resolve_path("old.pt") is None
    assert loras.sanitize([{"path": "old.bin", "weight": 1.0}]) == []


def test_safetensors_are_not_flagged_as_pickled():
    _lora("safe.safetensors", {"a.weight": _tensor()})
    (item,) = loras.list_loras()
    assert item["pickled"] is False


def test_a_full_checkpoint_is_listed_but_cannot_be_selected():
    _lora("checkpoint.safetensors", {
        "input_blocks.0.weight": _tensor(),
        "middle_block.0.weight": _tensor(),
    })
    (item,) = loras.list_loras()
    assert item["is_adapter"] is False
    assert item["selectable"] is False
    assert "not a recognized adapter" in item["unavailable_reason"]
    assert loras.sanitize([{"path": "checkpoint.safetensors", "weight": 1.0}]) == []


def test_probe_results_are_cached_by_mtime():
    _lora("cached.safetensors", {"a.weight": _tensor()})
    loras.list_loras()
    before = len(loras._PROBE_CACHE)
    loras.list_loras()
    assert len(loras._PROBE_CACHE) == before, "a second listing must not re-probe"


def test_replacing_a_file_invalidates_its_cache_entry():
    _lora("swap.safetensors", {"input_blocks.0.weight": _tensor(),
                               "middle_block.0.weight": _tensor()})
    assert loras.list_loras()[0]["family"] == "sd"
    _lora("swap.safetensors", {"double_blocks.0.img_attn.weight": _tensor(),
                               "vector_in.weight": _tensor()})
    assert loras.list_loras()[0]["family"] == "flux", "cache keyed on mtime and size"


def test_probe_cache_evicts_oldest_entries_instead_of_forgetting_everything(tmp_path, monkeypatch):
    from backend.app.modelprobe import Probe

    monkeypatch.setattr("backend.app.modelprobe.probe_file",
                        lambda path: Probe(family="unknown", display="Unknown"))
    files = []
    for index in range(loras._CACHE_LIMIT + 2):
        path = tmp_path / f"{index}.safetensors"
        path.write_bytes(b"x")
        files.append(path)
        loras.probe_cached(path)

    assert len(loras._PROBE_CACHE) == loras._CACHE_LIMIT
    assert not any(key[0] == str(files[0]) for key in loras._PROBE_CACHE)
    assert any(key[0] == str(files[-1]) for key in loras._PROBE_CACHE)


def test_sidecar_cache_invalidates_when_configuration_changes(tmp_path):
    import os

    adapter = tmp_path / "style.safetensors"
    adapter.write_bytes(b"x")
    sidecar = adapter.with_suffix(".json")
    sidecar.write_text('{"recommended_weight": 0.4}')
    assert loras.recommended_weight(adapter) == 0.4

    before = sidecar.stat().st_mtime_ns
    sidecar.write_text('{"recommended_weight": 0.8, "variants": ["turbo"]}')
    os.utime(sidecar, ns=(before + 1_000_000, before + 1_000_000))
    assert loras.recommended_weight(adapter) == 0.8
    assert loras.pinned_variants(adapter) == ["turbo"]


def test_route_reports_the_resolved_model(client):
    r = client.get("/api/loras?model_variant=turbo")
    assert r.status_code == 200
    assert r.json()["model"], "the UI needs to know what it is comparing against"


def test_the_suite_cannot_reach_a_real_lora_library():
    """This module writes fixtures into settings.loras_dir and unlinks them.

    That is only safe because conftest redirects LORA_DIR into the session temp
    root. Without it the suite deletes whatever the developer has installed —
    which it did, once, costing a multi-gigabyte re-download. This asserts the
    redirect is still in force, so the guard cannot be silently removed.
    """
    import os

    from backend.app.config import settings

    resolved = settings.loras_dir.resolve()
    assert "pytest-session-" in str(resolved), (
        f"loras_dir points at a real library: {resolved}")
    assert resolved != (Path(os.getcwd()) / "models" / "loras").resolve()
