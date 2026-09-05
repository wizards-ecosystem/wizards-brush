"""Cheap latent → JPEG previews for live progress.

A rough linear projection of the first latent channels, per-image normalized —
enough to judge composition mid-run and abort a bad generation early, without a
VAE decode. Anything unexpected returns None: previews are cosmetic and must
never fail a job.
"""
from __future__ import annotations

import io
import time
from typing import Any

from . import latent_rgb

# A preview that costs more than the step it is previewing is a bug, and nothing
# would ever report it. SD.Next warns past five seconds; ours are far cheaper
# than theirs (no decoder), so the bar is lower.
SLOW_PREVIEW_SECONDS = 1.0
_slow_warned = False


def latents_to_jpeg(pipe: Any, latents: Any, *, width: int, height: int,
                    max_side: int = 160, quality: int = 60,
                    model: str | None = None) -> bytes | None:
    started = time.monotonic()
    try:
        import torch  # noqa: F401  (lazy — only present when a pipeline is running)
        from PIL import Image, ImageOps

        lat = latents
        if lat is None:
            return None
        # Newer diffusers returns nested tensors on some batched paths, which
        # would fall straight through the dim() checks below.
        if getattr(lat, "is_nested", False):
            lat = lat.tensors[0]
        # Packed transformer latents ([B, seq, C]) → spatial via the pipeline's
        # own unpacker (Flux/Qwen/Z-Image family).
        if lat.dim() == 3 and hasattr(pipe, "_unpack_latents"):
            vsf = getattr(pipe, "vae_scale_factor", 8)
            lat = pipe._unpack_latents(lat, height, width, vsf)
        if lat.dim() == 5:  # video: [B, C, F, H, W] → middle frame
            lat = lat[:, :, lat.shape[2] // 2]
        if lat.dim() != 4:
            return None

        x = lat[0].detach().float().cpu()  # [C, H, W]
        c = x.shape[0]

        # A fitted projection, when we have one for this model, is a genuine
        # linear approximation of the VAE decoder — the colours come out right
        # rather than merely plausible. See generators/latent_rgb.py.
        fitted = latent_rgb.matrix_for(model)
        if fitted is not None:
            img = _project(x, fitted)
        elif c >= 3:
            # Blend channel groups into RGB-ish planes (most VAEs concentrate
            # luminance/structure in the early channels).
            third = max(1, c // 3)
            r = x[0:third].mean(0)
            g = x[third:2 * third].mean(0)
            b = x[2 * third:3 * third].mean(0)
            img = _stack_normalize(r, g, b)
        else:
            v = x.mean(0)
            img = _stack_normalize(v, v, v)

        pil = Image.fromarray(img)
        # BILINEAR, not the default LANCZOS: this frame is discarded in 200 ms and
        # encode speed matters more than resampling quality. `contain` rather than
        # `thumbnail` because it returns a new image instead of mutating in place.
        pil = ImageOps.contain(pil, (max_side, max_side), Image.Resampling.BILINEAR)
        buf = io.BytesIO()
        pil.save(buf, "JPEG", quality=quality, optimize=False)
        _warn_if_slow(time.monotonic() - started)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 — previews must never break generation
        return None


def _warn_if_slow(seconds: float) -> None:
    """Say something, once, if generating a preview is costing real time."""
    global _slow_warned
    if seconds < SLOW_PREVIEW_SECONDS or _slow_warned:
        return
    _slow_warned = True
    from .. import log

    log.get("preview").warning(
        "preview generation took %.2fs — that is slower than the denoising step "
        "it previews. Lower PREVIEW_EVERY, or set it to 0 to turn previews off.",
        seconds)


def _project(x: Any, fitted: tuple[list[list[float]], list[float] | None]) -> Any:
    """Apply a fitted [channels, 3] projection to a [C, H, W] latent.

    Normalised after projection rather than clamped: the fit targets the decoder's
    0..1 output, but a mid-denoise latent has a wider range than a finished one,
    so clamping would crush early previews to flat blocks. Normalising keeps the
    relative structure visible at every step, which is what the preview is for.
    """
    import numpy as np
    import torch

    matrix, bias = fitted
    m = torch.tensor(matrix, dtype=torch.float32)
    c = min(x.shape[0], m.shape[0])
    flat = x[:c].reshape(c, -1).T                  # [H*W, C]
    rgb = flat @ m[:c]                              # [H*W, 3]
    if bias is not None:
        rgb = rgb + torch.tensor(bias, dtype=torch.float32)
    rgb = rgb.reshape(x.shape[1], x.shape[2], 3)
    lo, hi = rgb.min(), rgb.max()
    rgb = torch.zeros_like(rgb) if float(hi - lo) < 1e-06 else (rgb - lo) / (hi - lo)
    return (rgb.numpy() * 255.0).astype(np.uint8)


def _stack_normalize(r: Any, g: Any, b: Any) -> Any:
    import numpy as np
    import torch

    t = torch.stack([r, g, b], dim=-1)  # [H, W, 3]
    lo, hi = t.min(), t.max()
    t = torch.zeros_like(t) if float(hi - lo) < 1e-06 else (t - lo) / (hi - lo)
    return (t.numpy() * 255.0).astype(np.uint8)
