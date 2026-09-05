"""Latent-to-RGB projection matrices for live previews.

A diffusion model's latent channels are not RGB, but the mapping between them is
close enough to linear that a single matrix multiply gives a recognisable
preview — no VAE decode, no decoder weights, no extra VRAM.

    rgb = latent.movedim(0, -1) @ M        # M is [channels, 3]

**Why fitted rather than copied.** ComfyUI ships equivalent constants, but that
file is GPL-3.0 and this project is Apache-2.0, so incorporating them is not
open to us. `scripts/fit_preview_matrix.py` derives ours from the model itself
with a least-squares solve, which takes an afternoon of GPU time once per model
and leaves the result unambiguously ours. See NOTICE.

**Why not a distilled decoder (TAESD).** More accurate, and it costs a model
download plus VRAM at exactly the moment we are trying to be cheap. A preview
exists so you can abort a bad generation early; it does not need to be right in
the small, only recognisable in the large.

Entries are keyed by model id. A model with no entry falls back to the channel
grouping heuristic in `preview.py`, which is what every model used before.
"""
from __future__ import annotations

from ..modelprobe import short_name

# model id -> (matrix [channels][3], bias [3] or None)
#
# Populate with `scripts/fit_preview_matrix.py <model-id>`, which appends here.
# Empty is a valid state: previews simply use the older heuristic.
MATRICES: dict[str, tuple[list[list[float]], list[float] | None]] = {}


def matrix_for(model: str | None) -> tuple[list[list[float]], list[float] | None] | None:
    """The fitted projection for `model`, or None if we have not fitted one.

    Falls back through the repo id's stem so a local path or a revision-pinned
    id still finds the entry fitted for the canonical repo.
    """
    if not model:
        return None
    if model in MATRICES:
        return MATRICES[model]
    stem = short_name(model)
    for key, value in MATRICES.items():
        if short_name(key) == stem:
            return value
    return None
