"""Normalising a LoRA file into what diffusers can actually load.

Lives in generators/ rather than next to the catalogue because it touches torch,
and `loras.py` is deliberately torch-free so the registry and API can list
adapters without paying for the diffusion stack.

Two real files in this library need it, and they fail in opposite ways. kohya
`.alpha` scalars make diffusers refuse the load outright — loud, easy to find.
The reference Z-Image fused `attention.qkv` layout does something worse: PEFT
reports the tensors as unexpected keys, applies everything else, and the adapter
half-works, so the image is merely weakly styled and nothing says why.
"""
from __future__ import annotations

from pathlib import Path

from .. import log

logger = log.get("loraconv")


# Suffixes that carry a LoRA's two factor matrices, in the namings trainers emit.
_FACTOR_PAIRS = ((".lora_A.weight", ".lora_B.weight"), (".lora_down.weight", ".lora_up.weight"))

# Z-Image's original (ComfyUI/reference) attention layout, and what diffusers
# calls the same tensors. `qkv` is one fused projection there and three separate
# ones here, so it is a split rather than a rename.
_FUSED_QKV = "attention.qkv"
_ATTN_OUT = "attention.out"
_QKV_PARTS = ("attention.to_q", "attention.to_k", "attention.to_v")
_ATTN_OUT_DIFFUSERS = "attention.to_out.0"


def _fold_alphas(sd: dict) -> tuple[dict, int]:
    """Drop kohya `.alpha` scalars, folding any scale they imply into lora_B.

    They encode strength as `scale = alpha / rank`. diffusers consumes the
    lora_A/lora_B pair and leaves the alpha behind, then refuses the whole load
    because the state dict "should be empty at this point" — the adapter is
    valid, nothing just knows to drop a key it already accounted for.

    Dropping them without folding would be wrong whenever `alpha != rank`: that
    silently rescales the adapter, which is a wrong image rather than an error.
    Multiplying one factor by the full scale is exact, since the model applies
    the product; lora_B is chosen because it is the zero-initialised one.
    """
    out = {k: v for k, v in sd.items() if not k.endswith(".alpha")}
    folded = 0
    for key in (k for k in sd if k.endswith(".alpha")):
        base = key[: -len(".alpha")]
        for a_suffix, b_suffix in _FACTOR_PAIRS:
            a_key, b_key = base + a_suffix, base + b_suffix
            if a_key not in out or b_key not in out:
                continue
            rank = int(out[a_key].shape[0])
            if rank > 0:
                scale = float(sd[key].reshape(-1)[0]) / rank
                if scale != 1.0:
                    out[b_key] = out[b_key] * scale
                    folded += 1
            break
    return out, folded


def _split_fused_qkv(sd: dict) -> tuple[dict, int]:
    """Rewrite the reference Z-Image attention layout into the diffusers one.

    The reference implementation fuses q, k and v into one `attention.qkv`
    projection; diffusers' ZImageTransformer2DModel keeps `to_q`/`to_k`/`to_v`
    separate and calls the output projection `to_out.0`. An adapter trained
    against the reference layout therefore has real weights with nowhere to land.

    diffusers ships no Z-Image LoRA converter, and PEFT does not fail on this —
    it reports the tensors as "unexpected keys" and applies everything else, so
    the adapter half-works and the image just looks weakly styled. That is worse
    than a rejection, because nothing surfaces it.

    The split is exact rather than approximate: lora_A is shared (one down
    projection feeds all three), and lora_B's rows are the fused output, so
    slicing it into three equal blocks reproduces q, k and v exactly.
    """
    fused = sorted({k[: -len(".lora_A.weight")] for k in sd
                    if k.endswith(".lora_A.weight") and k[: -len(".lora_A.weight")].endswith(_FUSED_QKV)})
    if not fused and not any(f"{_ATTN_OUT}.lora_A.weight" in k for k in sd):
        return sd, 0
    out = dict(sd)
    converted = 0
    for base in fused:
        a, b = out.pop(f"{base}.lora_A.weight"), out.pop(f"{base}.lora_B.weight")
        rows = b.shape[0]
        if rows % 3:
            logger.warning("fused qkv %s has %d rows, not divisible by 3 — left alone", base, rows)
            out[f"{base}.lora_A.weight"], out[f"{base}.lora_B.weight"] = a, b
            continue
        step, stem = rows // 3, base[: -len(_FUSED_QKV)]
        for i, part in enumerate(_QKV_PARTS):
            out[f"{stem}{part}.lora_A.weight"] = a.clone()
            out[f"{stem}{part}.lora_B.weight"] = b[i * step:(i + 1) * step].clone()
        converted += 1
    renamed = [k for k in out if f".{_ATTN_OUT}.lora_" in k]
    for key in renamed:
        out[key.replace(f".{_ATTN_OUT}.lora_", f".{_ATTN_OUT_DIFFUSERS}.lora_")] = out.pop(key)
    # Count modules, not tensors: lora_A and lora_B are two keys for one module,
    # and the fused split above counts modules too. A mixed unit would make the
    # log line mean nothing.
    converted += len({k.rsplit(".lora_", 1)[0] for k in renamed})
    return out, converted


def state_dict_for(path: Path) -> dict | None:
    """A loadable state dict for `path`, or None when the file needs no help.

    None means "hand diffusers the file, as before", so every adapter that loads
    correctly today keeps taking exactly the path it takes today. Only files
    diffusers would reject outright, or silently half-apply, are rewritten.
    """
    if path.suffix.lower() != ".safetensors":
        return None
    from safetensors.torch import load_file

    sd = load_file(str(path))
    needs = any(k.endswith(".alpha") for k in sd) or any(f".{_FUSED_QKV}.lora_" in k for k in sd)
    if not needs:
        return None
    sd, folded = _fold_alphas(sd)
    sd, converted = _split_fused_qkv(sd)
    logger.info("%s: normalised for diffusers (%d rescaled, %d attention modules converted)",
                path.name, folded, converted)
    return sd
