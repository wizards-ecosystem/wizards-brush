from backend.app.generators.base import ASPECTS, dims_for, res_for


def test_res_for_known_and_fallback():
    assert res_for("720p", "landscape") == (1280, 704)
    assert res_for("480p", "portrait") == (480, 832)
    assert res_for("nope", "nope") == (1024, 1024)


def test_dims_snapped_to_16():
    for aspect in ASPECTS:
        for tier in ("Draft", "Standard", "High"):
            for device in ("local", "a100"):
                w, h = dims_for(aspect=aspect, tier=tier, device=device)
                assert w % 16 == 0 and h % 16 == 0, (aspect, tier, device)


def test_dims_respect_device_cap():
    w, h = dims_for(aspect="16:9", tier="High", device="local")
    assert max(w, h) <= 1280
    w, h = dims_for(aspect="16:9", tier="High", device="a100")
    assert max(w, h) <= 1664


def test_dims_aspect_orientation():
    w, h = dims_for(aspect="9:16", tier="Standard", device="local")
    assert h > w
    w, h = dims_for(aspect="16:9", tier="Standard", device="local")
    assert w > h
    w, h = dims_for(aspect="1:1", tier="Standard", device="local")
    assert w == h


def test_named_aspects_are_exact_after_latent_snapping():
    for label, (rw, rh) in ASPECTS.items():
        w, h = dims_for(aspect=label, tier="Standard", device="local")
        assert w * rh == h * rw, (label, w, h)


def test_match_input_ratio_uses_tier_budget_not_literal_source_size():
    from backend.app.generators.base import dims_for_ratio

    w, h = dims_for_ratio(4000, 1000, tier="Draft", device="local")
    assert w <= 1280 and h <= 1280
    assert abs(w / h - 4.0) < 0.2
    assert (w, h) != (4000, 1000)


def test_dims_explicit_size_wins_but_capped():
    assert dims_for(device="local", width=1024, height=1024) == (1024, 1024)
    w, h = dims_for(device="local", width=4000, height=2000)
    assert max(w, h) <= 1280
    # aspect preserved (~2:1) rather than distorted by the cap
    assert abs(w / h - 2.0) < 0.1


def test_higher_tier_means_more_pixels():
    small = dims_for(aspect="1:1", tier="Draft", device="local")
    big = dims_for(aspect="1:1", tier="High", device="local")
    assert big[0] * big[1] > small[0] * small[1]
