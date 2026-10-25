"""P04: the pure resolver, over the section 4 fixture catalogue.

The context is injected: presets and resources come from tests/fixtures, the
target state is fixed, and nothing is downloaded. The registry and the job
builders are the live ones, so normalization is tested against the code that
runs.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.resources.catalogue import model_resource_id
from backend.app.studio import contracts
from backend.app.studio.resolver import (
    Context,
    ResolveError,
    ResolveRequest,
    Slot,
    resolve,
)

FIXTURES = Path(__file__).parent / "fixtures" / "studio"
PRESETS = json.loads((FIXTURES / "presets.json").read_text())
RESOURCES = {r["id"]: r for r in json.loads((FIXTURES / "resources.json").read_text())["resources"]}
TURBO = "hf:Tongyi-MAI/Z-Image-Turbo@f332072aa78be7aecdf3ee76d5c247082da564a6"
QWEN = "hf:Qwen/Qwen-Image-2512@25468b98e3276ca6700de15c6628e51b7de54a26"
INK = "local:sha256:724599363a4213947b5e04d81b350a3cc1d00de796c088111d00fa3d89ad4516"
DETAIL = "local:sha256:16af544427f8d4116611fd461474ec0710ec84d756076909611753f8ae540a92"
SDXL = "local:sha256:4ee5a2b71615b093dca8e3a9b4c3e9405ba3f01174f9fd0714c61be36d2443b4"


def _assets(tmp_path):
    from PIL import Image

    out = {}
    for asset_id, size in ((11, (512, 512)), (12, (640, 480)), (13, (512, 512)), (14, (640, 480))):
        path = tmp_path / f"{asset_id}.png"
        Image.new("RGB", size, (asset_id * 9 % 255, 80, 120)).save(path)
        out[asset_id] = SimpleNamespace(id=asset_id, kind="image", width=size[0],
                                        height=size[1], path=str(path))
    return out


def _lora_bytes(salt: int) -> bytes:
    import struct

    header = {
        "diffusion_model.layers.0.attention.to_q.lora_A.weight":
            {"dtype": "F16", "shape": [2, 2], "data_offsets": [0, 8]},
        "diffusion_model.layers.0.adaLN_modulation.0.lora_B.weight":
            {"dtype": "F16", "shape": [2, 2], "data_offsets": [8, 16]},
    }
    blob = json.dumps(header).encode()
    return struct.pack("<Q", len(blob)) + blob + bytes([salt]) + bytes(15)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from backend.app.config import settings

    library = tmp_path / "loras"
    library.mkdir()
    monkeypatch.setattr(settings, "lora_dir", str(library))
    for salt, name in enumerate(("ink_wash", "fine_detail", "sdxl_style")):
        (library / f"{name}.safetensors").write_bytes(_lora_bytes(salt))
    assets = _assets(tmp_path)
    presets = {doc["id"]: doc for key, doc in PRESETS.items() if not key.startswith("_")}

    def preset(preset_id, revision):
        doc = presets.get(preset_id)
        if doc is None:
            raise ResolveError(404, "unknown preset", code="unknown_preset", field="preset_id")
        return {"definition": doc, "revision": revision or doc.get("revision", 1),
                "definition_hash": contracts.parse_preset(doc).definition_hash()}

    def resource(rid):
        row = RESOURCES.get(rid)
        if row is None:
            return None
        return SimpleNamespace(kind=row["kind"], family=row["family"], verification="verified",
                               constraints={})

    from backend.app.finishing import describe
    from backend.app.variant_sets.operations import registry_specs

    state: dict = {"remote": {"connected": True, "features": ["edit_inpaint"]}, "local": "",
                   "cache": "ready", "hardware": {"vram_gb": 16.0}}
    context = Context(
        preset=preset, resource=resource,
        adapter_path=lambda rid: {INK: "ink_wash.safetensors",
                                  DETAIL: "fine_detail.safetensors",
                                  SDXL: "sdxl_style.safetensors"}.get(rid),
        specs=registry_specs,
        slots=lambda: [Slot("local", "image", "turbo", "Tongyi-MAI/Z-Image-Turbo"),
                       Slot("remote", "image", "quality", "Qwen/Qwen-Image-2512"),
                       Slot("remote", "edit", "edit", "Qwen/Qwen-Image-Edit-2511")],
        resource_for_repo=model_resource_id,
        asset=assets.get, asset_hash=lambda row: f"blake2b:{row.id:040x}",
        asset_file_exists=lambda row: True,
        local_unavailable=lambda: state["local"],
        local_model_status=lambda repo: state["cache"],
        remote_state=lambda: state["remote"], finishing=describe, hf_token=lambda: True,
        hardware=lambda: state["hardware"],
    )
    context.extra["state"] = state
    return context


def run(ctx, **fields):
    fields.setdefault("seed", 1234)
    return resolve(ResolveRequest(**fields), ctx)


# ---- the fixture catalogue ----------------------------------------------------------------
def test_no_lora_preset_resolves_to_an_ordinary_local_job(ctx):
    plan = run(ctx, preset_id="fixture/fast-general", prompt="a fox in snow")
    assert plan["kind"] == "image_local" and plan["target"]["resolved"] == "local"
    assert plan["readiness"]["ready"] is True and plan["adapters"] == []
    assert plan["prompt"]["positive"] == "a fox in snow"
    assert plan["effective_params"]["model_variant"] == "turbo"
    assert plan["provenance"]["quality"] == "preset"
    assert plan["provenance"]["guidance"] == "dependent_default"
    assert plan["provenance"]["sampler"] == "registry_default"
    assert plan["preset"]["id"] == "fixture/fast-general" and plan["preset"]["revision"] == 1
    assert "loras" not in plan["effective_params"] or plan["effective_params"]["loras"] == []


def test_an_adapter_stack_keeps_its_order_strengths_and_triggers(ctx):
    plan = run(ctx, preset_id="fixture/ink-detail", prompt="a heron")
    assert [a["resource"] for a in plan["adapters"]] == [INK, DETAIL]
    assert plan["effective_params"]["loras"] == [
        {"path": "ink_wash.safetensors", "weight": 0.8},
        {"path": "fine_detail.safetensors", "weight": 0.35}]
    assert plan["prompt"]["positive"] == "ink wash, a heron"
    assert plan["prompt"]["negative"] == "blurry"
    assert plan["settings"]["guidance"] == 1.5 and plan["provenance"]["guidance"] == "preset"


def test_a_character_reference_becomes_the_edit_inputs(ctx):
    plan = run(ctx, preset_id="fixture/character-scene", prompt="she reads in a cafe",
               notes="red scarf, freckles", inputs={"references": {"character": [11, 13]}})
    assert plan["kind"] == "image_edit" and plan["target"]["resolved"] == "remote"
    assert [r["asset_id"] for r in plan["inputs"]["references"]["character"]] == [11, 13]
    assert plan["prompt"]["positive"] == "she reads in a cafe. Keep this character: red scarf, freckles"
    assert all(h["hash"].startswith("blake2b:") for h in plan["inputs"]["references"]["character"])


@pytest.mark.parametrize("refs,code", [([], "missing_input"), ([11, 12, 13, 14], "invalid_request")])
def test_reference_counts_outside_the_role_are_refused(ctx, refs, code):
    with pytest.raises(ResolveError) as excinfo:
        run(ctx, preset_id="fixture/character-scene", prompt="x",
            inputs={"references": {"character": refs}})
    assert excinfo.value.field == "inputs.references.character" and excinfo.value.code == code


def test_ordered_finishing_is_preserved_into_the_job(ctx):
    plan = run(ctx, preset_id="fixture/sticker", prompt="a cactus")
    assert [s["processor"] for s in plan["finish"]] == ["background_removal", "resize"]
    assert plan["effective_params"]["finish_steps"] == plan["finish"]
    assert plan["finish"][1]["mode"] == "contain" and plan["finish"][1]["width"] == 512
    assert [v["validator"] for v in plan["validation"]] == ["dimensions", "alpha"]


def test_style_fragments_compose_in_declaration_order(ctx):
    plan = run(ctx, preset_id="fixture/storybook", prompt="a lantern festival")
    assert plan["prompt"]["style_ids"] == ["watercolor", "warm"]
    assert plan["prompt"]["positive"] == ("a storybook illustration of a lantern festival, "
                                          "soft watercolour washes, warm late-afternoon light")
    picked = run(ctx, preset_id="fixture/storybook", prompt="a lantern festival",
                 styles=["warm", "gouache"])
    assert picked["prompt"]["style_ids"] == ["gouache", "warm"]
    assert picked["provenance"]["styles"] == "override"
    assert picked["prompt"]["raw"] == "a lantern festival"


def test_enhancement_is_declared_text_viewable_and_off_by_default(ctx):
    plain = run(ctx, preset_id="fixture/storybook", prompt="a kite")
    assert plain["prompt"]["enhancement"] == {"enabled": False, "modifications": []}
    enhanced = run(ctx, preset_id="fixture/storybook", prompt="a kite", enhance=True)
    assert enhanced["prompt"]["enhancement"]["modifications"] == ["clear silhouettes",
                                                                  "readable story beat"]
    assert enhanced["prompt"]["positive"].endswith("clear silhouettes, readable story beat")
    assert enhanced["prompt"]["raw"] == "a kite"
    assert enhanced["hashes"]["creative_request_hash"] != plain["hashes"]["creative_request_hash"]


def test_an_unknown_style_names_its_position(ctx):
    with pytest.raises(ResolveError) as excinfo:
        run(ctx, preset_id="fixture/storybook", prompt="x", styles=["warm", "neon"])
    assert excinfo.value.field == "styles.1" and excinfo.value.code == "invalid_option"


def test_an_unsupported_combination_is_a_complete_plan_that_is_not_ready(ctx):
    plan = run(ctx, preset_id="fixture/sdxl-lora-on-zimage", prompt="x")
    assert plan["readiness"]["ready"] is False
    assert plan["adapters"][0]["status"] == "blocked"
    assert any(b["code"] == "incompatible_resource" and b["field"] == "adapters.0"
               for b in plan["readiness"]["blockers"])
    assert plan["hashes"]["creative_request_hash"].startswith("sha256:")


# ---- provenance and precedence ------------------------------------------------------------
def test_explicit_equal_to_default_keeps_its_override_provenance(ctx):
    untouched = run(ctx, preset_id="fixture/fast-general", prompt="x")
    default_guidance = untouched["settings"]["guidance"]
    chosen = run(ctx, preset_id="fixture/fast-general", prompt="x",
                 settings={"guidance": default_guidance})
    assert untouched["provenance"]["guidance"] == "dependent_default"
    assert chosen["provenance"]["guidance"] == "override"
    assert chosen["settings"]["guidance"] == default_guidance
    # Same value, so the same creative request: provenance is not content.
    assert chosen["hashes"]["creative_request_hash"] == untouched["hashes"]["creative_request_hash"]


def test_overrides_are_validated_strictly_and_never_clamped(ctx):
    with pytest.raises(ResolveError) as excinfo:
        run(ctx, preset_id="fixture/fast-general", prompt="x", settings={"guidance": 99})
    assert excinfo.value.field == "settings.guidance" and excinfo.value.code == "out_of_range"
    with pytest.raises(ResolveError) as excinfo:
        run(ctx, preset_id="fixture/fast-general", prompt="x", settings={"cfg": 3})
    assert excinfo.value.code == "unknown_setting" and "guidance" in excinfo.value.valid_options
    with pytest.raises(ResolveError) as excinfo:
        run(ctx, preset_id="fixture/fast-general", prompt="x", settings={"model_variant": "quality"})
    assert excinfo.value.field == "settings.model_variant"


def test_a_model_change_moves_only_model_tuned_preset_values(ctx):
    quality = model_resource_id("Tongyi-MAI/Z-Image")
    ctx.slots = lambda: [Slot("local", "image", "turbo", "Tongyi-MAI/Z-Image-Turbo"),
                         Slot("local", "image", "quality", "Tongyi-MAI/Z-Image")]
    plan = run(ctx, preset_id="fixture/ink-detail", prompt="x", model=quality, adapters=[])
    assert plan["model"]["origin"] == "override" and plan["model"]["variant"] == "quality"
    assert plan["provenance"]["guidance"] == "dependent_default"   # was the preset's 1.5
    assert plan["changed_dependencies"][0]["field"] == "guidance"
    assert plan["settings"]["quality"] == "High" and plan["provenance"]["quality"] == "preset"
    kept = run(ctx, preset_id="fixture/ink-detail", prompt="x", model=quality, adapters=[],
               settings={"guidance": 2.5})
    assert kept["settings"]["guidance"] == 2.5 and kept["provenance"]["guidance"] == "override"


def test_every_builder_normalization_is_in_the_preview(ctx):
    plan = run(ctx, preset_id="fixture/fast-general", prompt="x", settings={"steps": 20})
    steps = next(n for n in plan["normalized"] if n["field"] == "steps")
    assert steps["requested"] == 20 and steps["effective"] == plan["effective_params"]["steps"]
    assert any(w["code"] == "ignored_override" and w["field"] == "settings.steps"
               for w in plan["warnings"])
    sized = run(ctx, preset_id="fixture/fast-general", prompt="x",
                settings={"width": 777, "height": 333})
    assert {n["field"] for n in sized["normalized"]} >= {"width", "height"}
    custom = run(ctx, preset_id="fixture/fast-general", prompt="x",
                 settings={"aspect": "Custom", "width": 1000, "height": 1000})
    assert custom["effective_params"]["width"] % 16 == 0
    big = run(ctx, preset_id="fixture/fast-general", prompt="x",
              settings={"batch": 8, "quality": "Ultra", "aspect": "16:9"})
    assert big["effective_params"]["batch"] <= 8
    assert big["seed"]["items"] == [1234 + i for i in range(big["effective_params"]["batch"])]


def test_speed_mode_constraints_are_applied_and_conflicts_refused(ctx):
    plan = run(ctx, operation="text_to_image", model=QWEN, prompt="x", target="remote",
               settings={"speed_mode": True})
    assert plan["kind"] == "image_colab"
    assert plan["settings"]["guidance"] == 1.0 and plan["provenance"]["guidance"] == "runtime"
    with pytest.raises(ResolveError) as excinfo:
        run(ctx, operation="text_to_image", model=QWEN, prompt="x", target="remote",
            settings={"speed_mode": True, "guidance": 6})
    assert excinfo.value.code == "constrained" and excinfo.value.field == "settings.guidance"


# ---- targets ------------------------------------------------------------------------------
def test_remote_adapters_are_blocked_never_silently_dropped(ctx):
    ctx.slots = lambda: [Slot("remote", "image", "quality", "Tongyi-MAI/Z-Image-Turbo")]
    plan = run(ctx, preset_id="fixture/ink-detail", prompt="x", target="remote")
    assert plan["readiness"]["ready"] is False
    assert {b["code"] for b in plan["readiness"]["blockers"]} == {"adapters_unsupported_on_target"}


def test_auto_prefers_a_ready_local_target_and_never_routes_remote_unasked(ctx):
    local = run(ctx, preset_id="fixture/fast-general", prompt="x")
    assert local["target"]["resolved"] == "local" and "this machine can run it" in local["target"]["reason"]
    remote_only = run(ctx, operation="text_to_image", model=QWEN, prompt="x")
    assert remote_only["target"]["resolved"] == "remote"
    assert any(b["code"] == "remote_not_allowed" for b in remote_only["readiness"]["blockers"])
    allowed = run(ctx, operation="text_to_image", model=QWEN, prompt="x", allow_remote=True)
    assert allowed["readiness"]["ready"] is True and allowed["target"]["resolved"] == "remote"


def test_readiness_is_not_validity(ctx):
    ctx.extra["state"]["cache"] = "absent"
    plan = run(ctx, preset_id="fixture/fast-general", prompt="x")
    # Valid and submittable (the first run downloads it), but not claimed ready.
    assert plan["readiness"]["state"] == "needs_resources"
    assert plan["readiness"]["submittable"] is True and plan["readiness"]["ready"] is False
    assert any(w["code"] == "resource_not_ready" for w in plan["warnings"])
    ctx.extra["state"]["remote"] = {"connected": False}
    offline = run(ctx, preset_id="fixture/character-scene", prompt="x", allow_remote=True,
                  inputs={"references": {"character": [11]}})
    assert offline["readiness"]["ready"] is False
    assert [b["code"] for b in offline["readiness"]["blockers"]] == ["target_unavailable"]
    assert offline["kind"] == "image_edit"


def test_a_model_no_target_has_is_reported_not_substituted(ctx):
    plan = run(ctx, operation="text_to_image", prompt="x",
               model="hf:someone/unconfigured-model@0123456789abcdef")
    assert plan["readiness"]["ready"] is False and plan["effective_params"] == {}
    assert plan["readiness"]["blockers"][0]["code"] == "target_unavailable"


# ---- hashes and side effects --------------------------------------------------------------
def test_hashes_are_deterministic_and_separate(ctx):
    a = run(ctx, preset_id="fixture/storybook", prompt="a kite")
    b = run(ctx, preset_id="fixture/storybook", prompt="a kite")
    assert a["hashes"] == b["hashes"]
    reseeded = run(ctx, preset_id="fixture/storybook", prompt="a kite", seed=99)
    assert reseeded["hashes"]["creative_request_hash"] != a["hashes"]["creative_request_hash"]
    # Asking for "auto" or "local" is not a creative difference when both land on local.
    local = run(ctx, preset_id="fixture/storybook", prompt="a kite", target="local")
    assert local["hashes"]["creative_request_hash"] == a["hashes"]["creative_request_hash"]


def test_resolve_needs_a_concrete_seed(ctx):
    with pytest.raises(ResolveError) as excinfo:
        resolve(ResolveRequest(preset_id="fixture/fast-general", prompt="x"), ctx)
    assert excinfo.value.code == "seed_required"


def test_resolving_has_no_side_effects(client, ctx, monkeypatch):
    import threading

    import httpx
    from sqlmodel import select

    from backend.app import db
    from backend.app.models import Asset, Job, Recipe
    from backend.app.resources import store
    from backend.app.routers import common

    def counts():
        with db.session() as s:
            return tuple(len(list(s.exec(select(t)))) for t in (Job, Asset, Recipe))

    def forbid(*_a, **_k):
        raise AssertionError("resolve must not touch the network or the queue")

    # The app's own background catalogue refresh may write resources while
    # this runs; what must not happen is a write from the resolving thread.
    writes: list[str] = []
    here = threading.get_ident()
    for name in ("upsert", "upsert_install", "mark_installs"):
        original = getattr(store, name)

        def recorder(*args, _name=name, _original=original, **kwargs):
            if threading.get_ident() == here:
                writes.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(store, name, recorder)
    monkeypatch.setattr(httpx, "Client", forbid)
    monkeypatch.setattr(httpx, "AsyncClient", forbid)
    monkeypatch.setattr(common, "enqueue", forbid)
    before = counts()
    for case in ("fixture/fast-general", "fixture/ink-detail", "fixture/sticker"):
        run(ctx, preset_id=case, prompt="x")
    assert counts() == before and writes == []


def test_a_preset_and_an_operation_must_agree(ctx):
    with pytest.raises(ResolveError) as excinfo:
        run(ctx, preset_id="fixture/fast-general", prompt="x", operation="edit")
    assert excinfo.value.code == "operation_mismatch"
    with pytest.raises(ResolveError) as excinfo:
        run(ctx, prompt="x")
    assert excinfo.value.field == "operation"


def test_plans_serialize_without_paths(ctx):
    plan = run(ctx, preset_id="fixture/character-scene", prompt="x",
               inputs={"references": {"character": [11]}})
    text = json.dumps(plan)
    assert ".png" not in text and "image_paths" not in text
    assert copy.deepcopy(plan) == json.loads(text)


def test_an_adapter_file_gone_since_the_scan_blocks_rather_than_drops(ctx):
    from backend.app.config import settings

    (settings.loras_dir / "fine_detail.safetensors").unlink()
    plan = run(ctx, preset_id="fixture/ink-detail", prompt="x")
    assert plan["readiness"]["ready"] is False
    assert any(b["code"] == "resource_not_ready" and b["field"] == "adapters"
               for b in plan["readiness"]["blockers"])


# ---- reference sets ----------------------------------------------------------------------
def _sets(ctx, entries_by_revision):
    """A fake reference set 7 whose 'character' role changes between revisions."""
    def reference_set(set_id, revision, role):
        if set_id != 7:
            raise ResolveError(404, "no such set", code="not_found", field="reference_set")
        rev = revision or max(entries_by_revision)
        if role != "character":
            raise ResolveError(400, "no such role", code="invalid_request",
                               field=f"inputs.reference_sets.{role}")
        return {"id": 7, "name": "Mara", "revision": rev, "digest": f"sha256:{rev}",
                "notes": "red scarf, freckles", "entries": entries_by_revision[rev]}

    ctx.reference_set = reference_set
    return ctx


def test_a_reference_set_fills_a_role_in_order_and_the_plan_pins_its_revision(ctx):
    _sets(ctx, {1: [{"asset_id": 13}, {"asset_id": 11, "mask_id": 11, "note": "left profile"}],
                2: [{"asset_id": 11}]})
    plan = run(ctx, preset_id="fixture/character-scene", prompt="she reads",
               inputs={"reference_sets": {"character": {"id": 7, "revision": 1}}})
    refs = plan["inputs"]["references"]["character"]
    assert [r["asset_id"] for r in refs] == [13, 11]
    assert refs[1]["mask"]["asset_id"] == 11 and refs[1]["note"] == "left profile"
    assert plan["inputs"]["reference_sets"]["character"] == {
        "id": 7, "name": "Mara", "revision": 1, "digest": "sha256:1", "origin": "override"}
    # The set's notes fill {{notes}} for a role that asks for them.
    assert plan["prompt"]["positive"] == "she reads. Keep this character: red scarf, freckles"
    latest = run(ctx, preset_id="fixture/character-scene", prompt="she reads",
                 inputs={"reference_sets": {"character": {"id": 7}}})
    assert latest["inputs"]["reference_sets"]["character"]["revision"] == 2
    assert latest["hashes"]["material_hash"] != plan["hashes"]["material_hash"]


def test_a_set_with_more_references_than_the_role_takes_is_refused_not_truncated(ctx):
    _sets(ctx, {1: [{"asset_id": i} for i in (11, 12, 13, 14)]})
    with pytest.raises(ResolveError) as excinfo:
        run(ctx, preset_id="fixture/character-scene", prompt="x",
            inputs={"reference_sets": {"character": {"id": 7}}})
    assert excinfo.value.field == "inputs.reference_sets.character"
    assert "revision 1 has 4" in str(excinfo.value)


def test_ids_and_a_set_for_one_role_are_ambiguous(ctx):
    _sets(ctx, {1: [{"asset_id": 11}]})
    with pytest.raises(ResolveError) as excinfo:
        run(ctx, preset_id="fixture/character-scene", prompt="x",
            inputs={"references": {"character": [11]},
                    "reference_sets": {"character": {"id": 7}}})
    assert excinfo.value.code == "ambiguous_request"


def test_a_preset_can_bind_a_set_and_a_request_can_replace_it(ctx, monkeypatch):
    _sets(ctx, {1: [{"asset_id": 13}]})
    bound = copy.deepcopy(PRESETS["character_reference"])
    bound["id"] = "fixture/bound-character"
    bound["references"][0]["reference_set"] = 7
    original = ctx.preset
    ctx.preset = lambda pid, rev: ({"definition": bound, "revision": 1,
                                    "definition_hash": "x"} if pid == bound["id"]
                                   else original(pid, rev))
    plan = run(ctx, preset_id=bound["id"], prompt="x")
    assert plan["inputs"]["reference_sets"]["character"]["origin"] == "preset"
    explicit = run(ctx, preset_id=bound["id"], prompt="x",
                   inputs={"references": {"character": [12]}})
    assert [r["asset_id"] for r in explicit["inputs"]["references"]["character"]] == [12]
    assert "reference_sets" not in explicit["inputs"]


def test_a_preset_validation_policy_compiles_into_the_plan(ctx):
    sticker = copy.deepcopy(PRESETS["ordered_finishing"])
    sticker["id"] = "fixture/checked-sticker"
    sticker["validation"] = [
        {"validator": "alpha", "required": True, "options": {"alpha": "required"}},
        {"validator": "margins", "options": {"safe_margin": 8}},
    ]
    original = ctx.preset
    ctx.preset = lambda pid, rev: ({"definition": sticker, "revision": 1, "definition_hash": "x"}
                                   if pid == sticker["id"] else original(pid, rev))
    plan = run(ctx, preset_id=sticker["id"], prompt="a cat")
    assert plan["validation_spec"] == {"alpha": "required", "safe_margin": 8,
                                       "required": ["alpha"], "optional": ["margins"]}
