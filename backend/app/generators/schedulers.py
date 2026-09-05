"""Sampler (scheduler) selection for the local pipeline.

Only samplers from the model's own family are offered. Diffusers will happily
build most schedulers `from_config` and then produce noise, which is the failure
mode this whole codebase keeps running into: something that looks like it worked.
Z-Image is a flow-match model, so the list is flow-match only.

Kept free of heavy imports — the registry reads OPTIONS to build its control,
and the registry must stay torch-free.
"""
from __future__ import annotations

from .. import log

logger = log.get("schedulers")

# UI value -> (diffusers class, one-line description for the hint).
# "default" means "leave the scheduler the model shipped with".
SAMPLERS: dict[str, tuple[str, str]] = {
    "default": ("", "the scheduler the model ships with (flow-match Euler for Z-Image)"),
    "euler": ("FlowMatchEulerDiscreteScheduler", "explicit Euler — same as default, pinned"),
    "lcm": ("FlowMatchLCMScheduler",
            "few-step consistency sampling; suits the distilled Turbo model"),
    "flowmap": ("FlowMapEulerDiscreteScheduler",
                "Euler over a learned flow map; different trajectory, same cost"),
}

# FlowMatchHeunDiscreteScheduler is deliberately absent. It is flow-match and it
# builds from_config without complaint, but its set_timesteps takes no `sigmas`
# argument, and Z-Image's pipeline always passes a custom sigma schedule — so
# every generation raises. Being in the right family is not sufficient, which is
# what _supports_sigmas() below now checks for anything added here later.

OPTIONS = list(SAMPLERS)


def apply(pipe, sampler: str, *, model: str = "") -> str:
    """Swap `pipe`'s scheduler. Returns the name actually in effect.

    Raises on an unusable choice rather than falling back silently: the user
    picked this explicitly, and a sampler that quietly did not apply would leave
    them comparing two identical images and drawing the wrong conclusion.
    """
    name = (sampler or "default").lower()
    if name not in SAMPLERS:
        raise ValueError(f"unknown sampler {sampler!r}; expected one of {OPTIONS}")
    cls_name = SAMPLERS[name][0]
    if not cls_name:
        return _apply_model_default(pipe, model)

    import diffusers

    cls = getattr(diffusers, cls_name, None)
    if cls is None:
        raise ValueError(f"{cls_name} is not available in this diffusers build")
    if not _supports_sigmas(cls):
        raise ValueError(
            f"{cls_name} cannot be used here: it accepts no custom sigma schedule, "
            "and this pipeline always passes one. Building it would succeed and "
            "then fail on every generation."
        )
    try:
        pipe.scheduler = cls.from_config(pipe.scheduler.config)
    except Exception as e:
        raise ValueError(f"{cls_name} is not compatible with this model: {e}") from e
    logger.info("sampler: %s", cls_name)
    return name


def _apply_model_default(pipe, model: str) -> str:
    """Install the catalogue default when the repository ships a weak default.

    This is intentionally SDXL-only. Flow-matching schedulers are part of how
    Z-Image/Flux-family models were trained and must remain untouched.
    """
    from ..modelprobe import family_of_model

    if family_of_model(model) != "sdxl":
        return "default"
    try:
        from diffusers import DPMSolverMultistepScheduler

        pipe.scheduler = DPMSolverMultistepScheduler.from_config(
            pipe.scheduler.config,
            algorithm_type="sde-dpmsolver++",
            use_karras_sigmas=True,
        )
    except Exception as error:  # noqa: BLE001 — shipped scheduler is a safe fallback
        logger.warning("keeping shipped SDXL scheduler %s: %s",
                       type(pipe.scheduler).__name__, error)
        return "default"
    logger.info("sampler: DPM++ 2M SDE Karras (SDXL default)")
    return "dpmpp_2m_sde_karras"


def _supports_sigmas(cls) -> bool:
    """Does this scheduler accept a custom sigma schedule?

    The check that separates "same family" from "actually usable". Z-Image's
    pipeline calls retrieve_timesteps(..., sigmas=...), and a scheduler whose
    set_timesteps lacks that parameter raises there — after building cleanly,
    so from_config alone tells you nothing.
    """
    import inspect

    fn = getattr(cls, "set_timesteps", None)
    if fn is None:
        return False       # not a scheduler at all
    try:
        return "sigmas" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False       # builtin or C-level signature we cannot read
