"""The pure core of Variant Sets: axis expansion, canonical keys, safe
templating, and deterministic naming. No database, no queue, no images."""
from __future__ import annotations

import pytest

from backend.app.variant_sets import expansion as ex
from backend.app.variant_sets import naming
from backend.app.variant_sets import templates as tpl


def _axes(**spec):
    return [ex.make_axis(name, values) for name, values in spec.items()]


# ---- Cartesian expansion ----------------------------------------------------
def test_product_is_every_combination_in_nested_loop_order():
    axes = _axes(material=["wood", "steel", "glass"], color=["red", "green"],
                 lighting=["soft", "dramatic"])
    combos = list(ex.combinations(axes))
    assert ex.count(axes) == len(combos) == 12
    assert len({c.key for c in combos}) == 12
    # Last axis fastest, first axis slowest — exactly nested loops.
    assert [c.as_dict() for c in combos[:3]] == [
        {"material": "wood", "color": "red", "lighting": "soft"},
        {"material": "wood", "color": "red", "lighting": "dramatic"},
        {"material": "wood", "color": "green", "lighting": "soft"},
    ]
    assert combos[-1].as_dict() == {"material": "glass", "color": "green", "lighting": "dramatic"}
    assert [c.index for c in combos] == list(range(12))


def test_order_and_keys_are_deterministic_across_runs():
    spec = {"a": ["x", "y", "z"], "b": ["1", "2"]}
    first = [(c.index, c.key) for c in ex.combinations(_axes(**spec))]
    second = [(c.index, c.key) for c in ex.combinations(_axes(**spec))]
    assert first == second


def test_the_engine_has_no_domain_vocabulary():
    """A locale x layout recipe behaves exactly like any other."""
    combos = list(ex.combinations(_axes(locale=["en", "fr", "de"], layout=["compact", "spacious"])))
    assert len(combos) == 6
    assert combos[0].key == "locale=en,layout=compact"
    assert combos[5].key == "locale=de,layout=spacious"


def test_no_axes_is_one_combination():
    combos = list(ex.combinations([]))
    assert ex.count([]) == 1
    assert len(combos) == 1 and combos[0].key == "" and combos[0].as_dict() == {}


# ---- canonical keys ------------------------------------------------------------
def test_keys_are_built_from_identity_not_position():
    before = {c.as_dict()["b"]: c.key for c in ex.combinations(_axes(a=["x"], b=["one", "two"]))}
    after = {c.as_dict()["b"]: c.key for c in ex.combinations(_axes(a=["x"], b=["zero", "one", "two"]))}
    assert before["one"] == after["one"] and before["two"] == after["two"]


def test_key_uses_identifiers():
    [combo] = ex.combinations(_axes(finish=["Brushed Steel"], size=["2x"]))
    assert combo.key == "finish=brushed-steel,size=2x"


def test_slug_keeps_letters_of_any_script_and_drops_underscores():
    assert ex.slug("Dark  Grey!") == "dark-grey"
    assert ex.slug("snake_case") == "snake-case"
    assert ex.slug("Ｆｕｌｌｗｉｄｔｈ") == "fullwidth"
    assert ex.slug("日本語") == "日本語"
    assert "_" not in ex.slug("a__b__c")


# ---- validation ------------------------------------------------------------------
@pytest.mark.parametrize("values, fragment", [
    (["red", "red"], "appears twice"),
    (["Red", "red"], "both reduce to the identifier 'red'"),
    (["dark grey", "dark-grey"], "both reduce"),
    ([""], "empty value"),
    (["  "], "empty value"),
    (["{{other}}"], "never expanded"),
    (["a}}b"], "never expanded"),
    (["bad\x07value"], "control character"),
    (["!!!"], "no letters or digits"),
    (["x" * (ex.MAX_VALUE_CHARS + 1)], "longer than"),
    ([True], "must be text"),
    ([], "at least one value"),
])
def test_ambiguous_or_unusable_values_are_rejected(values, fragment):
    with pytest.raises(ex.ExpansionError, match=None) as caught:
        ex.make_axis("axis", values)
    assert fragment in str(caught.value)


@pytest.mark.parametrize("name", ["", "1st", "_private", "has space", "x" * 33, "dash-ed"])
def test_axis_names_must_be_identifiers(name):
    with pytest.raises(ex.ExpansionError, match="must start with a letter"):
        ex.make_axis(name, ["v"])


def test_numbers_are_accepted_as_text_values():
    assert ex.make_axis("size", [256, 512]).values == ("256", "512")


def test_duplicate_axis_names_are_rejected_case_insensitively():
    with pytest.raises(ex.ExpansionError, match="used twice"):
        ex.check_unique_names(_axes(color=["a"]) + _axes(Color=["b"]))
    with pytest.raises(ex.ExpansionError, match="used twice"):
        ex.check_unique_names(_axes(color=["a"]) + _axes(color=["b"]))


def test_too_many_values_is_an_error_not_a_truncation():
    with pytest.raises(ex.ExpansionError, match="the limit is 256"):
        ex.make_axis("n", [str(i) for i in range(ex.MAX_VALUES_PER_AXIS + 1)])


# ---- the cap -------------------------------------------------------------------------
def test_cap_is_far_above_the_grid_and_batch_limits():
    assert ex.DEFAULT_MAX_COMBINATIONS >= 500
    assert ex.check_cap([48], cap=ex.DEFAULT_MAX_COMBINATIONS) == 48


def test_cap_is_checked_arithmetically_before_anything_is_built():
    huge = [256] * 5   # 1.1e12 — would never finish if it were materialized
    with pytest.raises(ex.ExpansionError, match="limit is 1000"):
        ex.check_cap([ex.count([ex.Axis("a", tuple("x" * n))]) for n in huge], cap=1000)


def test_exactly_at_the_cap_is_allowed_one_over_is_refused():
    assert ex.check_cap([10, 10], cap=110) == 110
    with pytest.raises(ex.ExpansionError, match=r"10 \+ 100 = 110 generations; the limit is 109"):
        ex.check_cap([10, 10], cap=109)


def test_stage_totals_multiply_through_the_stages():
    assert ex.stage_totals([3, 2, 4]) == [3, 6, 24]


def test_configured_cap_is_clamped(monkeypatch):
    from backend.app.config import settings

    monkeypatch.setattr(settings, "variant_max_combinations", 10**9)
    assert ex.configured_cap() == ex.HARD_MAX_COMBINATIONS
    monkeypatch.setattr(settings, "variant_max_combinations", 0)
    assert ex.configured_cap() == 1


# ---- templating ------------------------------------------------------------------------
def test_substitution_and_content_maps():
    slots = {
        "material": tpl.Slot("wood"),
        "lighting": tpl.Slot("soft", "soft even neutral studio illumination"),
    }
    out = tpl.render("Rendered in {{material}}, {{ lighting }}; tag {{lighting.value}}", slots)
    assert out == "Rendered in wood, soft even neutral studio illumination; tag soft"


def test_check_returns_references_and_accepts_known_axes():
    assert tpl.check("{{b}} then {{a}} and {{b.value}}", field="prompt",
                     available={"a", "b"}) == ["b", "a"]


def test_unknown_placeholder_is_an_error():
    with pytest.raises(tpl.TemplateError, match=r"unknown placeholder \{\{colour\}\}"):
        tpl.check("in {{colour}}", field="prompt", available={"color"})


def test_placeholder_for_a_later_stage_is_a_distinct_error():
    with pytest.raises(tpl.TemplateError, match="defined by a later stage"):
        tpl.check("{{angle}}", field="stage 1 prompt", available={"material"}, later={"angle"})


@pytest.mark.parametrize("template", ["{{ }}", "{{1x}}", "open {{material", "close}} it",
                                      "{{a.b}}", "{{a}}}}"])
def test_malformed_braces_are_errors(template):
    with pytest.raises(tpl.TemplateError):
        tpl.check(template, field="prompt", available={"a", "material"})


def test_builtins_are_naming_only():
    with pytest.raises(tpl.TemplateError, match="only available in naming"):
        tpl.check("{{_index}}", field="prompt", available=set())
    assert tpl.check("{{_index}}-{{_key}}", field="name", available=set(), builtins=True) == [
        "_index", "_key"]


def test_expansion_is_single_pass_never_recursive():
    """Replacement text is never rescanned, so content that looks like a
    placeholder stays literal instead of expanding into another axis."""
    slots = {"a": tpl.Slot("x", "{{b}}"), "b": tpl.Slot("SECRET")}
    assert tpl.render("{{a}}", slots) == "{{b}}"


def test_single_braces_and_wildcards_pass_through_untouched():
    slots = {"a": tpl.Slot("x")}
    assert tpl.render("{red|blue} __mood__ {{a}}", slots) == "{red|blue} __mood__ x"
    assert tpl.check("{red|blue} __mood__ {{a}}", field="p", available={"a"}) == ["a"]


def test_empty_template_is_empty():
    assert tpl.render("", {}) == ""
    assert tpl.check("", field="p", available=set()) == []


# ---- naming ---------------------------------------------------------------------------------
def test_default_template_joins_every_axis_and_cannot_collide():
    # "a_b"+"c" and "a"+"b_c" would both render "a_b_c" if values kept "_".
    axes = _axes(first=["a_b", "a"], second=["c", "b_c"])
    template = naming.default_template([a.name for a in axes])
    names = {c.key: naming.render_name(template, c.as_dict()) for c in ex.combinations(axes)}
    assert naming.find_collisions(names) == {}
    assert names["first=a-b,second=c"] == "a-b_c.png"


def test_default_template_without_axes_uses_the_index():
    assert naming.default_template([]) == "{{_index}}"
    assert naming.render_name("{{_index}}", {}, builtins={"_index": "007"}) == "007.png"


def test_names_are_deterministic_and_use_identifiers_or_raw_values():
    values = {"material": "Brushed Steel", "size": "2x"}
    assert naming.render_name("{{material}}_{{size}}", values, prefix="set-") == \
        "set-brushed-steel_2x.png"
    assert naming.render_name("{{material.value}}", values) == "Brushed Steel.png"


def test_template_folders_are_allowed_but_values_cannot_add_one():
    assert naming.render_name("{{loc}}/{{size}}", {"loc": "fr", "size": "s"}) == "fr/s.png"
    assert naming.render_name("{{v.value}}", {"v": "../../etc/passwd"}) == "_.._etc_passwd.png"


@pytest.mark.parametrize("template", ["../{{a}}", "/{{a}}", "{{a}}/..", "./{{a}}", "{{a}}//x"])
def test_traversal_and_empty_components_are_refused(template):
    with pytest.raises(naming.NamingError, match="empty folder or file name"):
        naming.render_name(template, {"a": "x"})


def test_unsafe_characters_and_device_names_are_neutralised():
    assert naming.render_name('{{a.value}}', {"a": 'a<b>:c"d|e?f*g'}) == "a_b__c_d_e_f_g.png"
    assert naming.render_name("{{a}}", {"a": "CON"}) == "_con.png"
    assert naming.render_name("{{a.value}}.jpg", {"a": "photo"}) == "photo.png"


def test_depth_and_length_limits_are_errors():
    with pytest.raises(naming.NamingError, match="folders deep"):
        naming.render_name("a/b/c/d/{{x}}", {"x": "y"})
    with pytest.raises(naming.NamingError, match="longer than"):
        naming.render_name("{{x.value}}" * 2, {"x": "y" * 70})


def test_collisions_are_case_insensitive_and_catch_file_folder_clashes():
    assert naming.find_collisions({"k1": "Red.png", "k2": "red.png", "k3": "blue.png"}) == {
        "Red.png": ["k1", "k2"]}
    assert naming.find_collisions({"k1": "a.png", "k2": "a.png/b.png"}) == {"a.png": ["k1"]}


def test_a_template_that_drops_an_axis_collides_and_is_reported():
    axes = _axes(material=["wood", "steel"], color=["red", "green"])
    names = {c.key: naming.render_name("{{material}}", c.as_dict()) for c in ex.combinations(axes)}
    collisions = naming.find_collisions(names)
    assert collisions["wood.png"] == ["material=wood,color=red", "material=wood,color=green"]
