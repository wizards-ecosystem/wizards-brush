"""The guard that stops a CUDA OOM from becoming a machine-killing host OOM.

`enable_model_cpu_offload` keeps every component in HOST RAM. When a bnb load
fails and we fall back to bf16+offload, a 12B transformer plus a T5-XXL text
encoder wants ~34 GB of system memory — and asking a 20 GB VM for it does not
raise. The kernel OOM killer fires and takes the process, and under WSL the VM
goes with it. That is a hard crash mid-session, not a failed job.

So the fallback has to ask first. These tests pin the three answers that matter:
it refuses when the host demonstrably cannot back the load, it proceeds when it
can, and it proceeds when it cannot tell.
"""
from __future__ import annotations

from backend.app.generators import vram


def test_it_refuses_when_the_host_cannot_back_the_weights(monkeypatch):
    monkeypatch.setattr(vram, "model_weight_bytes", lambda m: 34 * 1024 ** 3)
    monkeypatch.setattr(vram, "free_host_ram_bytes", lambda: 20 * 1024 ** 3)
    shortfall = vram.host_ram_shortfall("black-forest-labs/FLUX.1-dev")
    assert shortfall is not None
    need, free = shortfall
    assert need > free


def test_it_proceeds_when_the_weights_fit(monkeypatch):
    monkeypatch.setattr(vram, "model_weight_bytes", lambda m: 6 * 1024 ** 3)
    monkeypatch.setattr(vram, "free_host_ram_bytes", lambda: 20 * 1024 ** 3)
    assert vram.host_ram_shortfall("an/sdxl-checkpoint") is None


def test_an_unknown_size_never_grounds_a_model(monkeypatch):
    """None means "not downloaded yet", which is exactly when we most want the
    load to proceed — refusing on an unknown would ground every new model."""
    monkeypatch.setattr(vram, "model_weight_bytes", lambda m: None)
    monkeypatch.setattr(vram, "free_host_ram_bytes", lambda: 1024)
    assert vram.host_ram_shortfall("never/downloaded") is None

    monkeypatch.setattr(vram, "model_weight_bytes", lambda m: 99 * 1024 ** 3)
    monkeypatch.setattr(vram, "free_host_ram_bytes", lambda: None)
    assert vram.host_ram_shortfall("unreadable/meminfo") is None


def test_the_safety_margin_refuses_a_load_that_only_just_fits(monkeypatch):
    """Weights are not the whole footprint: the allocator, the CUDA context and
    pinned staging buffers sit alongside them. A model that exactly equals free
    RAM does not fit."""
    monkeypatch.setattr(vram, "model_weight_bytes", lambda m: 20 * 1024 ** 3)
    monkeypatch.setattr(vram, "free_host_ram_bytes", lambda: 20 * 1024 ** 3)
    assert vram.host_ram_shortfall("exactly/borderline") is not None


def test_weight_bytes_follows_the_blob_symlinks(monkeypatch, tmp_path):
    """A HF snapshot directory is all symlinks into blobs/. Stat'ing the link
    instead of the target reports a few bytes and would make every model look
    like it fits."""
    from backend.app.config import settings

    repo = tmp_path / "models--acme--big" / "snapshots" / "abc123"
    blobs = tmp_path / "models--acme--big" / "blobs"
    repo.mkdir(parents=True)
    blobs.mkdir(parents=True)
    blob = blobs / "deadbeef"
    blob.write_bytes(b"0" * 4096)
    (repo / "model.safetensors").symlink_to(blob)
    (repo / "config.json").write_text("{}")      # not a weight file

    monkeypatch.setattr(type(settings), "hf_hub_path", property(lambda self: tmp_path))
    assert vram.model_weight_bytes("acme/big") == 4096


def test_nunchaku_costs_only_the_composed_artifacts(monkeypatch, tmp_path):
    """The base transformer is replaced by one checkpoint from another repo.

    Counting the whole base snapshot was both structurally wrong and large
    enough to reject a supported 16 GB host.  Multiple cached precision
    variants are alternatives, not simultaneous residents.
    """
    from backend.app.config import settings
    from backend.app.generators import quant

    def snapshot(model: str):
        root = tmp_path / f"models--{model.replace('/', '--')}"
        snap = root / "snapshots" / "revision"
        blobs = root / "blobs"
        snap.mkdir(parents=True)
        blobs.mkdir(parents=True)
        return snap, blobs

    def weight(snap, blobs, relative: str, size: int):
        blob = blobs / relative.replace("/", "-")
        blob.write_bytes(b"x" * size)
        target = snap / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(blob)

    model = "Tongyi-MAI/Z-Image-Turbo"
    base, base_blobs = snapshot(model)
    weight(base, base_blobs, "transformer/model.safetensors", 30_000)
    weight(base, base_blobs, "text_encoder/model.safetensors", 8_000)
    weight(base, base_blobs, "vae/model.safetensors", 1_000)

    repo = quant.NUNCHAKU_REPOS[model]
    nunchaku, nunchaku_blobs = snapshot(repo)
    weight(nunchaku, nunchaku_blobs,
           "svdq-int4_r128-z-image-turbo.safetensors", 4_000)
    weight(nunchaku, nunchaku_blobs,
           "svdq-nvfp4_r128-z-image-turbo.safetensors", 3_500)

    monkeypatch.setattr(type(settings), "hf_hub_path", property(lambda self: tmp_path))
    expected = int((9_000 * vram.host_fraction("4bit") + 4_000) * vram.HOST_SAFETY)
    monkeypatch.setattr(vram, "free_host_ram_bytes", lambda: expected - 1)
    assert vram.host_ram_shortfall(model, "nunchaku") == (expected, expected - 1)

    # The old whole-base estimate was materially larger and would have refused
    # a machine with ample room for the composed load.
    old = int(39_000 * vram.host_fraction("nunchaku") * vram.HOST_SAFETY)
    assert expected < old


def test_nunchaku_unknown_composition_keeps_the_conservative_fallback(monkeypatch):
    monkeypatch.setattr(vram, "_nunchaku_host_weight_bytes", lambda model: None)
    monkeypatch.setattr(vram, "model_weight_bytes", lambda model: 20 * 1024 ** 3)
    monkeypatch.setattr(vram, "free_host_ram_bytes", lambda: 8 * 1024 ** 3)
    assert vram.host_ram_shortfall("some/model", "nunchaku") is not None


def test_a_big_model_loads_into_an_idle_machine_and_not_into_a_full_one(monkeypatch):
    """The distinction the guard actually has to make.

    Z-Image-Turbo (30.6 GB of weights) and FLUX.1-dev (31.4 GB) are the same
    size to within 3%. On the 4-bit path Turbo loads fine here and FLUX took out
    16 GB of swap — so SIZE is not what separates them, and a threshold tuned to
    reject one by size rejects both. What separated them was headroom at the
    moment they started: one had an idle machine, the other had 0.7 GB free
    because other jobs were already resident.
    """
    monkeypatch.setattr(vram, "model_weight_bytes", lambda m: 31 * 1024 ** 3)

    monkeypatch.setattr(vram, "free_host_ram_bytes", lambda: 18 * 1024 ** 3)
    assert vram.host_ram_shortfall("big/model", "4bit") is None, "idle: must not block real work"

    monkeypatch.setattr(vram, "free_host_ram_bytes", lambda: 1 * 1024 ** 3)
    assert vram.host_ram_shortfall("big/model", "4bit") is not None, "under pressure: refuse"


def test_full_precision_is_refused_where_quantised_is_not(monkeypatch):
    """bf16 keeps the entire tensor set in host RAM, and with offload it stays
    there. 31 GB of weights genuinely cannot go into a 19 GiB VM that way, even
    though the same model is fine at 4-bit."""
    monkeypatch.setattr(vram, "model_weight_bytes", lambda m: 31 * 1024 ** 3)
    monkeypatch.setattr(vram, "free_host_ram_bytes", lambda: 18 * 1024 ** 3)
    assert vram.host_ram_shortfall("big/model", "bf16") is not None
    assert vram.host_ram_shortfall("big/model", "4bit") is None


def test_a_quantised_load_is_allowed_where_full_precision_is_not(monkeypatch):
    """The factor has to actually change the answer, or it is decoration — and
    refusing a 4-bit load that would have fitted is as much a bug as allowing a
    bf16 one that will not."""
    monkeypatch.setattr(vram, "model_weight_bytes", lambda m: 20 * 1024 ** 3)
    monkeypatch.setattr(vram, "free_host_ram_bytes", lambda: 16 * 1024 ** 3)
    assert vram.host_ram_shortfall("mid/model", "bf16") is not None
    assert vram.host_ram_shortfall("mid/model", "4bit") is None


def test_an_unknown_backend_is_costed_as_full_precision(monkeypatch):
    """The safe direction for a name nobody has measured."""
    assert vram.host_fraction("something-new") == 1.0
    assert vram.host_fraction("4bit") < 1.0


def test_4bit_guard_is_calibrated_below_the_old_overconservative_cutoff():
    """Keep a margin without rejecting a measured 4-bit load by megabytes."""
    assert 0.4 < vram.host_fraction("4bit") < 0.45


def test_it_waits_for_memory_instead_of_failing_the_job(monkeypatch):
    """Being short of RAM is a moment, not a verdict.

    A model swap frees gigabytes a moment later, so a job that merely arrived
    early was being marked failed forever for a condition that had already
    passed. It has to retry before it gives up.
    """
    from backend.app.generators import local_image

    calls = {"n": 0}

    def shortfall(model, quant="bf16"):
        calls["n"] += 1
        return (13 * 1024 ** 3, 4 * 1024 ** 3) if calls["n"] < 3 else None

    monkeypatch.setattr(vram, "host_ram_shortfall", shortfall)
    monkeypatch.setattr(local_image, "_free", lambda: None)
    monkeypatch.setattr(local_image, "_RAM_WAIT_SECONDS", 0.0)

    stages: list[tuple] = []
    rep = type("R", (), {"stage": lambda self, *a, **k: stages.append(a)})()
    local_image._await_host_ram("some/model", "4bit", rep)   # must return, not raise

    assert calls["n"] == 3, "it should have re-checked until memory came free"
    assert stages, "the wait has to be visible, not look like a hang"


def test_it_still_gives_up_rather_than_stalling_the_queue_forever(monkeypatch):
    """Waiting without limit on memory that is never coming back is a stalled
    queue with no explanation — worse than a job that failed for a stated
    reason. The reason has to name the numbers."""
    import pytest

    from backend.app.generators import local_image

    monkeypatch.setattr(vram, "host_ram_shortfall",
                        lambda m, q="bf16": (13 * 1024 ** 3, 4 * 1024 ** 3))
    monkeypatch.setattr(local_image, "_free", lambda: None)
    monkeypatch.setattr(local_image, "_RAM_WAIT_SECONDS", 0.0)
    rep = type("R", (), {"stage": lambda self, *a, **k: None})()

    with pytest.raises(RuntimeError, match=r"13\.0 GB"):
        local_image._await_host_ram("some/model", "4bit", rep)
