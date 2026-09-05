"""Estimating VRAM before spending it.

Our out-of-memory handling is a recovery path: catch it, free the pipelines, tell
the user to lower the tier. That is the right behaviour once it has happened, and
it is a bad substitute for not hitting it.

The peak of a generation is not the weights — those are known and resident — it
is the VAE decode at the end, which allocates activations proportional to the
output area. That allocation is predictable, so the decision to tile it can be
made before the run rather than after the failure.

The constants are measured, not derived. They are the bytes of peak working
memory per output pixel per element, and they differ by operation because
decoding allocates roughly twice what encoding does. They will be wrong for a
model family nobody has measured; `SAFETY` exists to absorb that, and the whole
thing degrades to "do not tile" rather than to a crash.

Adapted in design from InvokeAI's `backend/util/vae_working_memory.py`
(Apache-2.0), whose constants come with the issue number that produced them.
"""
from __future__ import annotations

import shutil

from .. import log

logger = log.get("vram")

# Bytes of peak working memory per output pixel, per byte of element size.
# Decode allocates about twice what encode does.
DECODE_PER_PIXEL = 2200

# Everything not accounted for: fragmentation, the allocator's own reserve,
# whatever else is resident. A generous margin is cheap; an OOM is not.
SAFETY = 1.35

# Tiling costs quality at the seams and time in overlap. Only worth it when a
# single-pass decode genuinely will not fit.
TILE_THRESHOLD = 0.9

# The exact geometry installed on Diffusers VAEs by local_image._set_vae_tiling.
# Keeping it here makes the memory estimate and the decoder consume one value.
VAE_TILE_SIZE = 512
VAE_TILE_OVERLAP = 0.25


def decode_bytes(width: int, height: int, *, fp32: bool = False,
                 tile_size: int | None = None) -> int:
    """Estimated peak working memory for a VAE decode, in bytes.

    With `tile_size`, the estimate is for one tile plus 25% for overlap and
    per-tile overhead — which is what makes "would tiling fit?" answerable.
    """
    element = 4 if fp32 else 2
    if tile_size:
        pixels = tile_size * tile_size
        return int(pixels * element * DECODE_PER_PIXEL * SAFETY * 1.25)
    return int(width * height * element * DECODE_PER_PIXEL * SAFETY)


def free_vram_bytes() -> int | None:
    """Free VRAM right now, or None if that cannot be determined.

    None is a real answer and every caller must treat it as "do not make a
    decision" rather than as zero — guessing that nothing is free would make us
    tile everything.
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        free, _total = torch.cuda.mem_get_info()
        stats = torch.cuda.memory_stats()
        return _free_plus_reclaimable(
            int(free),
            int(stats.get("reserved_bytes.all.current", 0)),
            int(stats.get("active_bytes.all.current", 0)),
        )
    except Exception:  # noqa: BLE001 — an estimate must never raise
        return None


def _free_plus_reclaimable(driver_free: int, reserved: int, active: int) -> int:
    """Driver-free memory plus unused blocks held by PyTorch's allocator."""
    return max(0, int(driver_free)) + max(0, int(reserved) - int(active))


def reset_peak_vram() -> None:
    """Start a per-item CUDA high-water measurement; a no-op off CUDA."""
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except Exception:  # noqa: BLE001 — diagnostics never fail generation
        return


def peak_vram() -> dict[str, float]:
    """Peak live and allocator-reserved CUDA memory since the last reset."""
    try:
        import torch

        if not torch.cuda.is_available():
            return {}
        return {
            "peak_vram_gb": round(int(torch.cuda.max_memory_allocated()) / 1024 ** 3, 3),
            "peak_reserved_vram_gb": round(int(torch.cuda.max_memory_reserved()) / 1024 ** 3, 3),
        }
    except Exception:  # noqa: BLE001 — diagnostics never fail generation
        return {}


def should_tile_decode(width: int, height: int, *, fp32: bool = False) -> bool:
    """Whether a full-frame decode is likely to run out of memory.

    False when free VRAM is unknown: tiling on a guess would cost quality and
    time on every generation for a problem that may not exist.
    """
    free = free_vram_bytes()
    if free is None:
        return False
    needed = decode_bytes(width, height, fp32=fp32)
    tile = needed > free * TILE_THRESHOLD
    if tile:
        tiled_needed = decode_bytes(
            width, height, fp32=fp32, tile_size=VAE_TILE_SIZE)
        logger.info(
            "decode of %dx%d needs ~%.1f GB and %.1f GB is free — tiling at %d px "
            "(~%.1f GB per tile)",
            width, height, needed / 1024 ** 3, free / 1024 ** 3,
            VAE_TILE_SIZE, tiled_needed / 1024 ** 3)
        if tiled_needed > free * TILE_THRESHOLD:
            logger.warning(
                "even one %d px VAE tile is estimated at %.1f GB against %.1f GB free; "
                "tiling is still the lowest-memory path but this decode may fail",
                VAE_TILE_SIZE, tiled_needed / 1024 ** 3, free / 1024 ** 3)
    return tile


def describe(width: int, height: int, *, fp32: bool = False) -> dict:
    """The numbers behind the decision, for logs and diagnostics."""
    free = free_vram_bytes()
    needed = decode_bytes(width, height, fp32=fp32)
    return {
        "output": f"{width}x{height}",
        "decode_estimate_gb": round(needed / 1024 ** 3, 2),
        "tile_size": VAE_TILE_SIZE,
        "tile_estimate_gb": round(
            decode_bytes(width, height, fp32=fp32, tile_size=VAE_TILE_SIZE) / 1024 ** 3, 2),
        "free_vram_gb": round(free / 1024 ** 3, 2) if free is not None else None,
        "would_tile": should_tile_decode(width, height, fp32=fp32),
    }


# ---- host RAM ------------------------------------------------------------
# The same idea one level out, for a failure that is much worse than an OOM.
#
# A CUDA OOM is recoverable: the handler frees the pipelines and the user lowers
# the tier. The bf16+offload fallback that runs on that OOM is not, because
# `enable_model_cpu_offload` keeps every component in HOST RAM and streams
# layers to the GPU on demand. A 12B transformer plus a T5-XXL text encoder is
# ~34 GB in bf16, and asking a 20 GB VM for it does not raise — it invokes the
# kernel OOM killer, which killed the whole WSL VM mid-session (three oom-kills
# on one pid, then the VM died, repeatedly).
#
# So the graceful degradation was the crash. Check whether the host can back the
# load before attempting it, and fail the job with a message instead.

# Weights are not the whole resident footprint: the allocator, the CUDA context
# and the pinned staging buffers all sit alongside them. Measured overhead is
# well under 15%, and refusing slightly early is much cheaper than being killed.
HOST_SAFETY = 1.15

WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pth", ".ckpt")

# A model fetch is not allowed to consume the filesystem's last gigabyte. The
# size of an arbitrary Diffusers repo is not reliably knowable without asking
# the Hub (and whole-repo totals overcount alternate weight formats), so this is
# a deliberately modest hard floor rather than a fabricated per-model number.
# It catches the dangerous case — beginning a multi-GB fetch on an already-full
# volume — while unknown measurements and healthy caches remain permissive.
DISK_RESERVE_BYTES = 1 * 1024 ** 3
MIN_MODEL_FETCH_BYTES = 8 * 1024 ** 3


def free_model_disk_bytes() -> int | None:
    """Free bytes on the filesystem that actually holds the HF cache."""
    from ..config import settings

    try:
        # ensure_dirs creates the cache in normal startup. Walking to an
        # existing parent keeps this probe usable in scripts before that point.
        path = settings.hf_hub_path
        while not path.exists() and path != path.parent:
            path = path.parent
        return int(shutil.disk_usage(path).free)
    except OSError:
        return None


def model_download_shortfall(model: str) -> tuple[int, int] | None:
    """(required free bytes, actual free bytes) before a cold/partial fetch.

    A ready snapshot needs no download headroom. Unknown disk capacity permits
    the load: a false refusal is worse than proceeding when the OS cannot report
    the filesystem. Partial caches still get the floor because a one-byte
    fragment is every bit as incomplete as a nearly finished transfer.
    """
    from ..backends.local import _cache_status

    if _cache_status(model)[0] == "ready":
        return None
    free = free_model_disk_bytes()
    if free is None:
        return None
    required = MIN_MODEL_FETCH_BYTES + DISK_RESERVE_BYTES
    return (required, free) if free < required else None


def free_host_ram_bytes() -> int | None:
    """Host RAM genuinely available right now, or None if it cannot be read.

    MemAvailable, not MemFree: the kernel's own estimate of what a new
    allocation can get without swapping, which already accounts for reclaimable
    page cache. MemFree would read as almost nothing on a warm machine and make
    us refuse every load.
    """
    from pathlib import Path

    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except Exception:  # noqa: BLE001 — a probe must never raise
        pass
    return None


def _snapshot_weight_bytes(
    model: str,
    *,
    exclude_components: frozenset[str] = frozenset(),
    filename_glob: str | None = None,
    largest_match_only: bool = False,
) -> int | None:
    """Weight bytes in the newest cached snapshot, with component filtering.

    Diffusers component directories are top-level snapshot directories.  This
    lets callers describe the files a composed pipeline will really consume
    instead of charging it for a component supplied from another repository.
    ``largest_match_only`` is for mutually exclusive checkpoint variants (for
    example Nunchaku INT4 versus NVFP4) that may both remain in the cache even
    though a load selects exactly one.
    Bytes are read from the cache rather than the Hub so this costs no network
    and works offline. None when the model has not been fetched yet is the
    honest answer, and callers treat it as "cannot decide", never as zero.

    Blob symlinks are resolved before stat: a snapshot directory is largely
    symlinks into `blobs/`, so an unresolved stat reports the size of the link.
    """
    from ..config import settings

    snapshots = settings.hf_hub_path / f"models--{model.replace('/', '--')}" / "snapshots"
    if not snapshots.is_dir():
        return None
    try:
        snaps = sorted((p for p in snapshots.iterdir() if p.is_dir()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    if not snaps:
        return None
    sizes: list[int] = []
    for p in snaps[0].rglob("*"):
        try:
            relative = p.relative_to(snaps[0])
        except ValueError:
            continue
        if relative.parts and relative.parts[0] in exclude_components:
            continue
        if filename_glob is not None and not p.match(filename_glob):
            continue
        if p.suffix.lower() not in WEIGHT_SUFFIXES:
            continue
        try:
            sizes.append(p.resolve().stat().st_size)
        except OSError:  # a dangling symlink is a partial download
            continue
    if not sizes:
        return None
    return max(sizes) if largest_match_only else sum(sizes)


def model_weight_bytes(model: str) -> int | None:
    """Bytes of all weight files in the newest cached model snapshot."""
    return _snapshot_weight_bytes(model)


def _nunchaku_host_weight_bytes(model: str) -> int | None:
    """Estimated resident bytes for the artifacts a Nunchaku load consumes.

    Nunchaku does not stream the base repository's transformer.  It loads one
    already-quantized SVDQuant checkpoint from a separate repository and asks
    Diffusers for only the base model's remaining components.  Charging a
    fraction of the *whole* base repo made a 3.7 GB transformer look like a
    14+ GB load and refused machines that could run it.

    The remaining base components include a text encoder quantized through the
    same shard-wise bitsandbytes path as a regular 4-bit load, while the chosen
    SVDQuant file itself must be resident.  Return the weighted sum before the
    common safety margin.  Unknown cache state stays unknown.
    """
    from . import quant as q

    repo = q.NUNCHAKU_REPOS.get(model)
    if repo is None:
        return None
    base = _snapshot_weight_bytes(model, exclude_components=frozenset({"transformer"}))
    transformer = _snapshot_weight_bytes(
        repo,
        filename_glob=q.checkpoint_pattern(model),
        largest_match_only=True,
    )
    if base is None or transformer is None:
        return None
    return int(base * host_fraction("4bit") + transformer)


# Host RAM a load needs, as a fraction of the weights on disk.
#
# Quantizing does not make the load free — the weights still stream through host
# memory on the way to the card, and with LOCAL_OFFLOAD the quantized copy stays
# there — but it is nowhere near the full tensor set either.
#
# These are calibrated against what this machine has actually done, which is the
# only reason to trust them. Z-Image-Turbo (30.6 GB of weights) and FLUX.1-dev
# (31.4 GB) are the same size to within 3%, and on the 4-bit path Turbo loads
# fine while FLUX took out 16 GB of swap. So the size of a model is NOT what
# separates the two, and a fraction tuned to reject FLUX by size would reject
# Turbo with it.
#
# What separated them was headroom at the moment they started: Turbo loaded into
# an idle machine, FLUX started with 0.7 GB free because other jobs were already
# resident. That is what this guard is for — refusing to *begin* a large load
# into memory that is already gone — so 4-bit is set to pass every model this
# machine has run when idle, and to refuse any of them under real pressure.
#
# bf16 stays at 1.0 because nothing is compressed: the full tensor set lands in
# host RAM and offload keeps it there.
# 0.45 was slightly above the host's idle availability once normal desktop
# services started (15.82 GiB required versus 15.77 GiB available), despite the
# measured 4-bit peak falling below it. 0.43 keeps the 15% safety factor while
# allowing a normal 16 GB profile to load its supported models.
_HOST_FRACTION = {"none": 1.0, "bf16": 1.0, "fp8": 0.6, "4bit": 0.43, "nunchaku": 0.4}


def host_fraction(quant: str) -> float:
    """Fraction of on-disk weight bytes a `quant` load needs in host RAM."""
    return _HOST_FRACTION.get((quant or "").lower(), 1.0)


def host_ram_shortfall(model: str, quant: str = "bf16") -> tuple[int, int] | None:
    """(needed, available) when host RAM cannot back loading `model`.

    None means "load it": either it fits, or one of the two numbers is unknown.
    Refusing on an unknown would ground every model that has not been downloaded
    yet, which is exactly the case where we most want the download to proceed.

    `quant` matters because the answer differs by a factor of two between a bf16
    load and a 4-bit one, and refusing a 4-bit load that would have fitted is as
    much of a bug as allowing a bf16 one that will not.
    """
    normalized_quant = (quant or "").lower()
    need = _nunchaku_host_weight_bytes(model) if normalized_quant == "nunchaku" else None
    weighted = need is not None
    # Preserve the old conservative estimate if either composed repository is
    # not measurable yet.  In the normal path prefetch has completed before
    # this guard runs, so this is only an offline/partial-cache fallback.
    if need is None:
        need = model_weight_bytes(model)
    free = free_host_ram_bytes()
    if need is None or free is None:
        return None
    if not weighted:
        need = int(need * host_fraction(normalized_quant) * HOST_SAFETY)
    else:
        need = int(need * HOST_SAFETY)
    return (need, free) if need > free else None
