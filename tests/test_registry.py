"""Registry invariants: every generator entry must be renderable by the frontend
and routable on the API — catches schema drift without any GPU work."""
from __future__ import annotations

from backend.app.generators.registry import registry
from backend.app.presets import TUNING_HINTS

VALID_TYPES = {"textarea", "toggle", "number", "select", "slider", "segmented", "aspect", "lora"}


def _entries():
    return registry()


def test_ids_unique_and_kind_matches():
    entries = _entries()
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids))
    for e in entries:
        assert e["kind"] == e["id"]
        assert e["output"] in ("image", "video")
        assert isinstance(e.get("needs_image", False), bool)
        assert isinstance(e.get("needs_remote", False), bool)


def test_completed_cpu_probe_disables_only_local_generators(monkeypatch):
    from backend.app.backends import local

    monkeypatch.setattr(local, "_DEVICE", ("CPU only", 0.0))
    entries = registry()
    local_entries = [entry for entry in entries if entry.get("device") == "local"]
    remote_entries = [entry for entry in entries if entry.get("needs_remote")]
    assert local_entries and all("CUDA GPU" in entry["unavailable_reason"] for entry in local_entries)
    assert remote_entries and all("unavailable_reason" not in entry for entry in remote_entries)


def test_unknown_hardware_does_not_preemptively_disable_local_generators(monkeypatch):
    from backend.app.backends import local

    monkeypatch.setattr(local, "_DEVICE", None)
    assert all("unavailable_reason" not in entry for entry in registry())


def test_endpoints_are_routable():
    from backend.app.main import app

    # The OpenAPI schema resolves lazily-included routers to concrete paths.
    paths = set(app.openapi()["paths"].keys())
    for e in _entries():
        assert e["endpoint"].startswith("/api/")
        assert e["endpoint"] in paths, f"{e['id']} endpoint {e['endpoint']} not routed"


def test_controls_schema_valid():
    for e in _entries():
        names = [c["name"] for c in e["controls"]]
        assert len(names) == len(set(names)), f"duplicate control names in {e['id']}"
        for c in e["controls"]:
            assert c.get("name") and c.get("label") and c.get("type"), f"bad control in {e['id']}: {c}"
            assert c["type"] in VALID_TYPES
            if c["type"] in ("select", "segmented"):
                assert c.get("options"), f"{e['id']}.{c['name']} needs options"
            if c["type"] == "slider":
                assert c["min"] <= c["default"] <= c["max"], f"{e['id']}.{c['name']} default out of range"
            if "show_if" in c:
                assert c["show_if"]["field"] in names, (
                    f"{e['id']}.{c['name']} show_if references unknown field")
            if "hint_key" in c:
                assert c["hint_key"] in TUNING_HINTS, f"missing hint {c['hint_key']}"
            if "hint_keys_by" in c:
                dependent = c["hint_keys_by"]
                assert dependent["field"] in names, (
                    f"{e['id']}.{c['name']} hint_keys_by references unknown field")
                for hint_key in dependent["map"].values():
                    assert hint_key in TUNING_HINTS, f"missing dependent hint {hint_key}"
            if "overrides_by" in c:
                dependent = c["overrides_by"]
                assert dependent["field"] in names, (
                    f"{e['id']}.{c['name']} overrides_by references unknown field")
                for override in dependent["map"].values():
                    if c["type"] == "slider":
                        assert override.get("min", c["min"]) <= c["default"] <= override.get("max", c["max"])


def test_image_inputs_shape():
    """Generators that take image inputs declare them consistently."""
    for e in _entries():
        for slot in e.get("image_inputs", []):
            assert slot.get("name") and slot.get("label")
        if e.get("image_inputs"):
            assert e["image_inputs"][0]["name"] == "image"  # slot 1 back-compat


# --- capability gating: the UI must not offer what the A100 cannot do -------
def _speed_controls(spec_id: str) -> bool:
    from backend.app.generators.registry import registry

    spec = next(s for s in registry() if s["id"] == spec_id)
    return any(c["name"] == "speed_mode" for c in spec["controls"])


def test_speed_falls_back_to_configured_before_any_session_answers(monkeypatch):
    """Unknown != unsupported. Hiding a control from someone who simply has not
    started their notebook is worse than showing one the A100 will reject."""
    import backend.app.generators.registry as reg
    from backend.app import backends

    monkeypatch.setattr(reg, "remote_gpu_seen", lambda: False)
    monkeypatch.setattr(backends.REMOTE_GPU, "features", frozenset)
    monkeypatch.setattr(reg.settings, "image_lightning_lora", "lightx2v/Qwen-Image-2512-Lightning")
    monkeypatch.setattr(reg.settings, "edit_lightning_lora", "")
    assert _speed_controls("image_colab") is True
    assert _speed_controls("image_edit") is False

    monkeypatch.setattr(reg.settings, "image_lightning_lora", "")
    assert _speed_controls("image_colab") is False

    monkeypatch.setattr(reg.settings, "edit_lightning_lora",
                        "lightx2v/Qwen-Image-Edit-2511-Lightning")
    assert _speed_controls("image_edit") is True


def test_a_connected_session_without_the_lora_hides_speed(monkeypatch):
    """The actual bug: .env named a Lightning LoRA, the A100 failed to load it,
    and the UI offered ⚡ anyway — so the backend clamped to 4 steps / CFG 1.0
    and a non-distilled model returned noise with no error anywhere."""
    import backend.app.generators.registry as reg
    from backend.app import backends

    monkeypatch.setattr(reg.settings, "image_lightning_lora", "lightx2v/Qwen-Image-2512-Lightning")
    monkeypatch.setattr(reg.settings, "edit_lightning_lora",
                        "lightx2v/Qwen-Image-Edit-2511-Lightning")
    monkeypatch.setattr(reg, "remote_gpu_seen", lambda: True)
    monkeypatch.setattr(backends.REMOTE_GPU, "features", lambda: frozenset({"preview", "cancel", "edit"}))
    assert _speed_controls("image_colab") is False
    assert _speed_controls("image_edit") is False


def test_a_connected_session_reporting_the_feature_shows_speed(monkeypatch):
    import backend.app.generators.registry as reg
    from backend.app import backends

    monkeypatch.setattr(reg.settings, "image_lightning_lora", "")
    monkeypatch.setattr(reg.settings, "edit_lightning_lora", "")
    monkeypatch.setattr(reg, "remote_gpu_seen", lambda: True)
    monkeypatch.setattr(backends.REMOTE_GPU, "features", lambda: frozenset({"speed_image"}))
    # reported by the live session, so offered even though .env says otherwise
    assert _speed_controls("image_colab") is True
    assert _speed_controls("image_edit") is False

    monkeypatch.setattr(backends.REMOTE_GPU, "features", lambda: frozenset({"speed_edit"}))
    assert _speed_controls("image_colab") is False
    assert _speed_controls("image_edit") is True


def test_colab_picker_hides_an_alt_slot_missing_from_the_live_worker(monkeypatch):
    """An old worker must never map an unknown custom slot to its base model."""
    import backend.app.generators.registry as reg
    from backend.app import backends

    monkeypatch.setattr(reg.settings, "a100_image_model_alt", "vendor/alternate")
    monkeypatch.setattr(reg, "remote_gpu_seen", lambda: True)
    monkeypatch.setattr(backends.REMOTE_GPU, "features", lambda: frozenset({"preview"}))

    assert reg._remote_model_picker() == []
    guidance = reg._guid_remote_image()
    assert "defaults_by" not in guidance


def test_hunyuan_engine_control_follows_the_live_session(monkeypatch):
    import backend.app.generators.registry as reg
    from backend.app import backends

    def engine_offered() -> bool:
        spec = next(s for s in reg.registry() if s["id"] == "t2v")
        return any(c["name"] == "engine" for c in spec["controls"])

    monkeypatch.setattr(reg.settings, "hunyuan_video_model", "tencent/HunyuanVideo")
    monkeypatch.setattr(reg, "remote_gpu_seen", lambda: True)
    monkeypatch.setattr(backends.REMOTE_GPU, "features", lambda: frozenset({"preview"}))
    assert engine_offered() is False, "session did not report hunyuan"

    monkeypatch.setattr(backends.REMOTE_GPU, "features", lambda: frozenset({"hunyuan"}))
    assert engine_offered() is True


def test_local_guidance_declares_a_variant_dependent_default():
    """Guards the fix for 'quality' silently running at Turbo's old CFG 1.0.

    Asserted against the catalogue rather than a literal map: the point is that
    every offered model carries its own CFG, which stays true as models are
    added or removed from `.env`. A hardcoded pair would have to be edited for
    each one, which is the coupling the catalogue exists to remove.
    """
    from backend.app.generators import variants
    from backend.app.generators.registry import registry

    expected = variants.guidance_map()
    assert expected, "no local model is configured at all"
    for spec_id in ("image_local", "img2img", "inpaint"):
        spec = next(s for s in registry() if s["id"] == spec_id)
        g = next(c for c in spec["controls"] if c["name"] == "guidance")
        assert g["defaults_by"]["field"] == "model_variant"
        assert g["defaults_by"]["map"] == expected
        # Every offered model must be selectable, and every CFG in range.
        picker = next(c for c in spec["controls"] if c["name"] == "model_variant")
        assert set(picker["options"]) == set(expected)
        for name, cfg in expected.items():
            assert g["min"] <= cfg <= g["max"], f"{name} CFG {cfg} outside the slider"


def test_edit_generators_are_honest_about_input_geometry():
    from backend.app.generators.registry import registry

    specs = {s["id"]: s for s in registry()}
    img_aspect = next(c for c in specs["img2img"]["controls"] if c["name"] == "aspect")
    assert img_aspect["default"] == "Match input"

    inpaint = {c["name"]: c for c in specs["inpaint"]["controls"]}
    assert "aspect" not in inpaint, "inpaint returns the source canvas; it cannot change aspect"
    assert inpaint["inpaint_area"]["default"] == "masked area"
    assert {"mask_grow", "mask_padding", "mask_blur"} <= inpaint.keys()


def test_every_variant_has_a_step_tier_reaching_its_default():
    """A model whose step group is missing silently falls back to another
    model's step count — the same class of bug as the CFG one above."""
    from backend.app.generators import variants
    from backend.app.presets import QUALITY_STEPS

    for v in variants.available():
        assert v.steps_group in QUALITY_STEPS, f"{v.name}: no {v.steps_group} step tier"
        tiers = QUALITY_STEPS[v.steps_group]
        assert set(tiers) == {"Draft", "Standard", "High"}
        assert tiers["Draft"] <= tiers["Standard"] <= tiers["High"]


def test_variant_names_resolve_and_are_stable():
    """Variant names are persisted in job params and replayed on rerun, so they
    are API. Unknown and unconfigured names must fall back rather than raise."""
    from backend.app.generators import variants

    for name in variants.options():
        assert variants.resolve(name), f"{name} is offered but resolves to nothing"
    # The two that predate the catalogue must never stop resolving.
    assert variants.resolve("turbo")
    # Neither an unknown name nor None may raise on the request path.
    assert variants.resolve("no-such-model") == variants.resolve(None)


def test_lora_control_is_local_lane_only():
    """Adapters are applied to the resident local pipeline. The Colab lane manages
    its own (the Lightning distill LoRA) and has no picker, so offering one there
    would be a control that silently does nothing."""
    for e in _entries():
        has_lora = any(c["name"] == "loras" for c in e["controls"])
        if e.get("device") == "local":
            assert has_lora, f"{e['id']} is local but has no LoRA picker"
        else:
            assert not has_lora, f"{e['id']} is not local but offers a LoRA picker"


def test_no_preset_inserts_dead_booster_tokens():
    """The models this app runs (Z-Image, Qwen-Image) train on descriptive
    natural-language captions, so SD-era quality incantations are at best inert.

    Covers BOTH sources of injected text. The first version of this test checked
    only STYLE_PROFILES, so it passed while the PROMPT_PRESETS dropdown — the one
    people actually click — kept injecting "8k" and "high detail skin texture".
    That only surfaced from reading real job params."""
    from backend.app.presets import PROMPT_PRESETS, STYLE_PROFILES

    dead = ("8k", "masterpiece", "best quality", "ultra detailed", "award winning",
            "highly detailed", "octane render", "4k", "hyperrealistic")

    texts = [(i["id"], i["text"]) for cat in STYLE_PROFILES for i in cat["items"]]
    texts += [(p["id"], p["text"]) for p in PROMPT_PRESETS]

    for pid, text in texts:
        low = text.lower()
        for token in dead:
            assert token not in low, f"{pid} still inserts {token!r}"


def test_default_negatives_stay_short():
    """A 30-token negative is an SD1.5 habit that mostly spends context here —
    and at the Turbo default of CFG 0 it is ignored outright."""
    from backend.app.presets import FACE_NEGATIVE, IMAGE_NEGATIVE

    for neg in (IMAGE_NEGATIVE, FACE_NEGATIVE):
        assert len(neg.split(",")) <= 6, f"negative grew back into a laundry list: {neg}"


def test_image_defaults_do_not_silently_inject_negative_text():
    """The current image models are caption-trained; a generic defect list is
    an opt-in tool, not part of every prompt. Wan keeps its published default."""
    for entry in _entries():
        control = next((c for c in entry["controls"] if c["name"] == "auto_negative"), None)
        if control is None:
            continue
        assert control["default"] is (entry["output"] == "video")

    from backend.app.routers.images import _common_params

    # Direct API clients that omit the UI field get the same modern default.
    assert _common_params({"prompt": "a ceramic fox"}, "image_local")["negative_prompt"] == ""


def test_prompt_and_workflow_presets_are_structured_and_unique():
    from backend.app.presets import PROMPT_PRESETS, SETTING_PRESETS

    assert len(PROMPT_PRESETS) >= 12
    assert len({p["id"] for p in PROMPT_PRESETS}) == len(PROMPT_PRESETS)
    assert all(p.get("category") and p.get("label") and p.get("text") for p in PROMPT_PRESETS)

    assert len({p["id"] for p in SETTING_PRESETS}) == len(SETTING_PRESETS)
    assert all(p.get("requires") and p.get("values") and p.get("description")
               for p in SETTING_PRESETS)


def test_the_negative_field_admits_when_it_does_nothing():
    """CFG 0 is the Turbo default and the model ignores negatives there.
    Silently offering a field that has no effect is the kind of thing this pass
    exists to remove, so the hint has to say so."""
    from backend.app.generators.registry import registry
    from backend.app.presets import TUNING_HINTS

    spec = next(s for s in registry() if s["id"] == "image_local")
    neg = next(c for c in spec["controls"] if c["name"] == "negative_prompt")
    assert neg.get("hint_key") == "negative_prompt"
    assert "ignores it" in TUNING_HINTS["negative_prompt"]


def test_every_generator_kind_has_a_registered_handler():
    """register_handler() is what makes rerun work. A new generator that skips it
    looks fine until someone hits Retry and the job dies with 'unknown kind'."""
    import backend.app.routers.grid
    import backend.app.routers.images
    import backend.app.routers.videos  # noqa: F401
    from backend.app.routers.common import _HANDLERS

    for e in _entries():
        assert e["kind"] in _HANDLERS, f"{e['kind']} has no registered handler — rerun would fail"


def test_controlnet_entry_appears_only_when_enabled(monkeypatch):
    """It is gated on ENABLE_CONTROLNET because it needs an extra ~1 GB model
    download that most users will not want by default."""
    import backend.app.generators.registry as reg

    monkeypatch.setattr(reg.settings, "enable_controlnet", False)
    assert not any(e["id"] == "control_local" for e in reg.registry())

    monkeypatch.setattr(reg.settings, "enable_controlnet", True)
    spec = next((e for e in reg.registry() if e["id"] == "control_local"), None)
    assert spec is not None
    modes = next(c for c in spec["controls"] if c["name"] == "control_mode")
    assert set(modes["options"]) == {"canny", "depth", "pose", "none"}


def test_mediapipe_stays_below_the_solutions_removal():
    """mediapipe dropped the legacy `mp.solutions` API between 0.10.21 and
    0.10.30. That silently breaks TWO features: the auto-detailer's face/hand
    detection, and controlnet_aux — which imports mediapipe.solutions at package
    scope, so ControlNet depth/pose stops being importable at all.

    The floor was uncapped before, so any fresh install after 0.10.30 shipped
    lost both with no error until someone actually used them. This asserts the
    cap survives, because removing it looks harmless."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / "pyproject.toml").read_text())
    spec = " ".join(cfg["project"]["optional-dependencies"]["detailer"])
    assert "mediapipe" in spec
    assert "<0.10.30" in spec or "<=0.10.21" in spec, (
        f"mediapipe cap removed ({spec}) — detailer and controlnet_aux both break"
    )


def test_face_restoration_uses_an_architecture_in_core_spandrel():
    """Core spandrel 0.4.2 recognizes GFPGAN but not CodeFormer.

    Pointing the downloader at CodeFormer looks healthy until the first real
    click, then ``ModelLoader`` rejects the checkpoint. Keep the runtime weight
    and public copy tied to the architecture that our declared dependency can
    actually load; ``_load_restorer`` performs the corresponding runtime ID
    check after opening the checkpoint.
    """
    from pathlib import Path

    from backend.app.generators.postprocess import GFPGAN_V14, GFPGAN_V14_NAME

    assert "TencentARC/GFPGAN" in GFPGAN_V14
    assert GFPGAN_V14_NAME == "GFPGANv1.4.pth"

    root = Path(__file__).resolve().parents[1]
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    tools = (root / "frontend/src/pages/Tools.tsx").read_text(encoding="utf-8")
    assert "GFPGAN" in project and "GFPGAN" in readme and "GFPGAN" in tools


def test_control_preprocessors_refuse_to_return_an_empty_map():
    """ControlNet accepts a blank control map happily and contributes nothing,
    so the user gets an ordinary unguided generation and no reason why. Pose on
    a landscape hits this every time — OpenPose finds no person and returns pure
    black. Measured on a real photo: pose gave std 0.0."""
    import pytest
    from PIL import Image

    from backend.app.generators.control_pre import _reject_if_blank
    from backend.app.generators.postprocess import ToolUnavailable

    # _reject_if_blank directly, not through preprocess(): canny would import
    # cv2, which the lazy-import guard forbids in this suite.
    with pytest.raises(ToolUnavailable, match="no person was detected"):
        _reject_if_blank(Image.new("RGB", (256, 256), "black"), "pose")
    with pytest.raises(ToolUnavailable, match="no edges were found"):
        _reject_if_blank(Image.new("RGB", (256, 256), "black"), "canny")

    # a map with real structure passes
    img = Image.new("L", (64, 64), 0)
    for x in range(0, 64, 4):
        for y in range(64):
            img.putpixel((x, y), 255)
    _reject_if_blank(img.convert("RGB"), "canny")


def test_a_ready_made_control_map_is_passed_through_unchecked():
    """mode='none' means the user supplied their own map. Second-guessing it
    would reject legitimate sparse maps."""
    from PIL import Image

    from backend.app.generators.control_pre import preprocess

    blank = Image.new("RGB", (64, 64), "black")
    assert preprocess(blank, "none").size == (64, 64)


def test_the_last_frame_slot_follows_real_flf2v_support(monkeypatch):
    """The upload slot used to be unconditional. Wan 2.2 TI2V-5B accepts a last
    frame and discards it — it has no image_encoder, so there is nothing for the
    frame to condition. Measured on the A100: the same seed with and without a
    last frame produced pixel-identical video, mean abs diff 0.000. Offering the
    slot there means half the user's input silently goes nowhere."""
    import backend.app.generators.registry as reg
    from backend.app import backends

    def slots() -> list[str]:
        spec = next(s for s in reg.registry() if s["id"] == "i2v")
        return [i["name"] for i in spec["image_inputs"]]

    monkeypatch.setattr(reg, "remote_gpu_seen", lambda: True)

    monkeypatch.setattr(backends.REMOTE_GPU, "features", lambda: frozenset({"flf2v"}))
    assert "last_frame" in slots()

    monkeypatch.setattr(backends.REMOTE_GPU, "features", lambda: frozenset({"preview", "cancel"}))
    assert "last_frame" not in slots(), "offered FLF2V against a model that ignores it"


def test_last_frame_is_offered_before_a_session_answers(monkeypatch):
    """Unknown is not unsupported — hiding it from someone who has not started
    their notebook is worse than the A100 rejecting it with a clear message."""
    import backend.app.generators.registry as reg
    from backend.app import backends

    monkeypatch.setattr(reg, "remote_gpu_seen", lambda: False)
    monkeypatch.setattr(backends.REMOTE_GPU, "features", frozenset)
    spec = next(s for s in reg.registry() if s["id"] == "i2v")
    assert "last_frame" in [i["name"] for i in spec["image_inputs"]]


# --- sampler selection -----------------------------------------------------
def test_the_sigmas_check_is_what_gates_a_sampler():
    """Being in the flow-match family is NOT sufficient, which a GPU run proved:
    FlowMatchHeunDiscreteScheduler builds from_config without complaint and then
    raises on every generation, because its set_timesteps takes no `sigmas` and
    Z-Image's pipeline always passes one.

    Exercised with stubs rather than real scheduler classes — importing diffusers
    here would trip the lazy-import guard."""
    from backend.app.generators.schedulers import _supports_sigmas

    class Usable:
        def set_timesteps(self, num_inference_steps=None, device=None, sigmas=None, mu=None):
            ...

    class LikeHeun:
        def set_timesteps(self, num_inference_steps=None, device=None):
            ...

    assert _supports_sigmas(Usable) is True
    assert _supports_sigmas(LikeHeun) is False
    assert _supports_sigmas(object()) is False   # never raises on junk


def test_the_known_incompatible_scheduler_stays_out_of_the_offered_set():
    """Guards the specific one that cost a GPU run to discover."""
    from backend.app.generators.schedulers import SAMPLERS

    offered = {cls for cls, _ in SAMPLERS.values()}
    assert "FlowMatchHeunDiscreteScheduler" not in offered
    assert "" in offered           # "default" leaves the model's own in place
    assert len(offered) >= 3


def test_sampler_is_local_lane_only_and_defaults_to_the_model_s_own():
    from backend.app.generators.registry import registry

    for spec in registry():
        ctl = next((c for c in spec["controls"] if c["name"] == "sampler"), None)
        if spec.get("device") == "local":
            assert ctl is not None, f"{spec['id']} is local but has no sampler control"
            assert ctl["default"] == "default"
        else:
            assert ctl is None, f"{spec['id']} is remote but offers a sampler"


def test_an_unknown_sampler_is_rejected_not_ignored():
    """Silently falling back would leave the user comparing two identical images
    and concluding the sampler made no difference."""
    import pytest

    from backend.app.generators import schedulers

    with pytest.raises(ValueError, match="unknown sampler"):
        schedulers.apply(object(), "dpmpp_3m_sde")


def test_default_sampler_touches_nothing():
    from backend.app.generators import schedulers

    sentinel = object()
    assert schedulers.apply(sentinel, "default") == "default"


def test_router_normalises_a_stale_sampler_value():
    from backend.app.routers.images import _common_params

    assert _common_params({"prompt": "x", "sampler": "bogus"}, "image_local")["sampler"] == "default"
    assert _common_params({"prompt": "x", "sampler": "LCM"}, "image_local")["sampler"] == "lcm"


# --- video engines ---------------------------------------------------------
def test_engine_control_lists_only_engines_the_session_reports(monkeypatch):
    """Offering an engine whose model was never configured queues a job that
    fails on load, minutes later, after evicting the resident pipeline."""
    import backend.app.generators.registry as reg
    from backend.app import backends

    def engines():
        spec = next(s for s in reg.registry() if s["id"] == "t2v")
        ctl = next((c for c in spec["controls"] if c["name"] == "engine"), None)
        return ctl["options"] if ctl else None

    monkeypatch.setattr(reg, "remote_gpu_seen", lambda: True)

    monkeypatch.setattr(backends.REMOTE_GPU, "features", lambda: frozenset({"preview"}))
    assert engines() is None, "wan-only session should not show an engine picker"

    monkeypatch.setattr(backends.REMOTE_GPU, "features", lambda: frozenset({"ltx"}))
    assert engines() == ["wan", "ltx"]

    monkeypatch.setattr(backends.REMOTE_GPU, "features", lambda: frozenset({"hunyuan", "ltx"}))
    assert engines() == ["wan", "hunyuan", "ltx"]


def test_wan_only_rules_do_not_leak_onto_other_engines():
    """The 4k+1 frame rule, Lightning speed mode and the Chinese auto-negative
    are Wan-specific. Applying them to another engine would silently distort."""
    from backend.app.routers.videos import _video_params

    wan = _video_params({"prompt": "x", "num_frames": 30, "engine": "wan",
                         "speed_mode": True, "seed": 1}, "t2v")
    assert (wan["num_frames"] - 1) % 4 == 0

    ltx = _video_params({"prompt": "x", "num_frames": 30, "engine": "ltx",
                         "speed_mode": True, "seed": 1}, "t2v")
    assert ltx["num_frames"] == 30          # no Wan frame clamp
    assert ltx["speed_mode"] is False       # Lightning is a Wan adapter


def test_an_unknown_engine_falls_back_to_wan():
    from backend.app.routers.videos import _video_params

    assert _video_params({"prompt": "x", "engine": "sora", "seed": 1}, "t2v")["engine"] == "wan"


# ---- search aliases -------------------------------------------------------
def test_generators_carry_alternative_names():
    """Back-compat maps make old *data* work. Aliases make old *vocabulary* work —
    someone who knows the previous name can still find the thing."""
    from backend.app.generators.registry import registry

    entries = {e["id"]: e for e in registry()}
    outpaint = entries.get("outpaint")
    assert outpaint is not None
    assert "extend" in outpaint["aliases"]
    assert "uncrop" in outpaint["aliases"]


def test_names_other_tools_use_are_findable():
    from backend.app.generators.registry import registry

    entries = {e["id"]: e for e in registry()}
    assert "image to image" in entries["img2img"]["aliases"]
    assert "remix" in entries["img2img"]["aliases"]
    assert "animate" in entries["t2v"]["aliases"]


def test_every_alias_key_names_something_real():
    """Aliases for tools (upscale, detail) are defined ahead of those joining the
    registry, which is fine. A key matching neither a generator nor a job kind is
    a typo that would silently never apply."""
    from backend.app.generators.registry import SEARCH_ALIASES, registry
    from backend.app.models import JobKind

    known = {e["id"] for e in registry()} | {k.value for k in JobKind}
    unknown = set(SEARCH_ALIASES) - known
    assert not unknown, f"aliases for unknown kinds: {sorted(unknown)}"


def test_an_alias_never_collides_with_a_generator_id():
    """An alias that shadows a real id would make search ambiguous."""
    from backend.app.generators.registry import SEARCH_ALIASES, registry

    ids = {e["id"] for e in registry()}
    for kind, aliases in SEARCH_ALIASES.items():
        assert not (set(aliases) & ids), f"{kind} aliases shadow a generator id"


def test_a_generator_without_aliases_is_unaffected():
    from backend.app.generators.registry import search_aliases

    assert search_aliases("nonexistent_kind") == []


def test_every_generator_has_a_usable_simple_form():
    """Simple mode renders only tier="basic" controls, so a generator with none
    would show an empty panel above a Generate button. The frontend falls back
    to the full form in that case, but silently — this is the check that says
    the fallback should never be needed."""
    for e in _entries():
        basic = [c["name"] for c in e["controls"] if c.get("tier") == "basic"]
        assert "prompt" in basic, f"{e['id']} Simple form has no prompt field"
        # Anything a Simple user cannot see must be able to run on its default.
        for c in e["controls"]:
            if c.get("tier") != "basic":
                assert "default" in c, f"{e['id']}.{c['name']} is hidden with no default"


def test_finish_replaces_the_post_toggles_in_the_simple_form():
    """The four post_* toggles collapse into one control. They still exist for
    Full mode, but only behind finish="custom" — two visible sources of truth
    for the same steps is exactly what this change removed."""
    from backend.app.generators.registry import FINISH_PRESETS

    for e in _entries():
        by_name = {c["name"]: c for c in e["controls"]}
        if "finish" not in by_name:
            continue
        assert by_name["finish"]["options"] == FINISH_PRESETS
        for toggle in ("post_detail", "post_face", "post_upscale"):
            control = by_name[toggle]
            assert control.get("tier") != "basic"
            assert control["show_if"] == {"field": "finish", "equals": "custom"}


def test_lora_picker_is_scoped_to_the_models_that_can_merge():
    """Adapters merge into the transformer, which SVDQuant INT4 cannot accept.

    That is a property of the SELECTED model, not of the app: `resolve_backend`
    degrades to bitsandbytes for any model with no published nunchaku checkpoint,
    and adapters merge fine in that degraded state. One global answer meant
    enabling nunchaku for Z-Image-Turbo also hid the picker for Chroma, Flux and
    SDXL, none of which have a nunchaku path at all.
    """
    from backend.app.generators import registry as reg
    from backend.app.generators import variants

    saved = dict(reg._LORAS_USABLE)
    try:
        names = [v.name for v in variants.available()]
        assert len(names) > 1, "needs more than one configured model to be meaningful"

        # Every model can merge -> an unconditional picker, no show_if.
        reg._LORAS_USABLE.clear()
        reg._LORAS_USABLE.update(dict.fromkeys(names, True))
        spec = next(s for s in reg.registry() if s["id"] == "image_local")
        lora = next(c for c in spec["controls"] if c["name"] == "loras")
        assert "show_if" not in lora

        # One model cannot -> the picker is scoped to the rest, not removed.
        reg._LORAS_USABLE[names[0]] = False
        spec = next(s for s in reg.registry() if s["id"] == "image_local")
        lora = next(c for c in spec["controls"] if c["name"] == "loras")
        assert lora["show_if"] == {"field": "model_variant", "equals": names[1:]}

        # No model can -> no picker at all rather than a dead one.
        reg._LORAS_USABLE.update(dict.fromkeys(names, False))
        spec = next(s for s in reg.registry() if s["id"] == "image_local")
        assert not any(c["name"] == "loras" for c in spec["controls"])
    finally:
        reg._LORAS_USABLE.clear()
        reg._LORAS_USABLE.update(saved)


def test_model_picker_labels_name_the_configured_model():
    """The option VALUE is the variant name — persisted and replayed on rerun,
    so it is API. The LABEL is the model that name currently resolves to, which
    is configuration: "sdxl" says nothing about which checkpoint is in the slot,
    and the slot is meant to be repointed."""
    from backend.app.generators import variants
    from backend.app.generators.registry import registry
    from backend.app.modelprobe import short_name

    for spec_id, lane in (("image_local", "local"), ("image_colab", "colab")):
        spec = next((s for s in registry() if s["id"] == spec_id), None)
        if spec is None:
            continue
        picker = [c for c in spec["controls"] if c["name"] == "model_variant"]
        if not picker:
            continue
        labels = picker[0]["option_labels"]
        assert set(labels) == set(picker[0]["options"])
        for v in variants.available(lane):
            assert labels[v.name].rstrip("…") in short_name(variants.repo_of(v))
            assert len(labels[v.name]) <= 24, "label too long for a picker"


def test_model_picker_surfaces_catalogue_notes():
    from backend.app.generators import variants
    from backend.app.generators.registry import registry

    spec = next(s for s in registry() if s["id"] == "image_local")
    picker = next(c for c in spec["controls"] if c["name"] == "model_variant")
    assert picker["option_hints"] == {
        v.name: v.note for v in variants.available("local")
    }


def test_lora_picker_is_basic_only_once_a_library_exists():
    """An empty picker is noise on a fresh install; a hidden one is a primary
    control withheld the moment adapters exist."""
    from backend.app.generators import registry as reg

    spec = next(s for s in reg.registry() if s["id"] == "image_local")
    lora = [c for c in spec["controls"] if c["name"] == "loras"]
    if not lora:
        return  # backend reports no variant can merge; nothing to assert
    assert (lora[0].get("tier") == "basic") == reg._has_loras()


def test_grid_sweepability_is_emitted_from_one_backend_vocabulary():
    from backend.app.generators.registry import GRID_SWEEPABLE, registry

    seen: set[str] = set()
    for spec in registry():
        for control in spec["controls"]:
            if control.get("sweepable"):
                seen.add(control["name"])
                assert control["name"] in GRID_SWEEPABLE
            elif control["name"] in GRID_SWEEPABLE:
                raise AssertionError(f"{control['name']} lost its sweepable marker")
    assert {"seed", "steps", "guidance", "aspect", "model_variant"} <= seen
