"""Hardware profiles: pick defaults that suit the machine, visibly."""
from __future__ import annotations

import pytest

from backend.app import hardware
from backend.app.config import settings
from backend.app.generators.base import batch_for, dims_for


def test_profiles_are_ordered_by_the_card_they_need():
    mins = [p.min_vram_gb for p in hardware.PROFILES]
    assert mins == sorted(mins), "profile_for walks the list and keeps the last match"


@pytest.mark.parametrize(("vram", "expect"), [
    (None, "16gb"),   # unprobed: assume a normal GPU box, as before
    (0.0, "cpu"),
    (6.0, "cpu"),     # below the 8 GB threshold
    (8.0, "8gb"),
    (12.0, "12gb"),
    (16.0, "16gb"),
    (24.0, "24gb"),
    (80.0, "24gb"),   # bigger than anything we model
])
def test_profile_chosen_by_vram(vram, expect):
    assert hardware.profile_for(vram).name == expect


def test_an_explicit_override_wins(monkeypatch):
    monkeypatch.setattr(settings, "hardware_profile", "8gb")
    assert hardware.active().name == "8gb"


def test_an_unknown_override_falls_back_rather_than_raising(monkeypatch):
    monkeypatch.setattr(settings, "hardware_profile", "banana")
    assert hardware.active().name == hardware.FALLBACK.name


def test_auto_means_detect(monkeypatch):
    monkeypatch.setattr(settings, "hardware_profile", "auto")
    assert hardware.active().name in {p.name for p in hardware.PROFILES}


def test_completed_no_cuda_probe_selects_cpu(monkeypatch):
    from backend.app.backends import local

    monkeypatch.setattr(settings, "hardware_profile", "auto")
    monkeypatch.setattr(local, "_DEVICE", ("CPU only", 0.0))
    assert hardware.active().name == "cpu"


def test_system_status_handles_multiple_gpus(monkeypatch):
    from types import SimpleNamespace

    from backend.app.routers import system

    output = "Small Card, 8192, 100, 10, 40\nBig Card, 24576, 200, 20, 50\n"
    monkeypatch.setattr(system.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(
        returncode=0, stdout=output,
    ))
    result = system._local_gpu()
    assert result["available"] is True
    assert result["name"] == "Big Card"
    assert result["memory_total_mb"] == 24576
    assert result["device_count"] == 2


def test_describe_shows_the_reasoning_not_just_the_answer(monkeypatch):
    """A user whose card was mis-detected needs to see that, not just get
    smaller images with no explanation."""
    monkeypatch.setattr(settings, "hardware_profile", "12gb")
    d = hardware.describe()
    assert d["profile"] == "12gb"
    assert d["source"] == "override"
    assert d["device"], "always name the device, even when it is 'not detected'"
    assert {"name", "label"} <= set(d["available"][0])


def test_describe_reports_loader_configuration_not_dead_profile_advice(monkeypatch):
    monkeypatch.setattr(settings, "hardware_profile", "16gb")
    monkeypatch.setattr(settings, "local_quant", "4bit")
    monkeypatch.setattr(settings, "local_offload", True)
    described = hardware.describe()
    assert described["profile"] == "16gb"
    assert described["quant"] == "4bit"
    assert described["offload"] is True


def test_sizing_follows_the_profile(monkeypatch):
    """The whole point: an 8 GB card must not inherit 16 GB targets."""
    monkeypatch.setattr(settings, "hardware_profile", "8gb")
    small = dims_for(aspect="1:1", tier="High", device="local")
    monkeypatch.setattr(settings, "hardware_profile", "24gb")
    large = dims_for(aspect="1:1", tier="High", device="local")
    assert small[0] * small[1] < large[0] * large[1]


def test_the_a100_is_unaffected_by_the_local_profile(monkeypatch):
    monkeypatch.setattr(settings, "hardware_profile", "cpu")
    assert dims_for(aspect="1:1", tier="High", device="a100")[0] >= 1024


def test_qwen_a100_high_uses_the_official_2x3_bucket():
    assert dims_for(aspect="2:3", tier="High", device="a100", family="qwen") == (1056, 1584)


def test_sizing_never_fails_even_if_the_hardware_layer_does(monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(hardware, "active", boom)
    w, h = dims_for(aspect="16:9", tier="Standard", device="local")
    assert w > 0 and h > 0, "refusing to pick a resolution means refusing to generate"


# ---- batch sizing ---------------------------------------------------------
def test_serial_batch_count_does_not_change_with_image_area(monkeypatch):
    monkeypatch.setattr(settings, "hardware_profile", "16gb")
    small = batch_for(512, 512, requested=8)
    large = batch_for(1280, 1280, requested=8)
    assert large == small == 8, "items run one at a time; area changes time, not batch peak"


def test_batch_never_drops_below_one(monkeypatch):
    monkeypatch.setattr(settings, "hardware_profile", "cpu")
    assert batch_for(4096, 4096, requested=8) == 1


def test_batch_never_exceeds_what_was_asked_for(monkeypatch):
    monkeypatch.setattr(settings, "hardware_profile", "24gb")
    assert batch_for(256, 256, requested=2) == 2


def test_batch_respects_the_profile_hard_cap(monkeypatch):
    monkeypatch.setattr(settings, "hardware_profile", "8gb")
    assert batch_for(256, 256, requested=8) <= hardware.by_name("8gb").max_batch


def test_serial_batch_count_reaches_the_generate_route(client, no_queue):
    import json

    from backend.app import db

    def submit(**kw):
        r = client.post("/api/generate/image/local",
                        data={"payload": json.dumps({"prompt": "p", "batch": 8, **kw})})
        assert r.status_code == 200
        return db.get_job(r.json()["job_id"]).params

    big = submit(aspect="Custom", width=1280, height=1280)
    small = submit(aspect="Custom", width=384, height=384)
    assert small["batch"] == big["batch"] == 8
