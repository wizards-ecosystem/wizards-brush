"""Allocator cleanup should be pressure-driven, and bad pixels should be visible."""
from __future__ import annotations

from PIL import Image

from backend.app.generators import local_image


def test_cuda_cache_is_kept_when_it_is_small_enough_to_reuse():
    gib = 1024 ** 3
    assert not local_image._should_empty_cuda_cache(
        reserved=5 * gib, active=4 * gib, driver_free=8 * gib,
    )


def test_cuda_cache_is_returned_when_it_hoards_over_a_quarter_of_free_capacity():
    gib = 1024 ** 3
    assert local_image._should_empty_cuda_cache(
        reserved=8 * gib, active=4 * gib, driver_free=4 * gib,
    )


def test_flat_output_adds_a_visible_warning():
    warnings: list[str] = []
    local_image._warn_degenerate_output(Image.new("RGB", (16, 16), "black"), warnings)
    assert warnings and "single flat color" in warnings[0]


def test_nonflat_output_is_unaffected():
    image = Image.new("RGB", (2, 1), "black")
    image.putpixel((1, 0), (255, 255, 255))
    warnings: list[str] = []
    local_image._warn_degenerate_output(image, warnings)
    assert warnings == []


def test_warm_up_runs_a_real_tiny_forward(monkeypatch):
    seen = {}
    monkeypatch.setattr(local_image, "generate", lambda **kwargs: seen.update(kwargs))
    local_image.warm_up()
    assert seen["mode"] == "txt2img"
    assert seen["steps"] == 1
    assert (seen["width"], seen["height"]) == (256, 256)


def test_prompt_tokenizer_limit_becomes_a_visible_warning():
    class Tokenizer:
        model_max_length = 5

        def __call__(self, text, **_kwargs):
            return {"input_ids": text.split()}

    class Pipe:
        tokenizer = Tokenizer()

    warnings: list[str] = []
    local_image._warn_prompt_truncation(
        Pipe(), "one two three four five six", "short", warnings)
    assert len(warnings) == 1
    assert "6 tokens" in warnings[0] and "only 5" in warnings[0]
