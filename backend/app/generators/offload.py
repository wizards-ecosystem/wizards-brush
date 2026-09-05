"""Making ControlNet coexist with CPU offload.

**The failure.** `enable_model_cpu_offload()` installs accelerate hooks on the
modules *registered on the pipeline it was called on*. Our ControlNet pipeline is
built from components borrowed out of the base pipeline plus a ControlNet that
was never registered anywhere, so two things end up believing they own device
placement. The transformer gets moved to CPU between steps while the ControlNet
stays on the GPU, and the next forward pass reaches across devices. CUDA answers
with an illegal memory access, which poisons the context for the whole process
and cannot be caught — only not attempted.

**The fix.** Own placement for every module in the pipeline that is actually
running. Strip the inherited hooks off the borrowed components, build the
ControlNet pipeline, then enable offload on *that* pipeline so accelerate
registers the ControlNet alongside everything else. One owner, no disagreement.

This is SD.Next's insight — a hook that manages all modules, with a per-module
opt-out — at the smallest scale that solves our problem. Their balanced offload
is ~400 lines across six modules with ten configuration options and a validate()
function to clamp the combinations users get wrong. We need one behaviour.

**Why this is opt-in.** The failure mode is process death, and the fix cannot be
verified without a CUDA device. `CONTROLNET_OFFLOAD` defaults to "refuse", which
keeps today's behaviour: a clear error before anything is loaded. Set it to
"rehook" to use the path below. That is a deliberate choice to ship a capability
without making an unverified path the default.
"""
from __future__ import annotations

from typing import Any

from .. import log

logger = log.get("offload")


def strip_hooks(modules: dict[str, Any]) -> int:
    """Remove accelerate hooks from borrowed components. Returns how many.

    The components handed out by `_pipe_for` are shared: they carry whatever
    hooks the base pipeline installed. Reusing them in a differently-shaped
    pipeline means those hooks describe a placement plan for a pipeline that is
    no longer running.
    """
    # Look before importing. Nothing to strip is the common case (offload off),
    # and accelerate pulls in torch — an import this function has no business
    # forcing when it has no work to do.
    hooked = {n: m for n, m in modules.items() if hasattr(m, "_hf_hook")}
    if not hooked:
        return 0
    try:
        from accelerate.hooks import remove_hook_from_module
    except ImportError:
        logger.warning("accelerate unavailable; cannot strip inherited offload hooks")
        return 0
    stripped = 0
    for name, module in hooked.items():
        try:
            remove_hook_from_module(module, recurse=True)
            stripped += 1
        except Exception as e:  # noqa: BLE001 — a module that resists is not fatal
            logger.debug("could not strip hook from %s: %s", name, e)
    return stripped


def enable_for_pipeline(pipe: Any) -> str:
    """Install offload on `pipe`, so it owns placement for all of its modules.

    Returns a short description of what was done, for the load log. Raises if
    offload cannot be installed — the caller must not proceed with half a plan,
    because that is precisely the state that aborts the process.
    """
    if not hasattr(pipe, "enable_model_cpu_offload"):
        raise RuntimeError(f"{type(pipe).__name__} does not support model CPU offload")
    pipe.enable_model_cpu_offload()
    return "offload(pipeline-owned)"


def prepare_controlnet_pipeline(components: dict[str, Any], controlnet: Any,
                                pipeline_cls: Any, *, offload: bool) -> tuple[Any, str]:
    """Build a ControlNet pipeline whose device placement has exactly one owner.

    With `offload=False` everything is resident and there is nothing to
    reconcile — the ControlNet is moved to the accelerator alongside the rest.

    With `offload=True` the borrowed components are un-hooked first, so the new
    pipeline's own `enable_model_cpu_offload()` is the only thing describing
    where anything lives.

    **The caller must drop the cached base pipeline afterwards.** Its components
    have been mutated: whatever hooks they arrived with are gone, so the next
    ordinary generation would run an unhooked pipeline that believes it is
    offloaded. `local_image.generate_control` clears `_STATE` in its finally
    block for exactly this reason.
    """
    import torch

    if not offload:
        controlnet.to("cuda", dtype=torch.bfloat16)
        pipe = pipeline_cls(**components, controlnet=controlnet)
        return pipe, "resident"

    stripped = strip_hooks(components)
    logger.info("stripped accelerate hooks from %d borrowed component(s)", stripped)
    controlnet.to("cpu", dtype=torch.bfloat16)   # offload will place it
    pipe = pipeline_cls(**components, controlnet=controlnet)
    how = enable_for_pipeline(pipe)
    return pipe, how
