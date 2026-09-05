"""A cold model fetch must not be the operation that fills the app's volume."""
from __future__ import annotations

import pytest

from backend.app.generators import local_image, vram


def test_ready_model_needs_no_download_headroom(monkeypatch):
    import backend.app.backends.local as local

    monkeypatch.setattr(local, "_cache_status", lambda model: ("ready", 12.0))
    monkeypatch.setattr(vram, "free_model_disk_bytes", lambda: 1)
    assert vram.model_download_shortfall("acme/ready") is None


@pytest.mark.parametrize("status", ["absent", "partial"])
def test_cold_or_partial_model_preserves_fetch_floor_and_reserve(monkeypatch, status):
    import backend.app.backends.local as local

    monkeypatch.setattr(local, "_cache_status", lambda model: (status, None))
    required = vram.MIN_MODEL_FETCH_BYTES + vram.DISK_RESERVE_BYTES
    monkeypatch.setattr(vram, "free_model_disk_bytes", lambda: required - 1)
    assert vram.model_download_shortfall("acme/large") == (required, required - 1)

    monkeypatch.setattr(vram, "free_model_disk_bytes", lambda: required)
    assert vram.model_download_shortfall("acme/large") is None


def test_unknown_free_space_never_causes_a_false_refusal(monkeypatch):
    import backend.app.backends.local as local

    monkeypatch.setattr(local, "_cache_status", lambda model: ("absent", None))
    monkeypatch.setattr(vram, "free_model_disk_bytes", lambda: None)
    assert vram.model_download_shortfall("acme/large") is None


def test_loader_refuses_with_specific_numbers_before_downloading(monkeypatch):
    required = vram.MIN_MODEL_FETCH_BYTES + vram.DISK_RESERVE_BYTES
    monkeypatch.setattr(vram, "model_download_shortfall", lambda model: (required, 2 * 1024 ** 3))
    events = []

    class Reporter:
        def stage(self, name, detail, **extra):
            events.append((name, detail))

    with pytest.raises(RuntimeError, match=r"2\.0 GB is free.*8 GB plus a 1 GB"):
        local_image._assert_download_space("acme/huge-model", Reporter())
    assert events == [("resolving", "checking free disk for huge-model")]
