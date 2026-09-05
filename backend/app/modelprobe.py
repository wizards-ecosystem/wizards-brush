"""Identify what a model file *is* by reading only its header.

A `.safetensors` file begins with an 8-byte little-endian length followed by
that many bytes of JSON listing every tensor's name, dtype and shape. That is
enough to tell a FLUX LoRA from an SDXL one without loading a single weight,
without torch, and without reading more than a few hundred kilobytes of a file
that may be several gigabytes.

**Why this exists.** `loras.py` used to return a filename listing and nothing
else, so the picker would happily offer a FLUX adapter while a Z-Image model was
loaded. Applying it then failed deep inside the pipeline with a shape error, or
worse, silently produced noise. Architecture is knowable up front, so it should
be known up front.

**Unknown is a real answer.** A file we cannot fingerprint reports `unknown`,
never a guess. Downstream that means "do not warn about this" — punishing a
format we failed to recognise is worse than staying quiet, because the user
knows things about their files that we do not.

The fingerprint table is adapted from SD.Next's `modules/model_probe.py`
(Apache-2.0), which verified these markers against real checkpoints.
"""
from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import log

logger = log.get("modelprobe")


def short_name(model_id: str) -> str:
    """A model id reduced to the part worth showing: "Tongyi-MAI/Z-Image-Turbo"
    becomes "Z-Image-Turbo".

    One implementation because it was previously nine, in two spellings, one of
    them a private static method on the settings object that three other modules
    reached across to call. Trivial code duplicated widely is still a place for
    behaviour to diverge, and "how do we display a model id" is exactly the sort
    of thing that should look the same everywhere it appears.
    """
    return model_id.rsplit("/", 1)[-1] if model_id else ""

# safetensors puts its JSON header first; nothing legitimate needs more than this
# and a larger value would just be a way to be handed a huge allocation.
MAX_HEADER_BYTES = 16 * 1024 * 1024

# Prefixes wrapped around the diffusion core by various training tools. Stripped
# before matching so one fingerprint covers every dialect of the same model.
STRIP_PREFIXES = (
    "lora_unet_", "lora_te_", "lora_te1_", "lora_te2_",
    "model.diffusion_model.", "diffusion_model.", "transformer.", "net.",
    "base_model.model.",
)

# Suffixes that mark a file as an *adapter* rather than a full model, across the
# LoRA/LoHa/LoKr/DoRA family.
ADAPTER_SUFFIXES = (
    ".lora_down.weight", ".lora_up.weight", ".lora_A.weight", ".lora_B.weight",
    ".hada_w1_a", ".hada_w1_b", ".hada_w2_a", ".hada_w2_b",
    ".lokr_w1", ".lokr_w2", ".dora_scale", ".diff", ".diff_b",
    ".oft_blocks", ".oft_diag", ".oft_block", ".on_input", ".on_output",
    ".a1.weight", ".a2.weight", ".b1.weight", ".b2.weight",
)


def _adapter_type(header: dict[str, Any], names: list[str]) -> str:
    """Adapter algebra from tensor suffixes, ordered most-specific first."""
    blob = "\n".join(names)
    metadata = header.get("__metadata__") or {}
    module = str(metadata.get("ss_network_module") or metadata.get("network_module") or "").lower() \
        if isinstance(metadata, dict) else ""
    if any(marker in blob for marker in (".lora_down.weight", ".lora_A.weight")):
        return "DoRA" if ".dora_scale" in blob else "LoRA"
    if ".hada_w1_a" in blob:
        return "LoHa"
    if ".lokr_w1" in blob:
        return "LoKr"
    if any(marker in blob for marker in (".oft_blocks", ".oft_diag", ".oft_block")) \
            or "oft" in module:
        return "OFT"
    if any(marker in blob for marker in (".on_input", ".on_output")) or "ia3" in module:
        return "IA3"
    if all(marker in blob for marker in (".a1.weight", ".a2.weight", ".b1.weight", ".b2.weight")) \
            or "glora" in module:
        return "GLoRA"
    if any(name.endswith((".diff", ".diff_b")) for name in names):
        return "Full diff"
    if ".dora_scale" in blob:
        return "DoRA"
    if any(word in module for word in ("lora", "lycoris", "locon", "loha", "lokr")):
        return "Adapter"
    return ""


@dataclass(frozen=True)
class Fingerprint:
    """One architecture's signature.

    `required` patterns must ALL match at least one key. `forbidden` patterns
    must match none. The pairing is what separates families that share a
    prefix — SD 1.x and SDXL both have `input_blocks`, and only SDXL has
    `label_emb`, so SD is defined as "input_blocks without label_emb".
    """
    family: str
    display: str
    required: tuple[str, ...]
    forbidden: tuple[str, ...] = ()


FINGERPRINTS: tuple[Fingerprint, ...] = (
    Fingerprint("sdxl", "Stable Diffusion XL",
                (r"input_blocks\.", r"middle_block\.", r"label_emb\.")),
    Fingerprint("sd", "Stable Diffusion 1.x/2.x",
                (r"input_blocks\.", r"middle_block\."), (r"label_emb\.",)),
    Fingerprint("sd3", "Stable Diffusion 3",
                (r"joint_blocks\.", r"context_embedder\.")),
    Fingerprint("flux", "FLUX.1",
                (r"double_blocks\.\d+\.img_attn", r"vector_in\."),
                (r"distilled_guidance_layer\.", r"stream_modulation")),
    # Flux as diffusers names it, which is what most LoRA trainers now emit and
    # what `load_lora_weights` consumes directly. The row above only knows the
    # original BFL/ComfyUI naming (double_blocks / vector_in), so every
    # diffusers-format Flux adapter probed as unknown. `single_transformer_blocks`
    # is the discriminator: Qwen and Z-Image have `transformer_blocks` too, but
    # only Flux splits out a single-stream stack.
    Fingerprint("flux", "FLUX.1",
                (r"single_transformer_blocks\.\d+\.attn",),
                (r"img_mlp", r"layers\.\d+\.adaLN_modulation", r"stream_modulation")),
    Fingerprint("flux2", "FLUX.2",
                (r"double_blocks\.\d+\.img_attn", r"stream_modulation"),
                (r"vector_in\.",)),
    Fingerprint("chroma", "Chroma",
                (r"double_blocks\.\d+\.img_attn", r"distilled_guidance_layer\.")),
    Fingerprint("qwen", "Qwen Image",
                (r"transformer_blocks\.\d+\.img_mlp", r"time_text_embed\."),
                (r"audio_attn",)),
    Fingerprint("zimage", "Z-Image",
                (r"transformer_blocks\.\d+\.attn", r"(time_embed|time_text_embed)"),
                (r"img_mlp", r"joint_blocks\.", r"input_blocks\.")),
    # Z-Image LoRAs as the training tools actually emit them. ai-toolkit (and the
    # sd-scripts lineage) name the DiT's stack `layers.N.*`, not the diffusers
    # `transformer_blocks.N.*` the row above matches, so every real Z-Image
    # adapter probed as "unknown" — which makes the picker mark it compatible
    # with everything rather than grouping it with the model it belongs to.
    Fingerprint("zimage", "Z-Image",
                (r"layers\.\d+\.attention\.", r"layers\.\d+\.adaLN_modulation"),
                (r"double_blocks\.", r"input_blocks\.", r"self_attn")),
    Fingerprint("wan", "Wan Video",
                (r"blocks\.\d+\.self_attn", r"(patch_embedding|time_projection)")),
    Fingerprint("hunyuan_video", "Hunyuan Video",
                (r"double_blocks\.\d+\.img_attn", r"txt_in\.")),
    Fingerprint("ltx", "LTX Video",
                (r"transformer_blocks\.\d+\.attn", r"(adaln_single|caption_projection)")),
)


@dataclass(frozen=True)
class Probe:
    """What we learned about a file."""
    family: str = "unknown"           # "sdxl", "flux", "zimage", … or "unknown"
    display: str = "Unknown"          # human-readable architecture name
    is_adapter: bool = False          # LoRA/LoHa/LoKr/DoRA rather than a full model
    adapter_type: str = ""            # LoRA, LoHa, LoKr, OFT, IA3, GLoRA, …
    rank: int | None = None           # adapter rank, when it can be read
    keys: int = 0                     # tensor count, for diagnostics
    error: str = ""                   # why the probe failed, if it did

    @property
    def known(self) -> bool:
        return self.family != "unknown"


def read_header(path: Path | str) -> dict[str, Any] | None:
    """The safetensors JSON header, or None if this is not a readable one.

    Returns None rather than raising for every failure mode — wrong format,
    truncated file, permission denied, absurd length prefix. Callers treat an
    unreadable file as "unknown", which is already a state they handle.
    """
    try:
        with Path(path).open("rb") as f:
            raw = f.read(8)
            if len(raw) < 8:
                return None
            (length,) = struct.unpack("<Q", raw)
            if not 0 < length <= MAX_HEADER_BYTES:
                return None
            blob = f.read(length)
            if len(blob) < length:
                return None
        header = json.loads(blob)
    except (OSError, ValueError, struct.error):
        return None
    return header if isinstance(header, dict) else None


def _strip(key: str) -> str:
    for prefix in STRIP_PREFIXES:
        if key.startswith(prefix):
            return key[len(prefix):]
    return key


# Keys training tools use to record what a LoRA was trained against. Checked in
# order; the first non-empty one wins.
_DECLARED_KEYS = (
    "ss_base_model_version",     # sd-scripts / ai-toolkit
    "modelspec.architecture",    # the ModelSpec convention
    "base_model",
    "ss_base_model",
)


def _declared_family(header: dict[str, Any]) -> str:
    """The family a file declares in its own metadata, or "" if it declares none.

    Reuses `family_of_model` so a declared string is matched exactly the way a
    repo id is — one substring table, not two that drift.
    """
    meta = header.get("__metadata__") or {}
    if not isinstance(meta, dict):
        return ""
    for key in _DECLARED_KEYS:
        value = str(meta.get(key) or "").strip()
        if value:
            family = family_of_model(value)
            if family != "unknown":
                return family
    return ""


def _match(keys: list[str], fp: Fingerprint) -> bool:
    blob = "\n".join(keys)
    if any(re.search(pat, blob) for pat in fp.forbidden):
        return False
    return all(re.search(pat, blob) for pat in fp.required)


def _rank_of(header: dict[str, Any]) -> int | None:
    """Adapter rank, read from the inner dimension of a down-projection.

    `lora_down.weight` has shape [rank, in_features], so the rank is the first
    dimension. Reported for the UI; nothing depends on it being present.
    """
    for name, meta in header.items():
        if not isinstance(meta, dict):
            continue
        if name.endswith((".lora_down.weight", ".lora_A.weight", ".hada_w1_a",
                          ".lokr_w1_a", ".a1.weight")):
            shape = meta.get("shape")
            if isinstance(shape, list) and shape:
                return int(shape[0])
    return None


def probe_header(header: dict[str, Any]) -> Probe:
    """Classify an already-read header. Pure, so it is trivially testable."""
    names = [k for k in header if k != "__metadata__"]
    if not names:
        return Probe(error="no tensors in header")

    adapter_type = _adapter_type(header, names)
    is_adapter = bool(adapter_type) or any(n.endswith(ADAPTER_SUFFIXES) for n in names)
    stripped = [_strip(n) for n in names]

    # Tensor names first, declared metadata only as a fallback.
    #
    # The tempting order is the opposite — a trainer stamping the base model
    # looks like a direct statement where a fingerprint is an inference. It is
    # not reliable: a real Flux LoRA in this library carries
    # `ss_base_model_version: "sd_1.5"`, a default its trainer never updated.
    # The weights cannot be wrong about their own shape, so they decide; the
    # metadata only speaks when the shapes match nothing we know.
    for fp in FINGERPRINTS:
        if _match(stripped, fp):
            return Probe(family=fp.family, display=fp.display, is_adapter=is_adapter,
                         adapter_type=adapter_type,
                         rank=_rank_of(header) if is_adapter else None, keys=len(names))

    declared = _declared_family(header)
    if declared:
        display = next((fp.display for fp in FINGERPRINTS if fp.family == declared), declared)
        return Probe(family=declared, display=display, is_adapter=is_adapter,
                     adapter_type=adapter_type,
                     rank=_rank_of(header) if is_adapter else None, keys=len(names))
    return Probe(is_adapter=is_adapter,
                 adapter_type=adapter_type,
                 rank=_rank_of(header) if is_adapter else None, keys=len(names))


def probe_file(path: Path | str) -> Probe:
    """Classify a file on disk. Never raises."""
    header = read_header(path)
    if header is None:
        return Probe(error="not a readable safetensors file")
    try:
        return probe_header(header)
    except Exception as e:  # noqa: BLE001 — a probe is advice, never a failure
        logger.debug("probe failed for %s: %s", path, e)
        return Probe(error=str(e))


# Which adapter families can be applied to which model families. A model family
# not listed accepts only adapters of its own family, which is the safe default.
_COMPATIBLE: dict[str, frozenset[str]] = {
    "sd": frozenset({"sd"}),
    "sdxl": frozenset({"sdxl"}),
    "sd3": frozenset({"sd3"}),
    "flux": frozenset({"flux"}),
    "flux2": frozenset({"flux2"}),
    "chroma": frozenset({"chroma", "flux"}),   # Chroma is a FLUX derivative
    "qwen": frozenset({"qwen"}),
    "zimage": frozenset({"zimage"}),
    "wan": frozenset({"wan"}),
    "hunyuan_video": frozenset({"hunyuan_video"}),
    "ltx": frozenset({"ltx"}),
}


def compatible(adapter_family: str, model_family: str) -> bool:
    """Whether an adapter of one family can be applied to a model of another.

    **Unknown on either side means yes.** If we could not identify the adapter,
    or we do not know what the loaded model is, the honest answer is that we
    have no evidence of a mismatch — and hiding or warning about a file on no
    evidence is worse than letting the user try it.
    """
    if adapter_family == "unknown" or model_family == "unknown":
        return True
    return adapter_family in _COMPATIBLE.get(model_family, frozenset({model_family}))


def family_of_model(model_id: str) -> str:
    """Best-effort architecture of a model repo id, from its name.

    A cheap heuristic, used only to decide which LoRAs to *suggest*. Wrong
    answers cost a warning that should not have appeared, never a failed job.
    """
    name = model_id.lower()
    for needle, family in (
        ("z-image", "zimage"), ("zimage", "zimage"),
        ("qwen", "qwen"), ("flux.2", "flux2"), ("flux2", "flux2"),
        ("flux", "flux"), ("chroma", "chroma"),
        ("wan", "wan"), ("hunyuan", "hunyuan_video"), ("ltx", "ltx"),
        ("sdxl", "sdxl"), ("stable-diffusion-xl", "sdxl"), ("sd3", "sd3"),
    ):
        if needle in name:
            return family
    return "unknown"
