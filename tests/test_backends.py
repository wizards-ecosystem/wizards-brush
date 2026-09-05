"""The backend layer: describing where jobs can run, and the unknown-vs-absent
distinction that decides whether a control is offered."""
from __future__ import annotations

import pytest

from backend.app import backends
from backend.app.backends import LOCAL, REMOTE_GPU, BackendKind, Health, ModelInfo


def test_both_backends_are_registered():
    ids = {b.id for b in backends.all_backends()}
    assert ids == {"local", "remote_gpu"}
    assert backends.get("local") is LOCAL
    assert backends.get("nope") is None


def test_local_is_local_and_colab_is_remote():
    assert LOCAL.kind is BackendKind.local
    assert REMOTE_GPU.kind is BackendKind.remote


def test_local_health_never_imports_the_heavy_stack():
    """health() runs on every settings render. It must read a cached probe, not
    perform one — importing torch into the web request path is exactly what the
    lazy-import rule exists to prevent."""
    import sys

    h = LOCAL.health()
    assert isinstance(h, Health)
    assert h.device, "always name the device, even before the probe has run"
    assert "torch" not in sys.modules


def test_device_reads_as_detecting_before_the_probe_runs():
    """Honest: we do not know yet. Claiming 'CPU only' would be a guess that
    looks like a fact."""
    from backend.app.backends import local

    if local._DEVICE is None:
        assert "detecting" in LOCAL.health().device.lower()


def test_completed_cpu_probe_marks_local_lane_unavailable(monkeypatch):
    from backend.app.backends import local

    monkeypatch.setattr(local, "_DEVICE", ("CPU only", 0.0))
    health = LOCAL.health()
    assert health.connected is False
    assert "CUDA" in local.unavailable_reason()


def test_local_catalog_lists_configured_variants():
    from backend.app.generators import variants

    models = LOCAL.catalog()
    assert models and all(isinstance(m, ModelInfo) for m in models)
    assert {m.id for m in models} == {variants.repo_of(v) for v in variants.available("local")}


def test_local_cache_status_distinguishes_absent_partial_and_ready(monkeypatch, tmp_path):
    from backend.app.backends import local
    from backend.app.config import settings

    monkeypatch.setattr(type(settings), "hf_hub_path", property(lambda self: tmp_path))
    assert local._cache_status("acme/new") == ("absent", None)

    root = tmp_path / "models--acme--new"
    blobs = root / "blobs"
    snapshot = root / "snapshots" / "revision"
    blobs.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    (blobs / "piece").write_bytes(b"1234")
    (blobs / "piece.incomplete").write_bytes(b"ignored")
    (snapshot / "model_index.json").write_text("{}")
    assert local._cache_status("acme/new") == ("partial", 0.0)

    (snapshot / "diffusion_pytorch_model.safetensors").write_bytes(b"weights")
    status, size = local._cache_status("acme/new")
    assert status == "ready"
    assert size == 0.0


def test_complete_snapshot_wins_over_a_stale_incomplete_blob(monkeypatch, tmp_path):
    from backend.app.backends import local
    from backend.app.config import settings

    monkeypatch.setattr(type(settings), "hf_hub_path", property(lambda self: tmp_path))
    root = tmp_path / "models--acme--complete"
    (root / "blobs").mkdir(parents=True)
    (root / "blobs" / "old.incomplete").write_bytes(b"unfinished")
    snapshot = root / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    (snapshot / "model.safetensors").write_bytes(b"weights")
    assert local._cache_status("acme/complete")[0] == "ready"


def test_local_always_has_its_own_resources():
    assert LOCAL.has_resource("any-digest-at-all") is True


def test_local_features_include_the_things_only_local_can_do():
    feats = LOCAL.features()
    assert {"loras", "inpaint", "outpaint"} <= feats


# ---- the unknown-vs-absent rule -------------------------------------------
def test_silence_means_unknown_so_configured_capability_wins(monkeypatch):
    """The mistake this prevents: hiding a control from someone who simply has
    not started their notebook yet."""
    monkeypatch.setattr(REMOTE_GPU, "features", frozenset)
    assert backends.has_feature("speed_image", configured=True) is True
    assert backends.has_feature("speed_image", configured=False) is False


def test_a_backend_that_answered_is_believed(monkeypatch):
    """The actual bug: .env named a Lightning LoRA, the session failed to load
    it, and the UI offered speed mode anyway."""
    monkeypatch.setattr(REMOTE_GPU, "features", lambda: frozenset({"preview", "edit"}))
    assert backends.has_feature("speed_image", configured=True) is False
    assert backends.has_feature("preview", configured=False) is True


def test_an_unknown_backend_id_falls_back_to_configured():
    assert backends.has_feature("anything", backend_id="ghost", configured=True) is True


# ---- health report --------------------------------------------------------
def test_health_report_covers_every_backend():
    report = backends.health_report()
    assert set(report) == {"local", "remote_gpu"}


def test_one_broken_backend_does_not_hide_the_others(monkeypatch):
    def boom():
        raise RuntimeError("tunnel exploded")

    monkeypatch.setattr(REMOTE_GPU, "health", boom)
    report = backends.health_report()
    assert report["remote_gpu"].connected is False
    assert "tunnel exploded" in report["remote_gpu"].reason
    assert "local" in report, "a failing backend must not take the report down"


def test_health_known_distinguishes_silence_from_disconnection():
    assert Health(connected=False).known is False
    assert Health(connected=False, features=frozenset({"preview"})).known is True
    assert Health(connected=True).known is True


# ---- registration ---------------------------------------------------------
def test_a_second_backend_can_be_registered():
    """The extension point: a second notebook, a LAN box, a rented GPU."""
    class Extra:
        id = "second-colab"
        label = "Second notebook"
        kind = BackendKind.remote

        def health(self): return Health(connected=True, device="A100")
        def catalog(self): return []
        def features(self): return frozenset({"image"})
        def has_resource(self, digest): return False

    backends.register(Extra())
    try:
        assert backends.get("second-colab") is not None
        assert backends.has_feature("image", backend_id="second-colab") is True
    finally:
        backends._REGISTRY.pop("second-colab", None)


def test_remote_lifecycle_methods_explain_why_they_are_not_implemented():
    """Remote GPU work goes through remote_gpu_client.run_remote as one unit. The stubs
    say so rather than failing obscurely."""
    with pytest.raises(NotImplementedError, match="run_remote"):
        REMOTE_GPU.submit("/generate", {})
