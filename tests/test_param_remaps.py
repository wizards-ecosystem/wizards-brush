"""Renaming a persisted param key must not orphan historical jobs."""
from __future__ import annotations

import json

import pytest

from backend.app import models
from backend.app.models import Asset, Job, PromptHistory, migrate_params, remap_params


@pytest.fixture
def remaps(monkeypatch):
    monkeypatch.setattr(models, "PARAM_REMAPS", {"cfg": "guidance", "n_frames": "num_frames"})


def test_no_remaps_configured_is_a_passthrough():
    raw = {"guidance": 1.0, "seed": 7}
    assert remap_params(raw) == raw


def test_legacy_key_is_translated(remaps):
    assert remap_params({"cfg": 3.5}) == {"guidance": 3.5}


def test_untouched_keys_survive(remaps):
    assert remap_params({"cfg": 3.5, "seed": 9}) == {"guidance": 3.5, "seed": 9}


def test_current_key_wins_over_a_legacy_alias(remaps):
    """A row carrying both spellings kept the newer one deliberately."""
    got = remap_params({"guidance": 7.0, "cfg": 1.0})
    assert got == {"guidance": 7.0}


def test_applied_when_reading_a_job(remaps):
    job = Job(kind="image_local", params_json=json.dumps({"cfg": 2.5, "prompt": "x"}))
    assert job.params == {"guidance": 2.5, "prompt": "x", "_v": 1}


def test_applied_when_reading_prompt_history(remaps):
    """History replays into the generator too, so it needs the same treatment."""
    h = PromptHistory(kind="image_local", params_json=json.dumps({"n_frames": 49}))
    assert h.params == {"num_frames": 49, "_v": 1}


def test_asset_metadata_uses_the_same_lazy_semantic_boundary(remaps):
    asset = Asset(kind="image", path="x.png", filename="x.png",
                  meta_json=json.dumps({"cfg": 4.0, "prompt": "x"}))
    assert asset.meta == {"guidance": 4.0, "prompt": "x", "_v": 1}


def test_newer_semantics_are_never_downgraded():
    future = {"_v": 99, "prompt": "future"}
    assert migrate_params(future) == future


def test_a_missing_migration_step_fails_loudly(monkeypatch):
    monkeypatch.setattr(models, "PARAM_SCHEMA_VERSION", 2)
    monkeypatch.setattr(models, "PARAM_MIGRATIONS", {0: lambda value: value})
    with pytest.raises(ValueError, match="version 1"):
        migrate_params({})


def test_rows_on_disk_are_left_alone(remaps):
    """Remap on read, never rewrite. Reversible, and no migration per rename."""
    raw = json.dumps({"cfg": 2.5})
    job = Job(kind="image_local", params_json=raw)
    _ = job.params
    assert job.params_json == raw


def test_no_remap_target_collides_with_a_source():
    """A -> B while B -> C would silently apply one rename or two depending on
    dict order. Chains are a bug; assert the table has none."""
    sources = set(models.PARAM_REMAPS)
    targets = set(models.PARAM_REMAPS.values())
    assert not (sources & targets), f"remap chain: {sources & targets}"
