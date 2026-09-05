"""Estimating VRAM before spending it, so tiling is a decision rather than a
permanent default paid for by every generation."""
from __future__ import annotations

import pytest

from backend.app.generators import vram


def test_the_estimate_scales_with_output_area():
    small = vram.decode_bytes(512, 512)
    big = vram.decode_bytes(1024, 1024)
    assert big == pytest.approx(4 * small, rel=0.01), "four times the pixels"


def test_fp32_costs_twice_fp16():
    assert vram.decode_bytes(512, 512, fp32=True) == 2 * vram.decode_bytes(512, 512)


def test_a_tiled_estimate_is_per_tile_not_per_image():
    """This is what makes 'would tiling fit?' answerable."""
    full = vram.decode_bytes(4096, 4096)
    tiled = vram.decode_bytes(4096, 4096, tile_size=512)
    assert tiled < full / 10


def test_the_safety_margin_is_applied():
    raw = 1024 * 1024 * 2 * vram.DECODE_PER_PIXEL
    assert vram.decode_bytes(1024, 1024) > raw, "margin absorbs fragmentation"


def test_unknown_free_vram_means_do_not_tile(monkeypatch):
    """Tiling on a guess would cost quality and time on every generation for a
    problem that may not exist."""
    monkeypatch.setattr(vram, "free_vram_bytes", lambda: None)
    assert vram.should_tile_decode(4096, 4096) is False


def test_a_decode_that_fits_comfortably_is_not_tiled(monkeypatch):
    monkeypatch.setattr(vram, "free_vram_bytes", lambda: 40 * 1024 ** 3)
    assert vram.should_tile_decode(1024, 1024) is False


def test_a_decode_that_would_not_fit_is_tiled(monkeypatch):
    monkeypatch.setattr(vram, "free_vram_bytes", lambda: 1 * 1024 ** 3)
    assert vram.should_tile_decode(4096, 4096) is True


def test_the_threshold_leaves_headroom(monkeypatch):
    """Deciding at exactly 100% of free VRAM would tile only after it was already
    too late — the allocator needs room to work in."""
    needed = vram.decode_bytes(2048, 2048)
    monkeypatch.setattr(vram, "free_vram_bytes", lambda: int(needed * 0.95))
    assert vram.should_tile_decode(2048, 2048) is True


def test_free_vram_is_none_rather_than_zero_when_torch_is_unavailable(monkeypatch):
    """None and zero are very different answers: zero would mean 'nothing fits'
    and would tile every generation. Probed without importing torch, which this
    suite forbids."""
    import builtins

    real = builtins.__import__

    def no_torch(name, *a, **kw):
        if name == "torch":
            raise ImportError("no torch here")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_torch)
    assert vram.free_vram_bytes() is None


def test_free_vram_counts_allocator_blocks_that_can_be_reclaimed():
    gib = 1024 ** 3
    assert vram._free_plus_reclaimable(2 * gib, 5 * gib, 3 * gib) == 4 * gib
    assert vram._free_plus_reclaimable(2 * gib, 3 * gib, 5 * gib) == 2 * gib


def test_peak_vram_reports_live_and_reserved_high_water_marks(monkeypatch):
    import sys
    from types import SimpleNamespace

    cuda = SimpleNamespace(
        is_available=lambda: True,
        max_memory_allocated=lambda: 3 * 1024 ** 3,
        max_memory_reserved=lambda: 4 * 1024 ** 3,
    )
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    assert vram.peak_vram() == {"peak_vram_gb": 3.0, "peak_reserved_vram_gb": 4.0}


def test_describe_reports_the_numbers_behind_the_decision(monkeypatch):
    monkeypatch.setattr(vram, "free_vram_bytes", lambda: 16 * 1024 ** 3)
    got = vram.describe(1024, 1024)
    assert got["output"] == "1024x1024"
    assert got["decode_estimate_gb"] > 0
    assert got["tile_size"] == vram.VAE_TILE_SIZE
    assert got["tile_estimate_gb"] > 0
    assert got["free_vram_gb"] == 16.0
    assert got["would_tile"] is False


def test_real_tiling_geometry_matches_the_estimator(monkeypatch):
    from types import SimpleNamespace

    from backend.app.generators import local_image

    class Vae:
        tile_sample_min_size = 128
        tile_latent_min_size = 16
        tile_overlap_factor = 0.1
        enabled = False

        def enable_tiling(self):
            self.enabled = True

        def disable_tiling(self):
            self.enabled = False

    vae = Vae()
    pipe = SimpleNamespace(vae=vae, vae_scale_factor=8)
    monkeypatch.setattr(vram, "should_tile_decode", lambda *_a, **_k: True)
    local_image._set_vae_tiling(pipe, 2048, 2048)

    assert vae.enabled is True
    assert vae.tile_sample_min_size == vram.VAE_TILE_SIZE
    assert vae.tile_latent_min_size == vram.VAE_TILE_SIZE // 8
    assert vae.tile_overlap_factor == vram.VAE_TILE_OVERLAP
