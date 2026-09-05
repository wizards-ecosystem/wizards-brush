from backend.app.utils.io import stamp_name
from backend.app.utils.seeds import resolve_seed


def test_resolve_seed_random_when_negative_or_none():
    for v in (None, -1, -99, "nan"):
        s = resolve_seed(v)
        assert 0 <= s < 2**32 - 1


def test_resolve_seed_zero_is_fixed():
    assert resolve_seed(0) == 0


def test_resolve_seed_clamps_max():
    assert resolve_seed(2**50) == 2**32 - 1


def test_stamp_name_unique_and_shaped():
    names = {stamp_name("img", "png") for _ in range(50)}
    assert len(names) == 50
    assert all(n.startswith("img_") and n.endswith(".png") for n in names)
