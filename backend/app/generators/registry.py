"""Generator registry — the single source of truth the frontend reads to render
controls dynamically. Each entry declares its endpoint, inputs, and a control
schema (sliders/selects/etc.) so adding a generator never requires UI edits.

Control schema fields the frontend understands:
  name, label, type, default, min/max/step, options, preset_group, hint_key,
  hint_keys_by ({field, map}) for model-dependent help,
  section ("Advanced" → collapsible), show_if ({field, equals}) for conditional
  visibility, and styles=True on the prompt to attach the style-chip picker.

  tier="basic" marks a control as part of the Simple form — the small set a
  generation actually needs a decision about. Everything else is only rendered
  once the user asks for the full form; its default still ships with the
  request, so hiding a control never changes what the backend receives, only
  what it has to be asked. Deciding this here rather than in React keeps the
  registry the single source of truth for the UI.
"""
from __future__ import annotations

from .. import backends
from ..config import settings
from ..modelprobe import short_name
from ..presets import ASPECT_OPTIONS, CAMERA_OPTIONS, QUALITY_TIERS
from ..remote_gpu_client import remote_gpu_seen
from . import variants
from .schedulers import OPTIONS as SAMPLER_OPTIONS

# How expensive it is to change a parameter's value, as a relative weight.
#
# 10 is a full model reload; 0 is free; negatives are cheaper than free because
# changing them does not even invalidate a cached conditioning. The scale comes
# from SwarmUI, which uses the same numbers for the same purpose.
#
# The grid generator sorts its axes by this, descending, so the expensive axis
# becomes the OUTER loop. On a 4x4 grid with model_variant on an axis that is the
# difference between 4 model loads and 16.
CHANGE_WEIGHT: dict[str, int] = {
    "model_variant": 10,   # drops the resident pipeline; 60-120s
    "speed_mode": 8,       # swaps the Lightning LoRA in or out
    "loras": 6,            # adapters are re-applied against shared components
    "quality": 1,          # step count only
    "aspect": 1,           # changes latent shape, may re-trigger allocation
    "steps": 0,
    "strength": 0,
    "guidance": -3,        # a scalar in the sampling loop
    "prompt_sr": -5,       # re-encode the prompt, nothing more
    "seed": -5,
}

# The one axis vocabulary for backend validation and the generic frontend.
# `prompt_sr` is virtual (it has no control row); `seed` is included even though
# the editor adds it first as a convenience. LoRAs are deliberately absent: a
# grid axis is scalar, while that control is a structured stack.
GRID_SWEEPABLE = frozenset({
    "steps", "guidance", "strength", "quality", "aspect",
    "model_variant", "speed_mode", "seed",
})


# Names a generator used to be known by, or is commonly called elsewhere.
#
# Distinct from the back-compat maps we already keep. `GEN_TO_SPEC` and
# `PARAM_REMAPS` make old *data* work — a stored asset or a saved job still
# resolves. These make old *vocabulary* work: someone who knows the previous name,
# or the name another tool uses, can still find the thing. Both problems are real
# and neither solves the other.
SEARCH_ALIASES: dict[str, tuple[str, ...]] = {
    "image_local": ("txt2img", "text to image", "local", "generate"),
    "image_colab": ("txt2img", "a100", "remote", "cloud"),
    "img2img": ("image to image", "variation", "remix"),
    "inpaint": ("mask", "fill", "remove object", "replace"),
    "outpaint": ("extend", "expand", "uncrop", "zoom out"),
    "image_edit": ("edit", "instruct", "modify"),
    "control_local": ("controlnet", "canny", "depth", "pose", "scribble"),
    "upscale": ("enlarge", "hires", "super resolution", "esrgan"),
    "face_restore": ("gfpgan", "codeformer", "fix faces"),
    "detail": ("adetailer", "after detailer", "refine faces"),
    "interpolate": ("rife", "smooth", "frame interpolation", "slow motion"),
    "t2v": ("text to video", "animate"),
    "i2v": ("image to video", "animate image"),
    "long_video": ("extend video", "stitch"),
}


def search_aliases(kind: str) -> list[str]:
    """Alternative names a generator can be found by. Never fails."""
    return list(SEARCH_ALIASES.get(kind, ()))


def change_weight(param: str) -> int:
    """Relative cost of changing `param`. Unknown params are treated as free,
    which keeps them where they are rather than promoting them speculatively."""
    return CHANGE_WEIGHT.get(param, 0)


# Reusable control fragments -------------------------------------------------
_PROMPT = {"name": "prompt", "label": "Prompt", "type": "textarea", "default": "", "styles": True,
           "tier": "basic"}
_NEGATIVE = {"name": "negative_prompt", "label": "Negative prompt", "type": "textarea",
             "default": "", "preset_group": "negative", "hint_key": "negative_prompt"}
_PROMPT_SYNTAX = {
    "name": "prompt_syntax", "label": "Pasted prompt emphasis", "type": "select",
    "default": "a1111", "options": ["a1111", "literal"],
    "option_labels": {"a1111": "A1111-compatible", "literal": "Keep punctuation literal"},
    "section": "Advanced", "hint_key": "prompt_syntax",
}
# Current caption-trained image models work best from a direct positive
# description; silently adding a generic SD-era defect list is no longer a
# sensible baseline. Wan is the exception: its published negative is useful and
# stays enabled for video. Both remain user-selectable.
_AUTO_NEG_IMAGE = {"name": "auto_negative", "label": "Auto negative when blank", "type": "toggle",
                   "default": False}
_AUTO_NEG_VIDEO = {**_AUTO_NEG_IMAGE, "default": True}
_SEED = {"name": "seed", "label": "Seed (-1 random)", "type": "number", "default": -1, "hint_key": "seed"}
# Local lane only: the Remote GPU worker keeps the scheduler its model ships with.
_SAMPLER = {"name": "sampler", "label": "Sampler", "type": "select", "default": "default",
            "options": SAMPLER_OPTIONS, "section": "Advanced", "hint_key": "sampler"}
_SEED_MODE = {"name": "seed_mode", "label": "Batch seed mode", "type": "select", "default": "increment",
              "options": ["increment", "fixed", "random"], "section": "Advanced", "hint_key": "seed_mode"}
_COMBINATORIAL = {"name": "combinatorial", "label": "All {a|b|c} combos as separate jobs",
                  "type": "toggle", "default": False, "section": "Advanced", "hint_key": "combinatorial"}
# Stays in the Simple form despite being a "power" control: swapping variants
# drops the resident pipeline and costs a 1-2 minute reload, so it is the one
# expensive decision the app must not make silently on the user's behalf.
#
# Built from the catalogue rather than listed here, so a model configured in
# `.env` appears without a registry edit and an unconfigured one cannot be
# offered. Segmented up to four; beyond that the buttons get too narrow to read
# and it becomes a dropdown.
# A picker option's VALUE is the variant name (persisted, replayed on rerun);
# its LABEL is the model it currently resolves to. "sdxl" says nothing about
# which checkpoint is in that slot, and the slot is configurable, so the label
# has to be derived rather than written down.
_LABEL_MAX = 24


def _option_label(v) -> str:
    name = short_name(variants.repo_of(v))
    if not name:
        return v.name
    return name if len(name) <= _LABEL_MAX else name[: _LABEL_MAX - 1] + "…"


def _model_variant(lane: str = "local", configured=None) -> dict:
    configured = variants.available(lane) if configured is None else configured
    names = [v.name for v in configured]
    return {"name": "model_variant", "label": "Model",
            "type": "segmented" if len(names) <= 4 else "select",
            "default": variants.default(lane).name, "options": names,
            "option_labels": {v.name: _option_label(v) for v in configured},
            "option_hints": {v.name: v.note for v in configured},
            "hint_key": f"model_variant_{lane}" if lane != "local" else "model_variant",
            "tier": "basic"}
_SPEED = {"name": "speed_mode", "label": "⚡ Lightning (4-step)", "type": "toggle", "default": False,
          "hint_key": "speed_mode", "tier": "basic"}
_BATCH = {"name": "batch", "label": "Batch count", "type": "slider", "default": 1,
          "min": 1, "max": 8, "step": 1, "tier": "basic"}
# Local-lane only: adapters are applied to the resident pipeline, and the Remote GPU
# side manages its own (the Lightning distill LoRA) with no picker.
def _has_loras() -> bool:
    """Whether any adapter is actually installed. Cheap: a directory listing."""
    try:
        d = settings.loras_dir
        return d.exists() and any(
            f.is_file() and f.suffix.lower() == ".safetensors"
            for f in d.rglob("*"))
    except Exception:  # noqa: BLE001 — a visibility probe must never raise
        return False


# `show_if` rather than omission: with several local models, whether adapters
# apply is a property of the SELECTED model, not of the app. The list is filled
# in per request by `_loras()` because it depends on the startup probe.
_LORAS = {"name": "loras", "label": "LoRAs", "type": "lora", "default": [],
          "hint_key": "loras"}


def _loras() -> dict:
    # Basic tier once there is actually a library to pick from. Hiding it by
    # default is right on a fresh install (an empty picker is noise) and wrong
    # the moment adapters exist, because then it is a primary creative control
    # that Simple mode was silently withholding.
    base = {**_LORAS, "tier": "basic"} if _has_loras() else _LORAS
    usable = _lora_variants()
    # Only constrain when it would actually hide something; an unconditional
    # show_if would wrongly hide the picker on generators with no model picker.
    if len(usable) == len(variants.available("local")):
        return base
    return {**base, "show_if": {"field": "model_variant", "equals": usable}}
_ASPECT = {"name": "aspect", "label": "Aspect ratio", "type": "aspect", "default": "1:1",
           "options": ASPECT_OPTIONS, "hint_key": "aspect", "tier": "basic"}
_INPUT_ASPECT = {**_ASPECT, "default": "Match input",
                 "options": ["Match input", *ASPECT_OPTIONS], "hint_key": "aspect_input"}
_WIDTH = {"name": "width", "label": "Width", "type": "number", "default": 1024, "section": "Advanced",
          "show_if": {"field": "aspect", "equals": "Custom"}}
_HEIGHT = {"name": "height", "label": "Height", "type": "number", "default": 1024, "section": "Advanced",
           "show_if": {"field": "aspect", "equals": "Custom"}}

# Video-only sizing (Wan has specific supported sizes; keep the discrete table).
_ORIENT = {"name": "orientation", "label": "Orientation", "type": "select", "default": "portrait",
           "options": ["portrait", "landscape", "square"], "tier": "basic"}
_RES = {"name": "resolution", "label": "Resolution", "type": "select", "default": "720p",
        "options": ["720p", "480p"], "hint_key": "resolution", "tier": "basic"}
_FRAMES = {
    "name": "num_frames", "label": "Length (frames · 4n+1)", "type": "slider", "default": 49,
    "min": 25, "max": 121, "step": 4, "hint_key": "num_frames", "tier": "basic",
    "overrides_by": {
        "field": "engine",
        "map": {
            "wan": {"label": "Length (frames · 4n+1)", "min": 25, "max": 121, "step": 4},
            "hunyuan": {"label": "Length (frames)", "min": 9, "max": 129, "step": 1},
            "ltx": {"label": "Length (frames)", "min": 9, "max": 129, "step": 1},
        },
    },
}
_SHOTS = {"name": "shots", "label": "Shots (×~per-shot length)", "type": "slider", "default": 3,
          "min": 2, "max": 6, "step": 1, "tier": "basic"}
_CAMERA = {"name": "camera", "label": "Camera move", "type": "select", "default": "none",
           "options": CAMERA_OPTIONS, "hint_key": "camera", "tier": "basic"}
_FPS = {"name": "fps", "label": "FPS", "type": "slider", "default": 20, "min": 8, "max": 30, "step": 1,
        "hint_key": "fps"}
_DIRECTION = {"name": "direction", "label": "Extend", "type": "segmented", "default": "all",
              "options": ["all", "wider", "taller", "left", "right", "up", "down"],
              "hint_key": "direction", "tier": "basic"}
_EXPAND = {"name": "expand_pct", "label": "Amount (% per edge)", "type": "slider", "default": 25,
           "min": 5, "max": 100, "step": 5, "hint_key": "expand_pct", "tier": "basic"}
# Outpainting wants a lower default than img2img: too high and the model stops
# respecting the primed canvas and invents its own exposure, which is what makes
# a seam. Exposed so a deliberate bigger departure is still possible.
_OUT_STRENGTH = {"name": "strength", "label": "Denoise strength", "type": "slider",
                 "default": 0.65, "min": 0.3, "max": 1.0, "step": 0.05,
                 "section": "Advanced", "hint_key": "strength_outpaint"}
# Basic on purpose: "how far from the input" is the whole question img2img and
# inpaint exist to answer, so it is not a tuning knob that can be defaulted away.
_STRENGTH = {"name": "strength", "label": "How much to change", "type": "slider", "default": 0.6,
             "min": 0.1, "max": 1.0, "step": 0.05, "hint_key": "strength", "tier": "basic"}
_INPAINT_AREA = {"name": "inpaint_area", "label": "Process", "type": "segmented",
                 "default": "masked area", "options": ["masked area", "whole image"],
                 "hint_key": "inpaint_area", "tier": "basic"}
_REGION_ONLY = {"field": "inpaint_area", "equals": "masked area"}
_MASK_GROW = {"name": "mask_grow", "label": "Grow mask (px)", "type": "slider",
              "default": 4, "min": 0, "max": 128, "step": 1,
              "section": "Advanced", "show_if": _REGION_ONLY, "hint_key": "mask_grow"}
_MASK_PADDING = {"name": "mask_padding", "label": "Context padding (px)", "type": "slider",
                 "default": 64, "min": 0, "max": 512, "step": 16,
                 "section": "Advanced", "show_if": _REGION_ONLY,
                 "hint_key": "mask_padding"}
_MASK_BLUR = {"name": "mask_blur", "label": "Blend edge (px)", "type": "slider",
              "default": 4, "min": 0, "max": 64, "step": 1,
              "section": "Advanced", "hint_key": "mask_blur"}

# Post-processing attached to generation jobs (auto-run after the result).
#
# One question — "how should this be finished?" — instead of four independent
# toggles that only ever appeared in a handful of meaningful combinations. The
# individual flags survive underneath it: `resolve_finish` in routers/common.py
# expands a preset into them, "custom" falls through to whatever they are set to,
# and a job saved before this control existed has no `finish` key at all and so
# replays from its own flags exactly as it originally ran.
FINISH_PRESETS = ["none", "faces", "upscale", "faces + upscale", "custom"]
_FINISH = {"name": "finish", "label": "Finishing", "type": "select", "default": "none",
           "options": FINISH_PRESETS, "hint_key": "finish", "tier": "basic"}
_CUSTOM_FINISH = {"field": "finish", "equals": "custom"}
_POST = [
    _FINISH,
    {"name": "post_detail", "label": "Auto-detail faces", "type": "toggle", "default": False,
     "hint_key": "post_detail", "section": "Advanced", "show_if": _CUSTOM_FINISH},
    {"name": "detail_prompt", "label": "Detail prompt (optional)", "type": "textarea", "default": "",
     "section": "Advanced", "show_if": {"field": "post_detail", "equals": True}},
    {"name": "post_face", "label": "Restore faces (GFPGAN)", "type": "toggle", "default": False,
     "section": "Advanced", "show_if": _CUSTOM_FINISH},
    {"name": "post_upscale", "label": "Upscale (Real-ESRGAN)", "type": "toggle", "default": False,
     "section": "Advanced", "show_if": _CUSTOM_FINISH},
    {"name": "post_scale", "label": "Upscale factor", "type": "select", "default": "4",
     "options": ["2", "4"], "section": "Advanced", "hint_key": "post_scale",
     "show_if": {"field": "post_upscale", "equals": True}},
]
_POST_VIDEO = [
    {"name": "post_interpolate", "label": "Smooth motion (interpolate ×2)", "type": "toggle",
     "default": False, "tier": "basic"},
]


def _quality(default: str = "Standard") -> dict:
    return {"name": "quality", "label": "Quality", "type": "segmented", "default": default,
            "options": QUALITY_TIERS, "hint_key": "quality", "tier": "basic"}


def _guidance(group: str, default: float, mx: float, step: float,
              defaults_by: dict | None = None) -> dict:
    c = {"name": "guidance", "label": "Guidance", "type": "slider", "default": default,
         "min": 0.0, "max": mx, "step": step, "hint_key": f"guidance_{group}"}
    if defaults_by:
        c["defaults_by"] = defaults_by
    return c


def _steps(group: str, default: int, mx: int) -> dict:
    # Only shown when quality == Custom; otherwise the tier sets the step count.
    return {"name": "steps", "label": "Steps", "type": "slider", "default": default, "min": 1,
            "max": mx, "step": 1, "section": "Advanced", "hint_key": f"steps_{group}",
            "show_if": {"field": "quality", "equals": "Custom"}}


# Answered once, off the request path. `None` means "not asked yet".
#
# The probe behind this is not cheap: with LOCAL_QUANT=nunchaku it imports torch
# and can run a subprocess preflight with a ten-minute timeout. `registry()` is
# built by GET /api/models, which is an async route — running it there would
# import the heavy stack into the request path (against the rule the whole
# codebase follows) and block the event loop, jobs WebSocket included, on the
# first page load. So it is memoized here and warmed at startup by
# `probe_lora_support()`, exactly as the device probe is.
_LORAS_USABLE: dict[str, bool] = {}


def probe_lora_support() -> dict[str, bool]:
    """Resolve and cache, per variant, whether merged LoRAs work here.

    Per variant rather than once for the machine: `resolve_backend` degrades to
    bitsandbytes for any model with no published SVDQuant checkpoint, and in that
    degraded state adapters merge fine. With LOCAL_QUANT=nunchaku and one answer for
    the whole app, turning nunchaku on for Z-Image-Turbo hid the picker for
    Chroma, Flux and SDXL as well — models that can all use adapters.

    Called from the startup thread, where paying for a torch import is already
    the plan. Only a model that actually has a nunchaku checkpoint does real
    work; the rest short-circuit inside `available()`.
    """
    for v in variants.available():
        if v.name in _LORAS_USABLE:
            continue
        try:
            from .quant import supports_merged_loras

            _LORAS_USABLE[v.name] = supports_merged_loras(variants.repo_of(v))
        except Exception:  # noqa: BLE001 — never let a capability probe hide the UI
            _LORAS_USABLE[v.name] = True
    return _LORAS_USABLE


def _lora_variants() -> list[str]:
    """Variants whose selected model can actually take a merged adapter.

    Adapters are merged into the transformer, which the INT4 SVDQuant backend
    cannot accept. Showing a picker whose selections silently cannot apply is
    worse than not showing it — the user gets no output difference and no error.

    Optimistic until the startup probe has answered: offering a picker that turns
    out to be unusable is a smaller failure than hiding one that would have
    worked, and it is the only answer available without blocking.
    """
    return [v.name for v in variants.available() if _LORAS_USABLE.get(v.name, True)]


def _local_loras_usable() -> bool:
    """Whether to offer the picker at all — true if ANY variant can merge."""
    return bool(_lora_variants())


def _img_controls(
    group: str, *, guidance: dict, steps: dict, extra: list | None = None,
    aspect: dict | None = _ASPECT,
) -> list:
    main: list[dict] = [
        _PROMPT, _NEGATIVE, _AUTO_NEG_IMAGE, _quality(),
        *([aspect] if aspect else []), guidance, _SEED, _BATCH,
    ]
    if group == "local" and _local_loras_usable():
        main.append(_loras())
    advanced: list[dict] = [_PROMPT_SYNTAX, steps, *([_WIDTH, _HEIGHT] if aspect else []),
                            _SEED_MODE, _COMBINATORIAL]
    if group == "local":
        advanced.insert(0, _SAMPLER)
    if any(c.get("name") == "speed_mode" for c in (extra or [])):
        reason = "Lightning fixes this value to its distilled 4-step recipe."
        forced: dict[str, object] = {
            "quality": "Custom", "negative_prompt": "", "auto_negative": False,
            "guidance": 1.0, "steps": 4,
        }
        main = [
            ({**c, "constrained_by": {"field": "speed_mode", "equals": True,
                                      "value": forced[c["name"]], "reason": reason}}
             if c.get("name") in forced else c)
            for c in main
        ]
        advanced = [
            ({**c, "constrained_by": {"field": "speed_mode", "equals": True,
                                      "value": forced[c["name"]], "reason": reason}}
             if c.get("name") in forced else c)
            for c in advanced
        ]
    return [*main, *(extra or []), *_POST, *advanced]


# The local lane serves models with incompatible CFG regimes: Z-Image-Turbo is
# distilled for CFG 0, while Z-Image base, Chroma (~3.0) and
# FLUX.1-dev (~3.5) are real-CFG models. One `default` cannot express that, and
# the server-side fallback never fired because the slider always ships a value —
# so picking Quality quietly ran a CFG model at Turbo's old CFG 1.0 default.
# `defaults_by` moves the
# slider with the variant, visibly, while leaving a hand-set value alone. The map
# comes from the catalogue so a new model cannot be added without its CFG.
_GUID_LOCAL = _guidance("local", 0.0, 8.0, 0.1, defaults_by={
    "field": "model_variant",
    "map": variants.guidance_map(),
})
_GUID_LOCAL["hint_keys_by"] = {
    "field": "model_variant",
    "map": {
        "turbo": "guidance_turbo",
        "quality": "guidance_zimage",
        "klein": "guidance_klein",
        "sdxl": "guidance_sdxl",
        "chroma": "guidance_chroma",
        "flux": "guidance_flux",
    },
}
# ControlNet is pinned to the default Z-Image-Turbo pipeline and has no model
# picker, so its guidance help must not depend on a field it does not render.
_GUID_CONTROL = _guidance("local", 0.0, 8.0, 0.1)
_GUID_CONTROL["hint_key"] = "guidance_turbo"
_GUID_A100 = _guidance("a100", 4.0, 12.0, 0.5)


def _guid_remote_image() -> dict:
    """Guidance for the Remote GPU txt2img lane, following the selected checkpoint.

    The lane serves two models with different CFG regimes (Qwen-Image ~4.0,
    FLUX.2-klein ~3.5), so the slider tracks the picker exactly as it does
    locally. The ceiling is the tightest any configured model wants: an SDXL
    checkpoint burns out above ~5 and its authors specify 2.5-4.5, so a slider
    running to 12 is mostly a way to ruin an image.

    Only this lane — the edit generator is a separate (Qwen) model with no picker.
    """
    from ..modelprobe import family_of_model

    configured = _available_remote_variants()
    families = {family_of_model(variants.repo_of(v)) for v in configured}
    group, top = ("a100_sdxl", 8.0) if "sdxl" in families else ("a100", 12.0)
    control = _guidance(group, variants.default("colab").guidance, top, 0.5)
    if len(configured) > 1:
        control["defaults_by"] = {"field": "model_variant",
                                  "map": {v.name: v.guidance for v in configured}}
        control["hint_keys_by"] = {
            "field": "model_variant",
            "map": {
                v.name: ("guidance_hidream" if v.name == "hidream" else control["hint_key"])
                for v in configured
            },
        }
    return control
_GUID_VIDEO = _guidance("video", 5.0, 12.0, 0.5)
# 50, not 30: Chroma and Flux want 40 at their High tier, so a Custom step
# count had a lower ceiling than the tier it was meant to override.
_STEPS_LOCAL = _steps("local", 9, 50)
_STEPS_A100 = _steps("a100", 30, 60)
_STEPS_VIDEO = _steps("video", 40, 80)


_REMOTE_FEATURE_FOR_IMAGE_VARIANT = {
    "hidream": "image_hidream",
    "alt": "image_alt",
}


def _available_remote_variants():
    """Configured A100 variants that the current Remote GPU worker can really serve.

    The primary model is always present. Each optional slot is included only
    when the connected worker advertises the matching feature; an older worker
    must not map an unknown name to its primary model.
    """
    configured = variants.available("colab")
    if not remote_gpu_seen():
        return configured
    return [
        v for v in configured
        if (feature := _REMOTE_FEATURE_FOR_IMAGE_VARIANT.get(v.name)) is None
        or _remote_has(feature, True)
    ]


def _remote_has(feature: str, configured: bool) -> bool:
    """Should a Remote-GPU-dependent control be offered?

    Gated on what the live session reported via /health, not on the local .env.
    Those two can disagree — the worker may be an older build, or a Lightning
    LoRA may have failed to load — and when they did, the UI happily offered
    speed mode while the backend clamped to 4 steps and CFG 1.0, so a
    non-distilled model ran 4 steps and returned noise with no error anywhere.

    Until a session answers, fall back to what is configured: hiding controls
    from a user who simply hasn't started their worker yet is worse than
    showing one the A100 will reject with a clear message.

    Now expressed through the backend registry, so a second remote answers the
    same question the same way. `remote_gpu_seen()` is still consulted because it
    distinguishes "reported an empty feature set" from "never answered", which a
    frozenset alone cannot.
    """
    if not remote_gpu_seen():
        return configured
    return backends.has_feature(feature, backend_id="remote_gpu", configured=configured)


def _video_extras() -> list[dict]:
    """Optional video controls, gated on what the connected A100 can actually do."""
    extras: list[dict] = []
    # Engines are listed individually: a session may have one, both or neither,
    # and offering an engine whose model was never configured would queue a job
    # that fails on load.
    engines = ["wan"]
    if _remote_has("hunyuan", bool(settings.hunyuan_video_model)):
        engines.append("hunyuan")
    if _remote_has("ltx", bool(settings.ltx_video_model)):
        engines.append("ltx")
    if len(engines) > 1:
        extras.append({"name": "engine", "label": "Engine", "type": "segmented", "default": "wan",
                       "options": engines, "hint_key": "engine", "tier": "basic"})
    if _remote_has("speed_video", bool(settings.video_lightning_lora)):
        extras.append(_SPEED)
    return extras


def _video_frames(extras: list[dict]) -> dict:
    """Attach engine-dependent framing only when the engine picker exists."""
    if any(control.get("name") == "engine" for control in extras):
        return _FRAMES
    # Wan is the only possible engine, so the base descriptor already tells the
    # complete truth and must not reference a controller the form does not have.
    return {key: value for key, value in _FRAMES.items() if key != "overrides_by"}


def _lane_subtitle(lane: str, suffix: str) -> str:
    """Header line for a generator whose lane may serve several models.

    It used to name the lane's default model outright, which read as "this
    generator IS that model" — so a picker offering five of them looked like it
    was not there. Name the default, then say how many more there are.
    """
    configured = variants.available(lane)
    if not configured:
        return suffix
    head = short_name(repo) if (repo := variants.repo_of(configured[0])) else configured[0].name
    extra = f" +{len(configured) - 1} more" if len(configured) > 1 else ""
    return f"{head}{extra} · {suffix}"


def _remote_model_picker() -> list[dict]:
    """The remote GPU image-model picker, offered only when there is a real choice.

    Gated on the LIVE session, not on local `.env`, for the same reason every
    other remote GPU control is: the worker may be an older build, or be running
    with the second model unset. Offering a variant the A100 does not have would
    queue a job it silently serves from the wrong model.
    """
    configured = _available_remote_variants()
    if len(configured) < 2:
        return []
    return [_model_variant("colab", configured)]


def _image_speed(kind: str = "image") -> list[dict]:
    """The model-matched ⚡ Lightning toggle for one remote GPU image lane.

    Text generation and editing are different Qwen checkpoints with different
    adapters. Gating them separately prevents a healthy generation LoRA from
    advertising a broken edit speed path, and vice versa.
    """
    if kind == "edit":
        feature, configured = "speed_edit", bool(settings.edit_lightning_lora)
    else:
        feature, configured = "speed_image", bool(settings.image_lightning_lora)
    if not _remote_has(feature, configured):
        return []
    speed = {**_SPEED}
    if kind == "image" and len(_available_remote_variants()) > 1:
        speed["show_if"] = {"field": "model_variant", "equals": "quality"}
    return [speed]


def _controlnet_entries() -> list[dict]:
    """Experimental local ControlNet — emitted only when ENABLE_CONTROLNET is on."""
    if not settings.enable_controlnet:
        return []
    return [{
        "id": "control_local", "kind": "control_local", "title": "ControlNet (Local)",
        "subtitle": "Z-Image Fun Union · canny / depth / pose",
        "endpoint": "/api/generate/image/control", "output": "image",
        "group": "local", "device": "local", "needs_image": True, "needs_remote": False,
        "image_inputs": [{"name": "image", "label": "Control source image", "required": True}],
        "controls": [
            _PROMPT, _NEGATIVE, _AUTO_NEG_IMAGE,
            {"name": "control_mode", "label": "Control", "type": "segmented", "default": "canny",
             "options": ["canny", "depth", "pose", "none"], "hint_key": "control_mode",
             "tier": "basic"},
            {"name": "control_weight", "label": "Control weight", "type": "slider", "default": 0.7,
             "min": 0.0, "max": 1.0, "step": 0.05, "hint_key": "control_weight"},
            _quality(), _ASPECT, _GUID_CONTROL, _SEED, _BATCH,
                    *([_loras()] if _local_loras_usable() else []),
            *_POST, _PROMPT_SYNTAX, _SAMPLER, _STEPS_LOCAL, _WIDTH, _HEIGHT, _SEED_MODE,
        ],
    }]


def registry() -> list[dict]:
    """Every generator, with its controls, as the frontend renders them.

    Aliases are attached here rather than being written into each entry so that
    adding one is a single-line change to SEARCH_ALIASES and cannot drift out of
    step with the entries themselves.
    """
    return [_with_aliases(entry) for entry in _entries()]


def _with_aliases(entry: dict) -> dict:
    controls = [
        ({**control, "sweepable": True}
         if control.get("name") in GRID_SWEEPABLE else control)
        for control in entry.get("controls", [])
    ]
    entry = {**entry, "controls": controls}
    if entry.get("device") == "local":
        from ..backends.local import unavailable_reason

        if reason := unavailable_reason():
            entry["unavailable_reason"] = reason
    aliases = search_aliases(str(entry.get("kind") or entry.get("id") or ""))
    return {**entry, "aliases": aliases} if aliases else entry


def _entries() -> list[dict]:
    video_extras = _video_extras()
    return [
        *_controlnet_entries(),
        {
            "id": "image_local", "kind": "image_local", "title": "Local Image",
            "subtitle": _lane_subtitle("local", "local GPU"),
            "endpoint": "/api/generate/image/local", "output": "image",
            "group": "local", "device": "local", "needs_image": False, "needs_remote": False,
            "controls": _img_controls("local", guidance=_GUID_LOCAL, steps=_STEPS_LOCAL,
                                      extra=[_model_variant()]),
        },
        {
            "id": "image_colab", "kind": "image_colab", "title": "Remote GPU Image",
            "subtitle": _lane_subtitle("colab", "Remote GPU"),
            "endpoint": "/api/generate/image/remote", "output": "image",
            "group": "a100", "device": "a100", "needs_image": False, "needs_remote": True,
            "controls": _img_controls("a100", guidance=_guid_remote_image(), steps=_STEPS_A100,
                                      extra=[*_remote_model_picker(), *_image_speed("image")]),
        },
        {
            "id": "img2img", "kind": "img2img", "title": "Img2Img (Local)",
            "subtitle": f"{short_name(settings.local_image_model)} · transform an input image",
            "endpoint": "/api/generate/image/img2img", "output": "image",
            "group": "local", "device": "local", "needs_image": True, "needs_remote": False,
            "controls": _img_controls("local", guidance=_GUID_LOCAL, steps=_STEPS_LOCAL,
                                      extra=[_STRENGTH, _model_variant()], aspect=_INPUT_ASPECT),
        },
        {
            "id": "inpaint", "kind": "inpaint", "title": "Inpaint (Local)",
            "subtitle": f"{short_name(settings.local_image_model)} · paint a mask, regenerate",
            "endpoint": "/api/generate/image/inpaint", "output": "image",
            "group": "local", "device": "local", "needs_image": True, "needs_mask": True,
            "needs_remote": False,
            "controls": _img_controls("local", guidance=_GUID_LOCAL, steps=_STEPS_LOCAL,
                                      extra=[_STRENGTH, _INPAINT_AREA, _model_variant(),
                                             _MASK_GROW, _MASK_PADDING, _MASK_BLUR],
                                      aspect=None),
        },
        {
            "id": "outpaint", "kind": "outpaint", "title": "Outpaint (Local)",
            "subtitle": f"{short_name(settings.local_image_model)} · extend past the frame",
            "endpoint": "/api/generate/image/outpaint", "output": "image",
            "group": "local", "device": "local", "needs_image": True, "needs_remote": False,
            "image_inputs": [{"name": "image", "label": "Image to extend", "required": True}],
            # No aspect/size controls: the canvas is derived from the source plus
            # the requested growth, and the source is never scaled or cropped.
            "controls": [
                _PROMPT, _NEGATIVE, _AUTO_NEG_IMAGE, _DIRECTION, _EXPAND, _quality(),
                _GUID_LOCAL, _SEED, _BATCH,
                *([_loras()] if _local_loras_usable() else []),
                _model_variant(), _OUT_STRENGTH,
                *_POST, _PROMPT_SYNTAX, _SAMPLER, _STEPS_LOCAL, _SEED_MODE,
            ],
        },
        {
            "id": "image_edit", "kind": "image_edit", "title": "Image Edit (A100)",
            "subtitle": f"{short_name(settings.qwen_edit_model)} · 1-3 input images",
            "endpoint": "/api/generate/image/edit", "output": "image",
            "group": "a100", "device": "a100", "needs_image": True, "needs_remote": True,
            "image_inputs": [
                {"name": "image", "label": "Image 1", "required": True},
                {"name": "image_2", "label": "Image 2", "required": False},
                {"name": "image_3", "label": "Image 3", "required": False},
            ],
            # No aspect/size controls — the edit output follows the input image.
            "controls": [
                _PROMPT, _NEGATIVE, _AUTO_NEG_IMAGE, _quality(), _GUID_A100, _SEED,
                *_image_speed("edit"),
                *_POST, _PROMPT_SYNTAX, _STEPS_A100, _SEED_MODE,
            ],
        },
        {
            "id": "t2v", "kind": "t2v", "title": "Text → Video",
            "subtitle": f"{short_name(settings.video_model)} · Remote GPU",
            "endpoint": "/api/generate/video/t2v", "output": "video",
            "group": "video", "needs_image": False, "needs_remote": True,
            "controls": [
                _PROMPT, _NEGATIVE, _AUTO_NEG_VIDEO, _quality(), _CAMERA, _ORIENT, _RES,
                _video_frames(video_extras),
                _GUID_VIDEO, _FPS, _SEED,
                *video_extras, *_POST_VIDEO, _PROMPT_SYNTAX, _STEPS_VIDEO, _SEED_MODE,
            ],
        },
        {
            "id": "i2v", "kind": "i2v", "title": "Image → Video",
            "subtitle": f"{short_name(settings.video_model)} · Remote GPU",
            "endpoint": "/api/generate/video/i2v", "output": "video",
            "group": "video", "needs_image": True, "needs_remote": True,
            "image_inputs": [
                {"name": "image", "label": "First frame", "required": True},
                # Offered only when the connected A100 actually supports it.
                # Wan 2.2 TI2V-5B has no image_encoder, so a last frame has
                # nothing to condition — it used to be accepted and silently
                # discarded, producing an ordinary i2v with no hint that half
                # the input was thrown away.
                *([{"name": "last_frame", "label": "Last frame (FLF2V, optional)",
                    "required": False}] if _remote_has("flf2v", True) else []),
            ],
            "controls": [
                _PROMPT, _NEGATIVE, _AUTO_NEG_VIDEO, _quality(), _CAMERA,
                _video_frames(video_extras), _GUID_VIDEO,
                _FPS, _SEED,
                *video_extras, *_POST_VIDEO, _PROMPT_SYNTAX, _STEPS_VIDEO, _SEED_MODE,
            ],
        },
        {
            "id": "long_video", "kind": "long_video", "title": "Long Video (Extend)",
            "subtitle": "Chained shots on A100, stitched locally",
            "endpoint": "/api/generate/video/long", "output": "video",
            "group": "video", "needs_image": False, "needs_remote": True,
            "controls": [
                _PROMPT, _NEGATIVE, _AUTO_NEG_VIDEO, _quality(), _CAMERA, _SHOTS, _ORIENT, _RES,
                _video_frames([]), _GUID_VIDEO, _FPS, _SEED,
                *_POST_VIDEO, _PROMPT_SYNTAX, _STEPS_VIDEO, _SEED_MODE,
            ],
        },
    ]
