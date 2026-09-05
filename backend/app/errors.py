"""Failure classification: what went wrong, what to tell the user, what to do.

Three jobs, deliberately in one small torch-free module so the queue, the
routers and the generators can all reach it without dragging in the heavy stack.

**Classification.** `is_oom()` recognises a memory failure without importing
torch, by walking the exception's class hierarchy for the name rather than the
class object. That keeps this module importable in the torch-free CI job, which
is where most of the tests for it actually run.

**The invert-the-catch idiom.** `raise_non_oom()` turns a broad `except` into a
narrow one after the fact:

    try:
        pipe = load_quantized()
    except Exception as e:
        raise_non_oom(e)          # a typo in a model id gets re-raised here
        pipe = load_plain()       # only genuine memory pressure degrades

Before this existed, the quantization fallback in local_image caught everything,
so a wrong model id looked exactly like a card that was too small and the real
error never surfaced.

**Guidance.** `tip_for()` maps a failure to one sentence a user can act on. The
traceback is still recorded; it just stops being the whole message.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum


class Severity(IntEnum):
    """How bad a failure is, in numeric bands.

    Bands rather than a flat enum so that `>= Severity.warning` is the test for
    "this did not stop the job", and adding a new warning kind needs no change to
    anything that decides how to display one. Adapted from krita-ai-diffusion's
    ErrorKind (GPL-3.0 — reimplemented, not copied).

    Below 300 the job did not produce its output. At or above 300 it did, but
    something is worth saying.
    """

    fatal = 100        # the job failed and cannot be retried as-is
    transient = 200    # the job failed but retrying may work (a dropped tunnel)
    warning = 300      # the job succeeded; something did not apply
    notice = 400       # informational; nothing went wrong

    @property
    def is_warning(self) -> bool:
        return self >= Severity.warning

    @property
    def is_retryable(self) -> bool:
        return self == Severity.transient


@dataclass(frozen=True)
class Failure:
    """A classified failure: what to tell the user, and what it means.

    `tip` may be empty — an unrecognised error gets no invented guidance.
    """

    severity: Severity
    tip: str = ""
    clears_gpu: bool = False

    @property
    def is_warning(self) -> bool:
        return self.severity.is_warning

    @property
    def is_retryable(self) -> bool:
        return self.severity.is_retryable


def classify(e: BaseException, kind: str = "") -> Failure:
    """Full classification of a failure. `tip_for` and `clears_gpu` wrap this."""
    for match, tip, clears, severity in _RULES:
        if match(e, kind):
            return Failure(severity, tip, clears)
    return Failure(Severity.fatal)


# (predicate, tip, clears_gpu, severity)
_RULES: list[tuple[Callable[[BaseException, str], bool], str, bool, Severity]] = []


def is_oom(e: BaseException) -> bool:
    """Whether this failure is the GPU (or host) running out of memory.

    Matches by class *name* rather than by importing torch.cuda.OutOfMemoryError,
    so this module stays free of the heavy stack. The message check catches the
    driver-level variants that surface as a plain RuntimeError.
    """
    for cls in type(e).__mro__:
        if cls.__name__ in ("OutOfMemoryError", "OutOfMemoryError_"):
            return True
    msg = str(e).lower()
    return ("out of memory" in msg
            or "cuda oom" in msg
            or "cublas_status_alloc_failed" in msg
            or "not enough memory" in msg)


def raise_non_oom(e: BaseException) -> None:
    """Re-raise unless `e` is a memory failure.

    Call this as the first statement of an `except Exception` block whose only
    purpose is to degrade under memory pressure. Anything else propagates
    untouched, with its original traceback.
    """
    if not is_oom(e):
        raise e


def _rule(pattern: str | None, tip: str, *, oom: bool = False, clears_gpu: bool = False,
          kinds: tuple[str, ...] = (), severity: Severity | None = None) -> None:
    rx = re.compile(pattern, re.IGNORECASE) if pattern else None

    def match(e: BaseException, kind: str) -> bool:
        if oom and not is_oom(e):
            return False
        if kinds and kind not in kinds:
            return False
        if rx is not None and not rx.search(str(e)):
            return False
        return oom or rx is not None

    _RULES.append((match, tip, clears_gpu, severity or Severity.fatal))


# Ordered: the first match wins, so put the specific before the general.
_LOCAL_GPU_KINDS = (
    "",  # direct callers/tests with no queue kind retain conservative cleanup
    "image_local", "img2img", "inpaint", "outpaint", "control_local",
    "upscale", "face_restore", "detail", "interpolate",
)
_LOCAL_MODEL_KINDS = (
    "",  # direct model-loader callers retain useful guidance
    "image_local", "img2img", "inpaint", "outpaint", "control_local",
)
_rule(None, "The GPU ran out of memory. Try a lower quality tier, a smaller "
            "batch, or fewer LoRAs — then run it again.",
      oom=True, clears_gpu=True, kinds=_LOCAL_GPU_KINDS)
_rule(None, "The remote accelerator ran out of memory. Lower quality, frame count, "
            "or batch size and retry; the local model was left untouched.",
      oom=True)
_rule(r"mat1 and mat2 shapes|size mismatch for|shape.*invalid for input",
      "This usually means a LoRA was built for a different model than the one "
      "loaded. Check the LoRA's architecture in the picker.")
_rule(r"incompatible_lora|could not activate LoRAs",
      "The images were generated, but one or more LoRAs could not be applied.",
      severity=Severity.warning)
_rule(r"could not disable prior LoRAs|resident model state vanished",
      "The resident model could not reset its adapter state and was unloaded. "
      "Retry the job; if it repeats, check that the selected LoRA is valid.",
      clears_gpu=True)
_rule(r"LOCAL_OFFLOAD",
      "ControlNet cannot run with CPU offload enabled. Turn LOCAL_OFFLOAD off "
      "in Settings and restart.")
_rule(r"(?:Remote GPU|Colab) is not connected|no url set",
      "No Remote GPU service is configured. Paste its HTTPS URL into Settings.")
_rule(r"(?:Remote GPU|Colab) lost this job|session expired",
      "The Remote GPU worker restarted, so it no longer has this job. Restart "
      "the worker if needed, then queue the job again.")
_rule(r"(?:Remote GPU|Colab) connection lost|(?:Remote GPU|Colab) unreachable",
      "The Remote GPU connection dropped. Check that the worker and its HTTPS "
      "route are still running, then retry.", severity=Severity.transient)
_rule(r"(?:Remote GPU|Colab) (?:error )?(?:401|403)|tunnel.*(?:401|403)",
      "The Remote GPU route refused this request. Confirm that its URL and shared "
      "secret match Settings, then reconnect.")
_rule(r"401|403|gated|authorization",
      "This local model needs a Hugging Face token with access. Set or replace HF_TOKEN in "
      "the project-root .env, accept the model's licence on huggingface.co, and restart.",
      kinds=_LOCAL_MODEL_KINDS)
_rule(r"No space left on device|disk quota|507|Not enough free disk",
      "The disk is full or too close to its safety reserve on the model-cache "
      "volume. Free space there, then retry; Diagnosis shows the affected cache "
      "and filesystem.")
_rule(r"CUDA error|CUDA driver|device-side assert",
      "The GPU driver reported an error. Restarting the app usually clears it; "
      "if it repeats, the driver may need updating.", clears_gpu=True)
_rule(r"Connection refused|Name or service not known|Temporary failure in name",
      "Could not reach the network. Check your connection and try again.",
      severity=Severity.transient)


def tip_for(e: BaseException, kind: str = "") -> str:
    """One actionable sentence, or "" when we have nothing useful to add.

    Empty is a valid answer. Inventing guidance for an error we do not recognise
    would send people chasing the wrong thing.
    """
    return classify(e, kind).tip


def clears_gpu(e: BaseException, kind: str = "") -> bool:
    """Whether the GPU should be freed before the next job runs.

    An OOM leaves the allocator fragmented and whatever was resident still
    resident, so the *next* job usually fails too. Recovering here is the
    difference between "that one was too big" and "the app is broken now".
    """
    return classify(e, kind).clears_gpu
