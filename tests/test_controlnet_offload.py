"""ControlNet's coexistence with CPU offload: the refusal, the opt-in, and the
cache invalidation that keeps the opt-in from poisoning later generations."""
from __future__ import annotations

import pytest

from backend.app.config import settings
from backend.app.generators import offload


def test_defaults_to_refusing(monkeypatch):
    """The failure mode is an uncatchable CUDA abort, so the default must be the
    path that fails early and clearly."""
    assert settings.controlnet_offload == "refuse"


def test_refusal_names_the_setting_that_changes_it(monkeypatch):
    from backend.app.generators import local_image

    monkeypatch.setattr(settings, "local_offload", True)
    monkeypatch.setattr(settings, "controlnet_offload", "refuse")
    with pytest.raises(RuntimeError) as e:
        local_image.generate_control(
            prompt="x", seed=1, width=64, height=64, control_image=None)
    assert "LOCAL_OFFLOAD=false" in str(e.value)
    assert "CONTROLNET_OFFLOAD" in str(e.value), "say how to opt in"


def test_refusal_happens_before_anything_is_loaded(monkeypatch):
    """Nothing may be imported or downloaded before the check — the point is to
    not attempt the configuration at all."""
    import sys

    from backend.app.generators import local_image

    monkeypatch.setattr(settings, "local_offload", True)
    with pytest.raises(RuntimeError):
        local_image.generate_control(
            prompt="x", seed=1, width=64, height=64, control_image=None)
    assert "torch" not in sys.modules


def test_strip_hooks_does_no_work_and_no_import_when_there_are_no_hooks():
    """The common case is offload being off. Importing accelerate — and through
    it torch — to discover there is nothing to do would be a real cost paid on
    every ControlNet run for nothing."""
    import sys

    class Bare:
        pass

    assert offload.strip_hooks({"vae": Bare(), "text_encoder": Bare()}) == 0
    assert "accelerate" not in sys.modules
    assert "torch" not in sys.modules


def test_strip_hooks_survives_accelerate_being_absent(monkeypatch):
    import builtins

    class Hooked:
        _hf_hook = object()

    real = builtins.__import__

    def no_accelerate(name, *a, **kw):
        if name.startswith("accelerate"):
            raise ImportError("no accelerate")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_accelerate)
    assert offload.strip_hooks({"vae": Hooked()}) == 0


def test_enable_for_pipeline_refuses_a_pipeline_that_cannot_offload():
    """Half a placement plan is the exact state that aborts the process, so a
    pipeline that cannot be offloaded must raise rather than be left partial."""
    class NoOffload:
        pass

    with pytest.raises(RuntimeError, match="does not support"):
        offload.enable_for_pipeline(NoOffload())


def test_enable_for_pipeline_installs_offload():
    calls = []

    class Pipe:
        def enable_model_cpu_offload(self):
            calls.append("offload")

    assert "pipeline-owned" in offload.enable_for_pipeline(Pipe())
    assert calls == ["offload"]
