"""The local model catalogue: what the `model_variant` control offers, and the
tuning each model needs.

One table, because the same facts were wanted in four places that each hardcoded
the turbo/quality pair independently: `resolve_model` (the repo id), the registry
(the picker's options and the guidance `defaults_by` map), `_common_params` (the
step tier and the guidance floor), and the hint text. Adding a model used to be
four edits, and the fourth was the one that got missed — that is exactly how
picking Quality ended up running a real-CFG model at Turbo's old CFG 1.0.

A variant is offered only when its setting names a repo. Clear the setting in
`.env` and the option disappears from the picker rather than becoming a button
that always fails.

Variant NAMES are persisted in job params and replayed on rerun, so they are
API, not labels. "turbo" and "quality" must keep resolving forever; renaming one
would silently re-point every stored job that used it.
"""
from __future__ import annotations

from typing import NamedTuple

from .. import log
from ..config import settings

logger = log.get("variants")


class Variant(NamedTuple):
    """One selectable model on one lane.

    `steps_group` keys into presets.QUALITY_STEPS; `guidance` is both the default
    CFG and the value the registry publishes in the slider's `defaults_by` map,
    so the two cannot drift.

    `lane` is what keeps the two catalogues from colliding: every lookup is
    lane-scoped, so a stored `model_variant` is read against the lane of the job
    that carries it and a name may legitimately mean different models on each.
    """

    lane: str
    name: str
    setting: str
    steps_group: str
    guidance: float
    default_steps: int
    note: str
    # Persisted names this row used to have. Canonical names always win if a
    # future alias collides, but collisions are rejected below so that choice
    # never depends on table order.
    aliases: tuple[str, ...] = ()
    # A regional detail pass runs only `denoise * steps` callbacks. Each model
    # needs enough requested steps to leave a useful partial schedule.
    refine_steps: int = 12
    # Hub access is needed only for a cold fetch; a complete local snapshot can
    # still be used offline after the token is removed.
    gated: bool = False
    # Z-Image can stop its costly unconditional branch late in sampling. The
    # signature filter makes this harmless data for pipelines that lack it.
    cfg_truncation: float = 1.0


# Ordered cheapest-to-run first: the picker renders them in this order, and the
# first configured one is the fallback for an unknown variant.
VARIANTS: tuple[Variant, ...] = (
    # ---- local: a 16 GB card -----------------------------------------------
    Variant("local", "turbo", "local_image_model", "local", 0.0, 9,
            "Distilled, 8 DiT forwards; official CFG 0 — fastest.", refine_steps=12),
    Variant("local", "quality", "local_image_model_hq", "local_hq", 4.0, 28,
            "Z-Image base: real CFG, more steps and stronger prompt control.",
            refine_steps=28, cfg_truncation=0.7),
    Variant("local", "klein", "local_image_model_klein", "local_klein", 1.0, 4,
            "FLUX.2-klein 4B: four-step realtime model for 4070-class GPUs.",
            refine_steps=8),
    Variant("local", "sdxl", "local_image_model_sdxl", "local_sdxl", 3.5, 30,
            "Configured SDXL checkpoint: deep LoRA ecosystem and a fixed ~1 MP bucket.",
            refine_steps=30),
    Variant("local", "chroma", "local_image_model_chroma", "local_chroma", 3.0, 40,
            "Optional compatibility slot for Chroma-family checkpoints.",
            refine_steps=40, cfg_truncation=0.7),
    Variant("local", "flux", "local_image_model_flux", "local_flux", 3.5, 28,
            "Optional gated FLUX-family slot; requires accepted Hub access on first fetch.",
            refine_steps=28, gated=True),
    # ---- remote: an A100 80 GB (legacy internal lane key: "colab") ---------
    Variant("colab", "quality", "a100_image_model", "a100", 4.0, 30,
            "Qwen-Image-2512 — official final-quality profile is 50 steps / true CFG 4"),
    Variant("colab", "hidream", "a100_image_model_hidream", "a100_hidream", 5.0, 50,
            "HiDream-O1-Image 8B — official full-model profile is 50 steps / CFG 5"),
    Variant("colab", "alt", "a100_image_model_alt", "a100_flux2", 3.5, 28,
            "optional generic A100 slot; tuned for a FLUX.2-klein-class checkpoint"),
)

LANES = ("local", "colab")
_BY_NAME = {(v.lane, v.name): v for v in VARIANTS}
_BY_ALIAS = {(v.lane, alias): v for v in VARIANTS for alias in v.aliases}
if set(_BY_NAME) & set(_BY_ALIAS):
    raise RuntimeError("a model variant alias collides with a canonical name")
if sum(len(v.aliases) for v in VARIANTS) != len(_BY_ALIAS):
    raise RuntimeError("a model variant alias is declared by more than one row")


def repo_of(v: Variant) -> str:
    """The model id a variant points at, or "" when it is not configured."""
    return str(getattr(settings, v.setting, "") or "").strip()


def name_of_repo(repo: str, lane: str = "local") -> str:
    """The variant name on `lane` that resolves to `repo`, or "".

    The inverse of `repo_of`, for the places that hold a model id and need the
    name the user chose it by — adapter pinning, mainly. Lane-scoped like every
    other lookup here, because the same name means different models per lane.
    """
    repo = (repo or "").strip()
    if not repo:
        return ""
    for v in VARIANTS:
        if v.lane == lane and repo_of(v) == repo:
            return v.name
    return ""


def by_repo(repo: str, lane: str = "local") -> Variant | None:
    """Configured catalogue row for an exact resolved model id."""
    repo = (repo or "").strip()
    return next((v for v in available(lane) if repo_of(v) == repo), None)


def available(lane: str = "local") -> list[Variant]:
    """Configured variants on `lane`, in picker order.

    May be empty for the remote lane if neither model is configured; callers use
    `default()` rather than [0] so that stays survivable.
    """
    return [v for v in VARIANTS if v.lane == lane and repo_of(v)]


def default(lane: str = "local") -> Variant:
    """The variant to fall back on: the first configured one on the lane.

    Returning a table entry rather than None keeps every caller total — an
    unknown variant costs the lane's base model, never an exception on the
    request path.
    """
    configured = available(lane)
    if configured:
        return configured[0]
    return next(v for v in VARIANTS if v.lane == lane)


def get(name: str | None, lane: str = "local") -> Variant:
    """The variant by name on `lane`, falling back to `default(lane)`.

    Unknown and unconfigured both fall back, which is what makes a stored job
    replayable after its model has been removed from `.env`: it runs on the
    lane's base model instead of failing to resolve.
    """
    requested = str(name or "")
    v = _BY_NAME.get((lane, requested))
    if v is None:
        v = _BY_ALIAS.get((lane, requested))
        if v is not None:
            logger.info("model variant alias %s/%s resolved to %s", lane, requested, v.name)
    if v is not None and repo_of(v):
        return v
    fallback = default(lane)
    if requested:
        reason = "is not configured" if v is not None else "is unknown"
        logger.warning("model variant %s/%s %s; falling back to %s",
                       lane, requested, reason, fallback.name)
    return fallback


def resolve(name: str | None, lane: str = "local") -> str:
    """The model repo id for a variant name on `lane`."""
    return repo_of(get(name, lane))


def options(lane: str = "local") -> list[str]:
    """Variant names for the lane's picker, in order."""
    return [v.name for v in available(lane)]


def guidance_map(lane: str = "local") -> dict[str, float]:
    """variant -> default CFG, for the guidance control's `defaults_by`.

    This is what moves the slider when the model changes, so switching to a
    real-CFG model does not leave it sitting at the distilled model's 1.0.
    """
    return {v.name: v.guidance for v in available(lane)}


def access_reason(name: str | None, lane: str = "local") -> str:
    """Actionable refusal for a cold gated model, or an empty string."""
    variant = get(name, lane)
    if not variant.gated or settings.hf_token:
        return ""
    model = repo_of(variant)
    if lane == "local":
        from ..backends.local import _cache_status

        if _cache_status(model)[0] == "ready":
            return ""
    return (f"{model} is gated and is not ready in the local cache. Accept its licence on "
            "Hugging Face, set HF_TOKEN in .env, restart The Wizard's Brush, and retry.")
