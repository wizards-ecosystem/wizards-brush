"""First-use model bytes are fetched before the process-global GPU section."""
from __future__ import annotations

from backend.app.generators import local_image, quant
from backend.app.generators.base import LoadReporter


class TrackingLock:
    def __init__(self):
        self.active = False
        self.acquires = 0
        self.releases = 0

    def acquire(self):
        assert not self.active
        self.active = True
        self.acquires += 1

    def release(self):
        assert self.active
        self.active = False
        self.releases += 1


def test_pipeline_policy_preserves_repository_safety_components():
    class Pipeline:
        def __init__(self, safety_checker, add_watermarker=True):
            pass

    assert local_image._pipeline_policy(Pipeline) == {"add_watermarker": False}


def test_model_prefetch_finishes_before_gpu_lock(monkeypatch):
    lock = TrackingLock()
    events: list[str] = []
    monkeypatch.setattr(local_image, "GPU_LOCK", lock)

    def prefetch(model, rep, *, force_base=False):
        assert not lock.active
        events.append(f"prefetch:{force_base}")
        return force_base

    def get(model, rep, *, local_files_only=False, force_base=False):
        assert lock.active
        assert local_files_only is True
        events.append("load")
        return {"component": object()}, (object, None, None)

    monkeypatch.setattr(local_image, "_prefetch_model", prefetch)
    monkeypatch.setattr(local_image, "_get", get)

    with local_image._model_locked("acme/model", LoadReporter(None)) as loaded:
        assert lock.active
        assert "component" in loaded[0]
    assert not lock.active
    assert events == ["prefetch:False", "load"]
    assert lock.acquires == lock.releases == 1


def test_nunchaku_fallback_fetches_base_after_releasing_gpu(monkeypatch):
    lock = TrackingLock()
    force_values: list[bool] = []
    loads = 0
    monkeypatch.setattr(local_image, "GPU_LOCK", lock)

    def prefetch(model, rep, *, force_base=False):
        assert not lock.active
        force_values.append(force_base)
        return force_base

    def get(model, rep, *, local_files_only=False, force_base=False):
        nonlocal loads
        assert lock.active
        loads += 1
        if loads == 1:
            raise local_image._BasePrefetchRequired("SVDQuant did not fit")
        assert force_base is True
        return {}, (object, None, None)

    monkeypatch.setattr(local_image, "_prefetch_model", prefetch)
    monkeypatch.setattr(local_image, "_get", get)

    with local_image._model_locked("acme/model", LoadReporter(None)):
        assert lock.active
    assert force_values == [False, True]
    assert lock.acquires == lock.releases == 2


def test_nunchaku_prefetch_omits_the_repositories_full_transformer(monkeypatch):
    captured = {}

    class Pipeline:
        @classmethod
        def download(cls, model, **kwargs):
            captured.update(kwargs)

    local_image._STATE.clear()
    monkeypatch.setattr(local_image, "resolve_classes", lambda model: (Pipeline, None, None))
    monkeypatch.setattr(local_image, "_assert_download_space", lambda model, rep: None)
    monkeypatch.setattr(local_image, "_pipeline_policy", lambda cls: {})
    monkeypatch.setattr(quant, "resolve_backend", lambda model: "nunchaku")
    monkeypatch.setattr(quant, "download_transformer", lambda model: "/cached/svdq.safetensors")

    forced = local_image._prefetch_model("acme/model", LoadReporter(None))
    assert forced is False
    assert "transformer" in captured
    assert captured["transformer"] is not None


def test_missing_nunchaku_checkpoint_prefetches_the_base_transformer(monkeypatch):
    captured = {}

    class Pipeline:
        @classmethod
        def download(cls, model, **kwargs):
            captured.update(kwargs)

    local_image._STATE.clear()
    monkeypatch.setattr(local_image, "resolve_classes", lambda model: (Pipeline, None, None))
    monkeypatch.setattr(local_image, "_assert_download_space", lambda model, rep: None)
    monkeypatch.setattr(local_image, "_pipeline_policy", lambda cls: {})
    monkeypatch.setattr(quant, "resolve_backend", lambda model: "nunchaku")

    def missing(model):
        raise RuntimeError("404 checkpoint not found")

    monkeypatch.setattr(quant, "download_transformer", missing)
    forced = local_image._prefetch_model("acme/model", LoadReporter(None))
    assert forced is True
    assert "transformer" not in captured
