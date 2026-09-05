"""A LoRA that does not apply must not be a silent no-op.

Every failure in `_apply_loras` is per-file and non-fatal: a missing adapter, a
corrupt one, a set the pipeline refuses. The job still produces images. That is
exactly why it cannot be an exception — and exactly why a log line is not
enough, because the user gets output that quietly ignored the style they asked
for with nothing on screen to explain it.
"""
from __future__ import annotations

import pytest

from backend.app.routers.common import image_meta


class _FakePipe:
    """Enough pipeline surface for _apply_loras, with scriptable failures."""

    def __init__(self, *, load_raises=False, activate_raises=False):
        self.load_raises = load_raises
        self.activate_raises = activate_raises
        self.enabled = False

    def disable_lora(self): self.enabled = False
    def delete_adapters(self, names): pass

    def load_lora_weights(self, folder, weight_name=None, adapter_name=None):
        if self.load_raises:
            raise RuntimeError("size mismatch for transformer.blocks.0")

    def set_adapters(self, names, adapter_weights=None):
        if self.activate_raises:
            raise RuntimeError("incompatible_lora: rank mismatch")

    def enable_lora(self): self.enabled = True


class _TrackingPipe(_FakePipe):
    def __init__(self):
        super().__init__()
        self.present: set[str] = set()

    def delete_adapters(self, names):
        self.present.difference_update(names)

    def load_lora_weights(self, folder, weight_name=None, adapter_name=None):
        if adapter_name is None:
            return
        self.present.add(adapter_name)

    def set_adapters(self, names, adapter_weights=None):
        missing = set(names) - self.present
        if missing:
            raise RuntimeError(f"not in the list of present adapters: {self.present}")


def test_mixed_adapter_families_are_filtered_before_activation(apply, monkeypatch, tmp_path):
    from backend.app import loras as lora_lib
    from backend.app.modelprobe import Probe

    def fake_resolve(rel: str):
        return tmp_path / rel

    def fake_probe(path):
        if path.name.startswith("flux"):
            return Probe(family="flux", display="FLUX.1")
        return Probe(family="zimage", display="Z-Image")

    for name in ("flux.safetensors", "zimage.safetensors"):
        (tmp_path / name).write_bytes(b"x")

    monkeypatch.setattr(lora_lib, "resolve_path", fake_resolve)
    monkeypatch.setattr(lora_lib, "probe_cached", fake_probe)
    got = apply(
        _TrackingPipe(),
        "Tongyi-MAI/Z-Image-Turbo",
        [
            {"path": "flux.safetensors", "weight": 1.0},
            {"path": "zimage.safetensors", "weight": 1.0},
        ],
    )
    assert got.desc == "zimage@1"
    assert "flux" not in got.desc


def test_replacing_a_lora_set_clears_previous_adapters(apply, monkeypatch, tmp_path):
    from backend.app import loras as lora_lib
    from backend.app.modelprobe import Probe

    def fake_resolve(rel: str):
        return tmp_path / rel

    def fake_probe(path):
        return Probe(family="zimage", display="Z-Image")

    for name in ("old.safetensors", "new.safetensors"):
        (tmp_path / name).write_bytes(b"x")

    monkeypatch.setattr(lora_lib, "resolve_path", fake_resolve)
    monkeypatch.setattr(lora_lib, "probe_cached", fake_probe)

    pipe = _TrackingPipe()
    apply(pipe, "Tongyi-MAI/Z-Image-Turbo", [{"path": "old.safetensors", "weight": 1.0}])
    apply(pipe, "Tongyi-MAI/Z-Image-Turbo", [{"path": "new.safetensors", "weight": 1.0}])

    assert pipe.present == {"new"}


@pytest.fixture(autouse=True)
def _no_file_conversion(monkeypatch):
    """Keep these tests off the state-dict converter.

    `_apply_loras` asks `loraconv.state_dict_for` whether a file needs rewriting,
    and answering means reading it with safetensors — which imports torch and
    would break the suite's torch-free guarantee. None is the "no rewrite
    needed" answer, so the adapter loads by path exactly as it did before the
    converter existed. What the converter itself does is covered, without torch,
    in test_lora_conversion.py.
    """
    from backend.app.generators import loraconv

    monkeypatch.setattr(loraconv, "state_dict_for", lambda path: None)


@pytest.fixture()
def apply(monkeypatch):
    from backend.app.generators import local_image

    prepared: set[str] = set()

    def run(pipe, model, loras):
        if model not in prepared:
            monkeypatch.setitem(local_image._STATE, model, {"components": {}, "classes": ()})
            prepared.add(model)
        return local_image._apply_loras(pipe, model, loras)

    return run


def test_a_missing_adapter_file_is_reported_not_skipped_quietly(apply, monkeypatch):
    from backend.app import loras as lora_lib

    monkeypatch.setattr(lora_lib, "resolve_path", lambda rel: None)
    got = apply(_FakePipe(), "m", [{"path": "styles/watercolour.safetensors", "weight": 1.0}])
    assert got.desc == ""
    assert len(got.skipped) == 1
    assert "watercolour" in got.skipped[0], "name the adapter the picker showed"
    assert "gone" in got.skipped[0]


def test_an_adapter_that_fails_to_load_carries_the_reason(apply, monkeypatch, tmp_path):
    from backend.app import loras as lora_lib

    f = tmp_path / "bad.safetensors"
    f.write_bytes(b"x")
    monkeypatch.setattr(lora_lib, "resolve_path", lambda rel: f)
    got = apply(_FakePipe(load_raises=True), "m", [{"path": "bad.safetensors", "weight": 1.0}])
    assert got.skipped and "size mismatch" in got.skipped[0]


def test_a_set_the_pipeline_refuses_is_reported(apply, monkeypatch, tmp_path):
    from backend.app import loras as lora_lib

    f = tmp_path / "ok.safetensors"
    f.write_bytes(b"x")
    monkeypatch.setattr(lora_lib, "resolve_path", lambda rel: f)
    got = apply(_FakePipe(activate_raises=True), "m", [{"path": "ok.safetensors", "weight": 1.0}])
    assert got.desc == ""
    assert got.skipped and "incompatible_lora" in got.skipped[0]


def test_the_warning_survives_the_repeat_that_a_batch_makes(apply, monkeypatch):
    """A batch of eight applies once and reuses the result seven times. A warning
    that lived only on the first pass would be reported for one image out of
    eight that all equally ignored the adapter."""
    from backend.app import loras as lora_lib

    monkeypatch.setattr(lora_lib, "resolve_path", lambda rel: None)
    req = [{"path": "styles/gone.safetensors", "weight": 1.0}]
    first = apply(_FakePipe(), "m", req)
    second = apply(_FakePipe(), "m", req)      # cache hit
    assert first.skipped == second.skipped != []


def test_no_loras_requested_warns_about_nothing(apply):
    got = apply(_FakePipe(), "m", [])
    assert got == ("", [])


def test_empty_warnings_are_not_recorded_in_metadata():
    """"Nothing went wrong" is the normal case. A key present and empty on every
    successful image trains people to stop reading it."""
    from PIL import Image

    img = Image.new("RGB", (8, 8))
    assert "warnings" not in image_meta({}, img, prompt="p", seed=1, warnings=[])
    assert image_meta({}, img, prompt="p", seed=1, warnings=["x"])["warnings"] == ["x"]


def test_reusing_a_lora_at_a_different_weight_still_activates(apply, monkeypatch, tmp_path):
    """Changing only the WEIGHT invalidates the applied-set cache, so the whole
    reset path runs — and it must leave the adapter attached.

    A reset that deleted every registered adapter but left `loaded` claiming
    them skipped the reload here, then handed `set_adapters` a name the pipeline
    no longer had. _TrackingPipe raises on exactly that, which is the bug.
    """
    from backend.app import loras as lora_lib
    from backend.app.modelprobe import Probe

    (tmp_path / "style.safetensors").write_bytes(b"x")
    monkeypatch.setattr(lora_lib, "resolve_path", lambda rel: tmp_path / rel)
    monkeypatch.setattr(lora_lib, "probe_cached",
                        lambda path: Probe(family="zimage", display="Z-Image"))

    pipe = _TrackingPipe()
    first = apply(pipe, "Tongyi-MAI/Z-Image-Turbo", [{"path": "style.safetensors", "weight": 1.0}])
    second = apply(pipe, "Tongyi-MAI/Z-Image-Turbo", [{"path": "style.safetensors", "weight": 0.4}])

    assert first.skipped == [] and second.skipped == []
    assert second.desc == "style@0.4"
    assert pipe.present == {"style"}


def test_sdxl_text_encoder_weight_is_component_scoped_and_cache_significant(
    apply, monkeypatch, tmp_path,
):
    from backend.app import loras as lora_lib
    from backend.app.modelprobe import Probe

    path = tmp_path / "character.safetensors"
    path.write_bytes(b"x")
    monkeypatch.setattr(lora_lib, "resolve_path", lambda rel: path)
    monkeypatch.setattr(lora_lib, "probe_cached",
                        lambda file: Probe(family="sdxl", display="Stable Diffusion XL"))

    class ComponentPipe(_TrackingPipe):
        _lora_loadable_modules = ("unet", "text_encoder", "text_encoder_2")

        def __init__(self):
            super().__init__()
            self.weights = []

        def get_list_adapters(self):
            return {component: list(self.present) for component in self._lora_loadable_modules}

        def set_adapters(self, names, adapter_weights=None):
            super().set_adapters(names, adapter_weights)
            self.weights.append(adapter_weights)

    pipe = ComponentPipe()
    model = "stabilityai/stable-diffusion-xl-base-1.0"
    first = apply(pipe, model, [{"path": path.name, "weight": 0.9, "te_weight": 0.35}])
    second = apply(pipe, model, [{"path": path.name, "weight": 0.9, "te_weight": 0.5}])

    assert first.desc == "character@0.9 (text 0.35)"
    assert second.desc == "character@0.9 (text 0.5)"
    assert pipe.weights[-1] == [{"unet": 0.9, "text_encoder": 0.5, "text_encoder_2": 0.5}]
    assert len(pipe.weights) == 2, "changing only text strength must invalidate applied-state cache"


def test_overwriting_a_lora_in_place_reloads_its_new_weights(apply, monkeypatch, tmp_path):
    """The picker and resident transformer must agree after retraining in place."""
    import os

    from backend.app import loras as lora_lib
    from backend.app.modelprobe import Probe

    path = tmp_path / "style.safetensors"
    path.write_bytes(b"old")
    monkeypatch.setattr(lora_lib, "resolve_path", lambda rel: path)
    monkeypatch.setattr(lora_lib, "probe_cached",
                        lambda file: Probe(family="zimage", display="Z-Image"))

    class CountingPipe(_TrackingPipe):
        loads = 0

        def load_lora_weights(self, folder, weight_name=None, adapter_name=None):
            self.loads += 1
            super().load_lora_weights(folder, weight_name, adapter_name)

    pipe = CountingPipe()
    req = [{"path": "style.safetensors", "weight": 1.0}]
    apply(pipe, "Tongyi-MAI/Z-Image-Turbo", req)
    before = path.stat().st_mtime_ns
    path.write_bytes(b"new weights")
    os.utime(path, ns=(before + 1_000_000, before + 1_000_000))
    got = apply(pipe, "Tongyi-MAI/Z-Image-Turbo", req)

    assert got.skipped == []
    assert pipe.loads == 2


def test_a_pipeline_that_cannot_disable_old_loras_is_not_allowed_to_contaminate_output(
    apply, monkeypatch, tmp_path,
):
    from backend.app import loras as lora_lib
    from backend.app.modelprobe import Probe

    path = tmp_path / "style.safetensors"
    path.write_bytes(b"x")
    monkeypatch.setattr(lora_lib, "resolve_path", lambda rel: path)
    monkeypatch.setattr(lora_lib, "probe_cached",
                        lambda file: Probe(family="zimage", display="Z-Image"))

    class CannotDisable(_TrackingPipe):
        def disable_lora(self):
            raise RuntimeError("backend reset failed")

    pipe = CannotDisable()
    apply(pipe, "Tongyi-MAI/Z-Image-Turbo", [{"path": "style.safetensors", "weight": 1.0}])
    with pytest.raises(RuntimeError, match="could not disable prior LoRAs"):
        apply(pipe, "Tongyi-MAI/Z-Image-Turbo", [])


def test_a_failing_delete_still_disables_the_previous_adapters(apply, monkeypatch, tmp_path):
    """The reset's two halves must not share a suppress().

    Dropping a stale adapter is best-effort; turning the previous set OFF is not.
    When they were one statement, a raising delete skipped the disable and the
    last generation's style silently carried into the next prompt.
    """
    from backend.app import loras as lora_lib
    from backend.app.modelprobe import Probe

    for name in ("old.safetensors", "new.safetensors"):
        (tmp_path / name).write_bytes(b"x")
    monkeypatch.setattr(lora_lib, "resolve_path", lambda rel: tmp_path / rel)
    monkeypatch.setattr(lora_lib, "probe_cached",
                        lambda path: Probe(family="zimage", display="Z-Image"))

    class _UndeletablePipe(_TrackingPipe):
        def delete_adapters(self, names):
            raise RuntimeError("this backend cannot delete adapters")

    pipe = _UndeletablePipe()
    apply(pipe, "Tongyi-MAI/Z-Image-Turbo", [{"path": "old.safetensors", "weight": 1.0}])
    pipe.enabled = True
    got = apply(pipe, "Tongyi-MAI/Z-Image-Turbo", [{"path": "new.safetensors", "weight": 1.0}])

    # The undeletable adapter is still attached — that is survivable — but the
    # new set applied cleanly on top of a pipeline that was genuinely reset.
    assert got.skipped == []
    assert got.desc == "new@1"
    assert "new" in pipe.present


def test_an_adapter_pinned_to_another_model_is_skipped_and_said_so(apply, monkeypatch, tmp_path):
    """Family matching cannot catch a checkpoint mismatch.

    Z-Image-Turbo and Z-Image base are both family "zimage" and every tensor
    shape lines up, so a Turbo-trained adapter loads into base without error and
    renders near-black. Measured, not hypothetical. The pin is the only thing
    that knows, so it has to be enforced where the adapter is applied — and
    reported, because a silently dropped adapter is the failure we are avoiding.
    """
    from backend.app import loras as lora_lib
    from backend.app.modelprobe import Probe

    (tmp_path / "turbo-only.safetensors").write_bytes(b"x")
    monkeypatch.setattr(lora_lib, "resolve_path", lambda rel: tmp_path / rel)
    monkeypatch.setattr(lora_lib, "probe_cached",
                        lambda path: Probe(family="zimage", display="Z-Image"))
    monkeypatch.setattr(lora_lib, "pinned_variants", lambda path: ["turbo"])

    req = [{"path": "turbo-only.safetensors", "weight": 0.8}]
    on_base = apply(_TrackingPipe(), "Tongyi-MAI/Z-Image", req)
    assert on_base.desc == "" and len(on_base.skipped) == 1
    assert "turbo" in on_base.skipped[0]

    on_turbo = apply(_TrackingPipe(), "Tongyi-MAI/Z-Image-Turbo", req)
    assert on_turbo.desc == "turbo-only@0.8" and on_turbo.skipped == []


def test_an_unpinned_adapter_is_unaffected(apply, monkeypatch, tmp_path):
    """Most adapters carry no sidecar. Absence of a pin must mean "anywhere its
    family fits", never "nowhere"."""
    from backend.app import loras as lora_lib
    from backend.app.modelprobe import Probe

    (tmp_path / "free.safetensors").write_bytes(b"x")
    monkeypatch.setattr(lora_lib, "resolve_path", lambda rel: tmp_path / rel)
    monkeypatch.setattr(lora_lib, "probe_cached",
                        lambda path: Probe(family="zimage", display="Z-Image"))
    got = apply(_TrackingPipe(), "Tongyi-MAI/Z-Image",
                [{"path": "free.safetensors", "weight": 1.0}])
    assert got.desc == "free@1" and got.skipped == []
