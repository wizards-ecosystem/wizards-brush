from backend.app.presets import FACE_NEGATIVE, IMAGE_NEGATIVE, WAN_NEGATIVE_ZH
from backend.app.prompt_engine import (
    clamp_seed,
    expand_all,
    expand_prompt,
    has_wildcards,
    prepare_prompt,
    resolve_negative,
    seed_for,
    strip_a1111_emphasis,
)


def test_expand_no_wildcards_passthrough():
    assert expand_prompt("a plain prompt", 3) == "a plain prompt"
    assert expand_prompt("", 0) == ""
    assert expand_prompt("keep {this} literal", 2) == "keep {this} literal"


def test_expand_single_group_cycles():
    p = "a {red|blue|green} car"
    assert expand_prompt(p, 0) == "a red car"
    assert expand_prompt(p, 1) == "a blue car"
    assert expand_prompt(p, 2) == "a green car"
    assert expand_prompt(p, 3) == "a red car"  # wraps


def test_expand_multi_group_cartesian():
    p = "{a|b} {x|y}"
    seen = {expand_prompt(p, i) for i in range(4)}
    assert seen == {"a x", "b x", "a y", "b y"}


def test_expand_strips_whitespace_in_options():
    assert expand_prompt("{ red | blue }", 0) == "red"


def test_a1111_emphasis_is_normalized_without_damaging_escaped_text():
    assert strip_a1111_emphasis(
        r"(masterpiece:1.4), [soft], \(literal\), unmatched (mark"
    ) == "masterpiece, soft, (literal), unmatched (mark"


def test_prompt_syntax_is_an_explicit_replayable_choice():
    prompt = "portrait, (fine detail:1.25), [soft light]"
    assert prepare_prompt(prompt, syntax="literal") == prompt
    assert prepare_prompt(prompt, syntax="a1111") == "portrait, fine detail, soft light"


def test_capped_combinations_sample_the_whole_product():
    prompt = "{" + "|".join(f"a{i}" for i in range(8)) + "} " \
             + "{" + "|".join(f"b{i}" for i in range(8)) + "} {c0|c1}"
    sampled = expand_all(prompt, cap=32)
    assert len(sampled) == 32
    assert {item.rsplit(" ", 1)[-1] for item in sampled} == {"c0", "c1"}


def test_has_wildcards():
    assert has_wildcards("a {b|c}")
    assert not has_wildcards("a {b}")
    assert not has_wildcards("")


def test_resolve_negative_explicit_wins():
    assert resolve_negative("t2v", "my negative", auto=True) == "my negative"


def test_resolve_negative_auto_defaults():
    assert resolve_negative("t2v", "", auto=True) == WAN_NEGATIVE_ZH
    assert resolve_negative("img2img", "", auto=True) == FACE_NEGATIVE
    assert resolve_negative("image_local", "", auto=True) == IMAGE_NEGATIVE
    assert resolve_negative("image_local", "", auto=False) == ""


def test_seed_for_modes():
    assert seed_for(100, 3, "increment") == 103
    assert seed_for(100, 3, "fixed") == 100
    r = seed_for(100, 3, "random")
    assert 0 <= r < 2**32 - 1


def test_clamp_seed_bounds():
    assert clamp_seed(-5) == 0
    assert clamp_seed(2**40) == 2**32 - 1
    assert clamp_seed(42) == 42


def test_long_video_shots_are_reproducible_across_reruns():
    """Regression: the long-video handler called expand_prompt without seed=, so
    prompt_engine took its random.choice branch and every rerun of an expensive
    multi-shot job resolved __wildcards__ differently."""
    import backend.app.prompt_engine as pe

    pe.save_wildcard("t_mood", ["calm", "tense", "eerie", "bright"])
    try:
        prompt = "a __t_mood__ street"
        first = [pe.expand_prompt(prompt, i, seed=42) for i in range(4)]
        again = [pe.expand_prompt(prompt, i, seed=42) for i in range(4)]
        assert first == again, "same seed must reproduce the same shot prompts"
        # a different seed should genuinely re-roll (not pinned to one value)
        other = [pe.expand_prompt(prompt, i, seed=99) for i in range(4)]
        assert any(a != b for a, b in zip(first, other, strict=True))
    finally:
        pe.delete_wildcard("t_mood")


def test_seedless_expansion_is_still_random_for_callers_that_want_it():
    import backend.app.prompt_engine as pe

    pe.save_wildcard("t_many", [f"opt{i}" for i in range(40)])
    try:
        seen = {pe.expand_prompt("x __t_many__", 0) for _ in range(30)}
        assert len(seen) > 1, "seed=None must keep picking randomly"
    finally:
        pe.delete_wildcard("t_many")
