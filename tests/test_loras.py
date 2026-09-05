"""LoRA catalogue: listing, path containment, and param sanitization.

`loras` is user-supplied, persisted into job params, and replayed verbatim on
rerun — so the containment check is load-bearing, not a formality.
"""
from __future__ import annotations

import pytest

from backend.app import loras as L
from backend.app.config import settings


@pytest.fixture()
def lora_dir(tmp_path, monkeypatch):
    d = tmp_path / "loras"
    (d / "nested").mkdir(parents=True)
    (d / "Cool Style v2.safetensors").write_bytes(b"x" * 10)
    (d / "another-lora.safetensors").write_bytes(b"y" * 20)
    (d / "nested" / "deep.safetensors").write_bytes(b"z" * 5)
    (d / "notes.txt").write_text("not a lora")
    monkeypatch.setattr(type(settings), "loras_dir", property(lambda self: d))
    return d


def test_lists_only_adapter_files(lora_dir):
    names = {i["filename"] for i in L.list_loras()}
    assert names == {"Cool Style v2.safetensors", "another-lora.safetensors", "deep.safetensors"}


def test_labels_are_readable_and_names_are_peft_safe(lora_dir):
    by_file = {i["filename"]: i for i in L.list_loras()}
    entry = by_file["Cool Style v2.safetensors"]
    assert entry["label"] == "Cool Style v2"
    # peft embeds the adapter name in module paths — no spaces, no separators
    assert entry["name"] == "Cool_Style_v2"


def test_adapter_names_include_the_relative_directory():
    assert L.adapter_name("portraits/style.safetensors") != L.adapter_name(
        "landscapes/style.safetensors"
    )


def test_missing_directory_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(type(settings), "loras_dir", property(lambda self: tmp_path / "nope"))
    assert L.list_loras() == []


# --- containment -----------------------------------------------------------
@pytest.mark.parametrize("escape", [
    "../secret.safetensors",
    "../../etc/passwd",
    "nested/../../outside.safetensors",
    "/etc/passwd",
])
def test_paths_cannot_escape_the_lora_dir(lora_dir, escape):
    assert L.resolve_path(escape) is None


def test_resolves_a_real_entry(lora_dir):
    p = L.resolve_path("another-lora.safetensors")
    assert p is not None and p.name == "another-lora.safetensors"
    assert L.resolve_path("nested/deep.safetensors") is not None


def test_non_adapter_extensions_do_not_resolve(lora_dir):
    assert L.resolve_path("notes.txt") is None


# --- sanitize --------------------------------------------------------------
def test_sanitize_normalizes_strings_and_dicts(lora_dir):
    out = L.sanitize(["another-lora.safetensors",
                      {"path": "nested/deep.safetensors", "weight": 0.6}])
    assert out == [
        {"path": "another-lora.safetensors", "weight": 1.0},
        {"path": "nested/deep.safetensors", "weight": 0.6},
    ]


def test_sanitize_drops_unknown_entries_instead_of_failing_the_job(lora_dir):
    """A rerun of an older job may reference a LoRA that has since been deleted.
    That should cost you the adapter, not the whole generation."""
    out = L.sanitize([{"path": "gone.safetensors"}, "another-lora.safetensors"])
    assert out == [{"path": "another-lora.safetensors", "weight": 1.0}]


def test_sanitize_rejects_traversal(lora_dir):
    assert L.sanitize([{"path": "../../etc/passwd", "weight": 1.0}]) == []


def test_sanitize_clamps_weight_and_allows_negatives(lora_dir):
    f = "another-lora.safetensors"
    assert L.sanitize([{"path": f, "weight": 99}])[0]["weight"] == L.WEIGHT_MAX
    assert L.sanitize([{"path": f, "weight": -99}])[0]["weight"] == L.WEIGHT_MIN
    assert L.sanitize([{"path": f, "weight": -0.5}])[0]["weight"] == -0.5


def test_sanitize_preserves_an_explicit_text_encoder_weight(lora_dir):
    item = L.sanitize([{
        "path": "another-lora.safetensors",
        "weight": 0.9,
        "te_weight": 0.35,
    }])[0]
    assert item == {
        "path": "another-lora.safetensors",
        "weight": 0.9,
        "te_weight": 0.35,
    }


def test_sanitize_caps_the_stack_and_dedupes(lora_dir):
    f = "another-lora.safetensors"
    assert len(L.sanitize([{"path": f}] * 10)) == 1  # deduped
    many = [{"path": n} for n in ("Cool Style v2.safetensors", "another-lora.safetensors",
                                  "nested/deep.safetensors")] * 3
    assert len(L.sanitize(many)) <= L.MAX_ACTIVE


@pytest.mark.parametrize("junk", [None, "string", 42, {"path": "x"}, [None, 5, {}], [{"weight": 1}]])
def test_sanitize_never_raises_on_junk(lora_dir, junk):
    assert isinstance(L.sanitize(junk), list)


def test_sanitize_survives_an_unparseable_weight(lora_dir):
    out = L.sanitize([{"path": "another-lora.safetensors", "weight": "heavy"}])
    assert out == [{"path": "another-lora.safetensors", "weight": 1.0}]
