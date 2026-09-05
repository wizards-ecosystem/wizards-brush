#!/usr/bin/env python
"""Fit a latent-to-RGB projection for one model, for live previews.

Run once per model. Writes the result into
`backend/app/generators/latent_rgb.py`, which the preview path then uses.

    python scripts/fit_preview_matrix.py Tongyi-MAI/Z-Image-Turbo

**Method.** Sample random latents, decode them with the model's real VAE,
downsample each decoded image back to latent resolution, then solve

    latents @ M = rgb

by least squares. That is the best linear approximation of the decoder, which is
all a preview needs: recognisable composition and colour, computed in one matmul
with no decoder in memory.

**Why fit rather than copy.** Equivalent constants exist in ComfyUI, but that
file is GPL-3.0 and this project is Apache-2.0. Fitting takes a few minutes of
GPU time and the result is ours. See NOTICE.

Needs torch and diffusers, so it lives in scripts/ and is never imported by the
app — the backend's lazy-import rule stays intact.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "backend" / "app" / "generators" / "latent_rgb.py"


def fit(model: str, samples: int = 256, seed: int = 0) -> tuple[list[list[float]], list[float]]:
    import torch
    from diffusers import AutoencoderKL, DiffusionPipeline

    from backend.app.model_sources import hub_revision

    torch.manual_seed(seed)
    print(f"loading VAE for {model} …")
    try:
        vae = AutoencoderKL.from_pretrained(model, subfolder="vae",
                                            torch_dtype=torch.float32,
                                            **hub_revision(model))
    except Exception as e:  # noqa: BLE001 — any load failure means "try the pipeline"
        print(f"  no standalone vae/ subfolder ({e}); loading the full pipeline")
        pipe = DiffusionPipeline.from_pretrained(
            model, torch_dtype=torch.float32, **hub_revision(model)
        )
        vae = pipe.vae
    vae = vae.to("cuda" if torch.cuda.is_available() else "cpu").eval()

    channels = int(vae.config.latent_channels)
    scale = float(getattr(vae.config, "scaling_factor", 1.0) or 1.0)
    device = next(vae.parameters()).device
    print(f"latent channels: {channels}, scaling factor: {scale}")

    xs, ys = [], []
    with torch.no_grad():
        for i in range(0, samples, 8):
            n = min(8, samples - i)
            # Latents at the scale the sampler actually produces. Random noise
            # covers the space more evenly than encoded photographs, which
            # cluster and would bias the fit toward whatever we happened to feed it.
            lat = torch.randn(n, channels, 16, 16, device=device) * 1.0
            img = vae.decode(lat / scale).sample          # [n, 3, 128, 128]
            img = img.clamp(-1, 1).add(1).div(2)          # to 0..1
            # Average each decoder output back down to latent resolution: one
            # RGB triple per latent cell, which is what the projection maps.
            img = torch.nn.functional.adaptive_avg_pool2d(img, (lat.shape[-2], lat.shape[-1]))
            xs.append(lat.permute(0, 2, 3, 1).reshape(-1, channels).float().cpu())
            ys.append(img.permute(0, 2, 3, 1).reshape(-1, 3).float().cpu())
            print(f"  {i + n}/{samples}", end="\r")

    x = torch.cat(xs)
    y = torch.cat(ys)
    # Append a ones column so the solve produces a bias term as well as a matrix.
    x1 = torch.cat([x, torch.ones(len(x), 1)], dim=1)
    solution = torch.linalg.lstsq(x1, y).solution        # [channels + 1, 3]
    matrix, bias = solution[:-1], solution[-1]

    pred = x1 @ solution
    residual = (pred - y).abs().mean().item()
    print(f"\nmean absolute error: {residual:.4f}  (0 is exact, 0.5 is noise)")
    if residual > 0.25:
        print("WARNING: high error — this model's latents may not be linearly "
              "projectable. The preview will still work, just approximately.")
    return matrix.tolist(), bias.tolist()


def write(model: str, matrix: list[list[float]], bias: list[float]) -> None:
    text = TARGET.read_text()
    entry = (f'    {model!r}: (\n'
             f'        {_fmt(matrix)},\n'
             f'        {[round(b, 6) for b in bias]},\n'
             f'    ),\n')
    marker = "MATRICES: dict[str, tuple[list[list[float]], list[float] | None]] = {"
    if model in text:
        print(f"{model} already has an entry; remove it first to refit.")
        return
    text = text.replace(marker, marker + "\n" + entry, 1)
    TARGET.write_text(text)
    print(f"wrote {model} into {TARGET.relative_to(ROOT)}")


def _fmt(matrix: list[list[float]]) -> str:
    rows = ",\n         ".join(str([round(v, 6) for v in row]) for row in matrix)
    return "[" + rows + "]"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model", help="Hugging Face repo id")
    ap.add_argument("--samples", type=int, default=256)
    ap.add_argument("--dry-run", action="store_true", help="print, do not write")
    args = ap.parse_args()

    matrix, bias = fit(args.model, args.samples)
    if args.dry_run:
        print(f"{args.model!r}: ({_fmt(matrix)}, {[round(b, 6) for b in bias]})")
        return 0
    write(args.model, matrix, bias)
    return 0


if __name__ == "__main__":
    sys.exit(main())
