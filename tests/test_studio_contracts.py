"""P00 contract vocabulary: presets, resources, plans and the legacy database.

These pin names other packets build on. A failure here usually means a
persisted value was renamed or a document family started to look like
another, both of which break stored data rather than code.
"""
from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, select

from backend.app import db
from backend.app.models import Job, JobKind, Recipe, VariantItem, VariantSet
from backend.app.studio import contracts as c

FIXTURES = Path(__file__).parent / "fixtures"
PRESETS = json.loads((FIXTURES / "studio" / "presets.json").read_text())
RESOURCES = json.loads((FIXTURES / "studio" / "resources.json").read_text())["resources"]
DOCUMENTS = json.loads((FIXTURES / "studio" / "documents.json").read_text())
CASES = [k for k in PRESETS if not k.startswith("_")]


@pytest.mark.parametrize("case", CASES)
def test_every_fixture_preset_is_a_valid_v2_definition(case):
    preset = c.parse_preset(PRESETS[case])
    assert preset.schema_version == 2
    assert preset.operation in c.OPERATIONS


def test_the_fixture_catalogue_covers_the_required_cases():
    """Section 4: no LoRA, a two-adapter stack, a character reference, ordered
    finishing, a style template and an unsupported combination."""
    assert c.parse_preset(PRESETS["no_lora"]).adapters == []
    assert len(c.parse_preset(PRESETS["two_adapter_stack"]).adapters) == 2
    assert c.parse_preset(PRESETS["character_reference"]).references[0].role == "character"
    assert [s.processor for s in c.parse_preset(PRESETS["ordered_finishing"]).finish] == [
        "background_removal", "resize"]
    assert c.parse_preset(PRESETS["style_template"]).prompt.styles
    assert "unsupported_combination" in PRESETS


def test_every_referenced_resource_has_a_fixture_identity():
    known = {r["id"] for r in RESOURCES}
    for case in CASES:
        preset = c.parse_preset(PRESETS[case])
        assert preset.model.resource in known
        assert all(a.resource in known for a in preset.adapters)


def test_order_is_preserved_because_it_changes_meaning():
    raw = copy.deepcopy(PRESETS["two_adapter_stack"])
    raw["adapters"].reverse()
    preset = c.parse_preset(raw)
    assert [a.strength for a in preset.adapters] == [0.35, 0.8]
    assert preset.definition_hash() != c.parse_preset(PRESETS["two_adapter_stack"]).definition_hash()


# ---- the three document families -------------------------------------------------------
def test_classify_keeps_saved_generators_presets_and_set_recipes_apart():
    assert c.classify(DOCUMENTS["saved_generator_v1"]) == "saved_generator_v1"
    assert c.classify(PRESETS["no_lora"]) == "preset_v2"
    assert c.classify(DOCUMENTS["variant_recipe"]) == "variant_recipe"


def test_a_preset_that_also_names_a_job_kind_is_ambiguous():
    mixed = {**PRESETS["no_lora"], "kind": "image_local"}
    with pytest.raises(c.ContractError) as excinfo:
        c.classify(mixed)
    assert excinfo.value.code == "ambiguous_request" and excinfo.value.field == "kind"


# ---- refusals carry machine-readable paths ---------------------------------------------
def test_unknown_fields_are_refused_with_their_path():
    raw = {**PRESETS["no_lora"], "sampler_hack": True}
    with pytest.raises(c.ContractError) as excinfo:
        c.parse_preset(raw)
    assert excinfo.value.code == "unknown_field"
    assert excinfo.value.field == "sampler_hack"


def test_out_of_range_values_name_the_exact_element():
    raw = copy.deepcopy(PRESETS["two_adapter_stack"])
    raw["adapters"][1]["strength"] = 9
    with pytest.raises(c.ContractError) as excinfo:
        c.parse_preset(raw)
    assert excinfo.value.field == "adapters.1.strength"


def test_an_unsupported_schema_version_is_refused_not_guessed():
    with pytest.raises(c.ContractError) as excinfo:
        c.parse_preset({**PRESETS["no_lora"], "schema_version": 3})
    assert excinfo.value.code == "unsupported_schema"


@pytest.mark.parametrize("bad", [
    "not-a-resource", "hf:../../etc/passwd", "http://example.com/model", "",
])
def test_invalid_resource_identifiers_are_refused(bad):
    raw = copy.deepcopy(PRESETS["no_lora"])
    raw["model"]["resource"] = bad
    with pytest.raises(c.ContractError) as excinfo:
        c.parse_preset(raw)
    assert excinfo.value.field == "model.resource"


@pytest.mark.parametrize("template", [
    "{{prompt | upper}}",            # a filter: refused, never evaluated
    "{{ __import__('os') }}",        # code: refused
    "{{subject}}",                   # unknown placeholder
    "{{prompt.value}}",              # Variant Set-only form
    "{{prompt",                      # malformed
])
def test_templates_use_only_the_fixed_placeholder_grammar(template):
    raw = copy.deepcopy(PRESETS["no_lora"])
    raw["prompt"]["template"] = template
    with pytest.raises(c.ContractError) as excinfo:
        c.parse_preset(raw)
    assert excinfo.value.code == "invalid_template"


def test_single_braces_stay_literal_so_prompt_choices_keep_working():
    raw = copy.deepcopy(PRESETS["no_lora"])
    raw["prompt"]["template"] = "{{prompt}}, {red|blue} {% not jinja %}"
    assert c.parse_preset(raw).prompt.template.endswith("{% not jinja %}")


def test_a_curated_preset_needs_its_evidence():
    raw = {**PRESETS["no_lora"], "origin": "curated"}
    with pytest.raises(c.ContractError) as excinfo:
        c.parse_preset(raw)
    assert excinfo.value.code == "curation_incomplete"
    raw["curation"] = {
        "evidence": [{"kind": "evaluation", "ref": "eval-pack/fast-general-r1"}],
        "tested_targets": [{"target": "local", "hardware": "RTX 4070 Ti SUPER 16 GB"}],
        "limitations": ["text longer than a few words is unreliable"],
    }
    assert c.parse_preset(raw).curation.status == "unreviewed"


def test_settings_hold_scalars_and_structured_fields_have_their_own_place():
    raw = copy.deepcopy(PRESETS["no_lora"])
    raw["settings"]["loras"] = [{"path": "x.safetensors"}]
    with pytest.raises(c.ContractError) as excinfo:
        c.parse_preset(raw)
    assert excinfo.value.field == "settings.loras"


# ---- persisted identifiers -------------------------------------------------------------
def test_no_persisted_job_kind_is_renamed_or_removed():
    assert {k.value for k in JobKind} == set(c.PERSISTED_KINDS)


def test_every_logical_operation_maps_to_an_existing_kind():
    assert set(c.KIND_FOR.values()) <= c.PERSISTED_KINDS
    assert {op for op, _ in c.KIND_FOR} == set(c.OPERATIONS)


def test_resource_ids_are_provider_plus_exact_version():
    assert c.resource_id("hf", "Tongyi-MAI/Z-Image-Turbo", "abc") == "hf:Tongyi-MAI/Z-Image-Turbo@abc"
    assert c.resource_id("hf", "org/custom").endswith("@unpinned")
    assert c.resource_id("local", "f" * 64) == f"local:sha256:{'f' * 64}"
    with pytest.raises(c.ContractError):
        c.resource_id("ftp", "x")


# ---- hashing ---------------------------------------------------------------------------
def test_canonical_hashing_ignores_spelling_but_not_meaning():
    assert c.digest({"g": 4, "a": [1, 2]}, domain="t") == c.digest({"a": [1, 2], "g": 4.0}, domain="t")
    assert c.digest({"a": [1, 2]}, domain="t") != c.digest({"a": [2, 1]}, domain="t")
    assert c.digest({"a": 1}, domain="creative") != c.digest({"a": 1}, domain="execution")
    with pytest.raises(c.ContractError):
        c.digest({"a": float("nan")}, domain="t")


# ---- the legacy database ---------------------------------------------------------------
def _legacy_engine(tmp_path: Path):
    path = tmp_path / "gen.db"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "legacy" / "wizards-brush-0.1.8.sql").read_text())
    conn.close()
    return create_engine(f"sqlite:///{path}")


def test_the_legacy_database_upgrades_and_keeps_every_record(tmp_path):
    """Migrations must preserve historical jobs, assets, recipes and sets. This
    runs every migration this build knows over a real 0.1.8 database."""
    engine = _legacy_engine(tmp_path)
    db._migrate(engine=engine)
    db._migrate(engine=engine)  # idempotent
    with Session(engine) as s:
        jobs = {j.id: j for j in s.exec(select(Job))}
        recipes = {r.slug: r for r in s.exec(select(Recipe))}
        sets = list(s.exec(select(VariantSet)))
        items = list(s.exec(select(VariantItem)))
    assert set(jobs) == set(range(1, 8))
    assert jobs[1].params["finish_steps"][1]["mode"] == "contain"
    assert jobs[1].params["loras"] == [{"path": "ink_wash.safetensors", "weight": 0.8}]
    assert jobs[2].status == "queued" and jobs[2].client == "mcp"
    # A pre-schema row reads through the lazy parameter migration, unchanged.
    assert jobs[6].params["post_upscale"] is True and jobs[6].params["_v"] == 1
    assert recipes["ceramic-cup"].kind == "image_local"
    assert recipes["ceramic-cup"].defaulted == ["guidance", "seed"]
    assert recipes["jacket-recolour"].inputs["images"] == [1]
    assert len(sets) == 1 and sorted(i.state for i in items) == ["pending", "succeeded"]


def test_the_legacy_database_is_the_0_1_8_schema():
    """Guard against the fixture being regenerated with a newer schema."""
    text = (FIXTURES / "legacy" / "wizards-brush-0.1.8.sql").read_text()
    applied = sorted({line.split("'")[1] for line in text.splitlines()
                      if line.startswith('INSERT INTO "applied_migrations"')})
    assert applied[-1] == "0011_recipe_defaulted" and len(applied) == 11
