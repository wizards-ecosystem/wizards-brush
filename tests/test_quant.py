"""Quantization backend selection.

No GPU and no torch here — this covers the decision logic, which is the part
that decides whether a user's generation runs at all.
"""
from __future__ import annotations

import backend.app.generators.quant as q

MODEL = "Tongyi-MAI/Z-Image-Turbo"


def test_non_nunchaku_backends_pass_straight_through(monkeypatch):
    for want in ("4bit", "fp8", "none"):
        monkeypatch.setattr(q.settings, "local_quant", want)
        assert q.resolve_backend(MODEL) == want


def test_unknown_quant_backend_falls_back_to_a_real_one(monkeypatch):
    monkeypatch.setattr(q.settings, "local_quant", "wishful-int3")
    assert q.resolve_backend(MODEL) == "4bit"


def test_unknown_model_falls_back_without_touching_the_gpu(monkeypatch):
    monkeypatch.setattr(q.settings, "local_quant", "nunchaku")
    ok, why = q.available("some/unpublished-model")
    assert ok is False
    assert "no published Nunchaku checkpoint" in why
    assert q.resolve_backend("some/unpublished-model") == "4bit"


def test_preflight_failure_falls_back_rather_than_crashing(monkeypatch):
    """A kernel mismatch SIGABRTs rather than raising, so the check runs in a
    subprocess. Whatever it reports, the caller must get a usable backend."""
    monkeypatch.setattr(q.settings, "local_quant", "nunchaku")
    monkeypatch.setattr(q, "available", lambda m: (True, ""))
    monkeypatch.setattr(q, "preflight", lambda m: False)
    assert q.resolve_backend(MODEL) == "4bit"


def test_preflight_success_selects_nunchaku(monkeypatch):
    monkeypatch.setattr(q.settings, "local_quant", "nunchaku")
    monkeypatch.setattr(q, "available", lambda m: (True, ""))
    monkeypatch.setattr(q, "preflight", lambda m: True)
    assert q.resolve_backend(MODEL) == "nunchaku"


def test_preflight_is_cached_so_the_subprocess_runs_once(monkeypatch):
    calls = []

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(q, "_PREFLIGHT", {})
    import subprocess

    def fake_run(*a, **k):
        calls.append(1)
        return _Proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert q.preflight(MODEL) is False
    assert q.preflight(MODEL) is False
    assert len(calls) == 1, "preflight must be cached — it costs a model load"


def test_a_preflight_that_cannot_run_is_treated_as_unusable(monkeypatch):
    """Never let a broken check block generation — fall back, don't raise."""
    import subprocess

    monkeypatch.setattr(q, "_PREFLIGHT", {})

    def boom(*a, **k):
        raise OSError("no python?")

    monkeypatch.setattr(subprocess, "run", boom)
    assert q.preflight(MODEL) is False


def test_checkpoint_name_matches_the_published_filenames(monkeypatch):
    """Guards against silently requesting a file the repo does not have."""
    monkeypatch.setattr(q, "precision", lambda: "int4")
    assert q.checkpoint_name(MODEL, 128) == "svdq-int4_r128-z-image-turbo.safetensors"
    assert q.checkpoint_name(MODEL, 32) == "svdq-int4_r32-z-image-turbo.safetensors"
    monkeypatch.setattr(q, "precision", lambda: "fp4")
    assert q.checkpoint_name(MODEL, 128) == "svdq-fp4_r128-z-image-turbo.safetensors"


def test_every_mapped_repo_has_a_derivable_checkpoint_name(monkeypatch):
    monkeypatch.setattr(q, "precision", lambda: "int4")
    for model in q.NUNCHAKU_REPOS:
        name = q.checkpoint_name(model)
        assert name.startswith("svdq-int4_r") and name.endswith(".safetensors")
