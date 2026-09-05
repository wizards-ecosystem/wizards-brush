"""Local quantization backends for the diffusion transformer.

Three options, in descending order of speed on a 16 GB consumer card:

  nunchaku  SVDQuant INT4/NVFP4. Absorbs activation outliers into a low-rank
            branch, so 4-bit holds up where naive weight-only quantization does
            not. Roughly 3x the throughput of bitsandbytes NF4 and small enough
            that the transformer stops needing CPU offload — which is where most
            of the wall-clock actually went. Needs a pre-quantized checkpoint and
            a wheel built for the installed torch minor.
  4bit/fp8  bitsandbytes. Works anywhere, no extra checkpoint, but slower and
            usually still needs offload.
  none      plain bf16.

Nunchaku is opt-in rather than default because it needs both a matching wheel
and a published checkpoint for the model in use; `resolve_backend` degrades to
bitsandbytes with a logged reason rather than failing a generation.
"""
from __future__ import annotations

from .. import log
from ..config import settings
from ..model_sources import revision_for
from ..modelprobe import short_name

logger = log.get("quant")

# model id -> repo holding its SVDQuant checkpoints. Only models with published
# Nunchaku weights can use this backend; anything else falls back.
NUNCHAKU_REPOS: dict[str, str] = {
    "Tongyi-MAI/Z-Image-Turbo": "nunchaku-ai/nunchaku-z-image-turbo",
}

# Rank trades quality for size/speed: r32 smallest, r256 closest to bf16.
# r128 is the balanced default and what Nunchaku recommends for 16 GB cards.
DEFAULT_RANK = 128


# Quantization backends that cannot accept a LoRA merged into their weights.
#
# `pipe.load_lora_weights()` adds a bf16 delta into the target tensor. An
# SVDQuant INT4 transformer has no such tensor to add into — nunchaku carries its
# own adapter API instead — so the merge path is not applicable there. Offering
# the control anyway means a user selects adapters that cannot take effect, and
# the only signal is a warning in a log they are not reading.
_NO_MERGED_ADAPTERS = frozenset({"nunchaku"})


def supports_merged_loras(model: str | None = None) -> bool:
    """Whether LoRAs can be merged into the transformer for the active backend.

    Resolved against the backend that will actually be used, not the configured
    one: `resolve_backend` degrades to bitsandbytes when a nunchaku checkpoint is
    missing, and in that degraded state adapters work fine.
    """
    try:
        backend = resolve_backend(model or settings.local_image_model)
    except Exception:  # noqa: BLE001 — a control-visibility question must not raise
        return True
    return backend not in _NO_MERGED_ADAPTERS


def precision() -> str:
    """int4 on Ampere/Ada, nvfp4 on Blackwell — Nunchaku decides from the GPU.

    Public because the loader reports it in the "how was this built" string that
    reaches the log and the model status line.
    """
    from nunchaku.utils import get_precision

    return str(get_precision())


def checkpoint_pattern(model: str, rank: int = DEFAULT_RANK) -> str:
    """Cache glob for this model/rank without importing CUDA or Nunchaku.

    Diagnostics and memory preflight run on ordinary web/worker paths where a
    torch import would be an expensive side effect.  Precision is the one
    hardware-dependent part of the filename, so leave only that segment as a
    glob.
    """
    stem = short_name(model).lower()
    return f"svdq-*_r{rank}-{stem}.safetensors"


def checkpoint_name(model: str, rank: int = DEFAULT_RANK) -> str:
    """Filename of the SVDQuant checkpoint for this model on this GPU."""
    stem = short_name(model).lower()
    return f"svdq-{precision()}_r{rank}-{stem}.safetensors"


def available(model: str) -> tuple[bool, str]:
    """(usable, reason). Reason is only meaningful when usable is False."""
    if model not in NUNCHAKU_REPOS:
        return False, f"no published Nunchaku checkpoint for {model}"
    try:
        import nunchaku  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, (f"nunchaku not installed ({e}). Wheels are built per torch "
                       "minor — see scripts/setup.sh.")
    try:
        import torch

        if not torch.cuda.is_available():
            return False, "no CUDA device"
    except Exception as e:  # noqa: BLE001
        return False, f"torch unavailable ({e})"
    return True, ""


def download_transformer(model: str, rank: int = DEFAULT_RANK, *,
                         local_files_only: bool = False) -> str:
    """Cache the SVDQuant checkpoint without constructing a CUDA module."""
    from huggingface_hub import hf_hub_download

    repo = NUNCHAKU_REPOS[model]
    fname = checkpoint_name(model, rank)
    path = hf_hub_download(
        repo,
        fname,
        token=settings.hf_token or None,
        cache_dir=settings.hf_hub_path,
        local_files_only=local_files_only,
        revision=revision_for(repo),
    )
    logger.info("nunchaku transformer: %s/%s", repo, fname)
    return str(path)


def load_transformer(model: str, rank: int = DEFAULT_RANK, *,
                     local_files_only: bool = False):
    """The SVDQuant transformer for `model`, downloaded on first use.

    Raises rather than returning None: callers decide whether to fall back, and
    a silent None would look like "quantization off" instead of "load failed".
    """
    import torch
    from nunchaku.models.transformers import NunchakuZImageTransformer2DModel

    path = download_transformer(model, rank, local_files_only=local_files_only)
    # Deliberately no device= kwarg: from_pretrained forwards **kwargs down into
    # _patch_model and on into SVDQW4A4Linear, which has its own `device`
    # parameter, so passing it raises "got multiple values for keyword argument
    # 'device'". Build on the default (CPU) and move it ourselves.
    #
    # Offload is explicitly unsupported for this model class, and unnecessary —
    # the point of SVDQuant here is that the transformer fits resident.
    transformer = NunchakuZImageTransformer2DModel.from_pretrained(
        path, torch_dtype=torch.bfloat16,
    )
    return transformer.to("cuda")


def text_encoder_config():
    """4-bit config for the text encoder.

    Nunchaku only quantizes the transformer. Z-Image's Qwen3 text encoder is
    ~8 GB in bf16, which would put the pair back over a 16 GB card and force the
    offload we just removed, so it gets bitsandbytes NF4 (it runs once per
    generation, so its speed barely matters).
    """
    import torch
    from diffusers.quantizers import PipelineQuantizationConfig

    return PipelineQuantizationConfig(
        quant_backend="bitsandbytes_4bit",
        quant_kwargs={"load_in_4bit": True, "bnb_4bit_quant_type": "nf4",
                      "bnb_4bit_compute_dtype": torch.bfloat16},  # type: ignore[dict-item]
        components_to_quantize=["text_encoder"],
    )


# Preflight result per model, so the subprocess check runs once per process.
_PREFLIGHT: dict[str, bool] = {}

_PREFLIGHT_SRC = """
import sys
from backend.app.config import settings
from backend.app.generators import quant, local_image
from backend.app.model_sources import revision_for
model = sys.argv[1]
transformer = quant.load_transformer(model)
base_cls, _, _ = local_image.resolve_classes(model)
import torch
pipe = base_cls.from_pretrained(model, transformer=transformer, torch_dtype=torch.bfloat16,
                                quantization_config=quant.text_encoder_config(),
                                cache_dir=str(settings.hf_hub_path),
                                revision=revision_for(model)).to("cuda")
pipe(prompt="x", num_inference_steps=1, guidance_scale=1.0, width=256, height=256,
     generator=torch.Generator(device="cpu").manual_seed(0))
print("PREFLIGHT_OK")
"""


def preflight(model: str) -> bool:
    """Can this GPU actually run a Nunchaku generation for `model`?

    Run in a subprocess on purpose. A kernel/API mismatch between Nunchaku and
    the installed diffusers does not raise a Python exception — it trips a C++
    assertion inside the CUDA kernel and SIGABRTs the process. Caught in-process
    that would take the whole backend down on the user's first generation, so
    the check happens somewhere expendable and the answer is cached.

    Known-bad today: Nunchaku 1.2.1's Z-Image kernel against diffusers >= 0.37.
    Its rope hook packs freqs_cis to a 256 multiple and the kernel then asserts
    `rotary_emb.shape[0] * rotary_emb.shape[1] == M`, which no resolution or
    prompt length satisfies. Nunchaku pins diffusers==0.36 in its own CI. This
    is empirical rather than a version comparison so it starts working by itself
    when upstream catches up.
    """
    if model in _PREFLIGHT:
        return _PREFLIGHT[model]

    import subprocess
    import sys

    from ..config import ROOT

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PREFLIGHT_SRC, model],
            capture_output=True, text=True, timeout=600, cwd=str(ROOT),
        )
        ok = proc.returncode == 0 and "PREFLIGHT_OK" in proc.stdout
        if not ok:
            tail = (proc.stderr or proc.stdout).strip().splitlines()
            reason = tail[-1][:200] if tail else f"exit {proc.returncode}"
            logger.warning("nunchaku preflight failed for %s: %s", model, reason)
    except Exception as e:  # noqa: BLE001 — a failed check must never block generation
        logger.warning("nunchaku preflight could not run (%s); assuming unusable.", e)
        ok = False

    _PREFLIGHT[model] = ok
    if ok:
        logger.info("nunchaku preflight OK for %s", model)
    return ok


def resolve_backend(model: str) -> str:
    """The quantization backend to actually use, after checking availability.

    Returns one of: nunchaku | 4bit | fp8 | none.
    """
    want = (settings.local_quant or "4bit").lower()
    if want not in {"nunchaku", "4bit", "fp8", "none"}:
        logger.warning("unknown LOCAL_QUANT=%r; falling back to 4bit", want)
        return "4bit"
    if want != "nunchaku":
        return want
    ok, why = available(model)
    if not ok:
        logger.warning("LOCAL_QUANT=nunchaku unusable (%s); falling back to 4bit.", why)
        return "4bit"
    if not preflight(model):
        logger.warning(
            "LOCAL_QUANT=nunchaku: the installed nunchaku cannot run %s with this "
            "diffusers build; falling back to 4bit. Re-check after upgrading either.", model)
        return "4bit"
    return "nunchaku"
