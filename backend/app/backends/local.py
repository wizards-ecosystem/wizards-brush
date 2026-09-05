"""The local GPU as a backend.

Describes what this machine can do so the UI can reason about local and remote
through one shape. It does not implement `RemoteBackend`: running a handler here
is a function call, and the lane already dispatches those.
"""
from __future__ import annotations

import sys

from ..config import settings
from .base import Backend, BackendKind, Health, ModelInfo


class LocalBackend(Backend):
    id = "local"
    label = "This machine"
    kind = BackendKind.local

    def health(self) -> Health:
        """What the local GPU is and what it is currently holding.

        Reads a cached device probe rather than performing one. This is called on
        every settings render and in the startup banner, and performing the probe
        here would import torch into the web request path — exactly what the
        lazy-import rule exists to prevent.

        Before the background probe has run, the device reads as "detecting",
        which is honest. Saying "CPU only" at that point would be a guess
        wearing the clothes of a fact.
        """
        device, vram = _device_info()
        probed = _DEVICE is not None
        connected = bool(device) and vram != 0
        return Health(
            connected=connected,
            reason=("" if connected else
                    "no CUDA device detected" if probed else "detecting hardware"),
            device=device or ("CPU only" if probed else "detecting…"),
            vram_gb=vram,
            features=self.features(),
            models_loaded=_resident(),
            disk_free_gb=_free_gb(),
        )

    def catalog(self) -> list[ModelInfo]:
        """The variants this install is configured for.

        `ready` means the weights are already in the local cache, so the UI can
        distinguish "will start instantly" from "will download several GB first".
        """
        from ..generators import variants
        from ..modelprobe import family_of_model, short_name

        out: list[ModelInfo] = []
        seen: set[str] = set()
        resident = set(_resident())
        for variant in variants.available("local"):
            model = variants.repo_of(variant)
            if not model or model in seen:
                continue
            seen.add(model)
            status, size = _cache_status(model)
            if model in resident:
                status = "ready"
            out.append(ModelInfo(
                id=model, label=f"{short_name(model)} ({variant.name})",
                kind="image", ready=status == "ready", status=status,
                size_gb=size, family=family_of_model(model),
            ))
        return out

    def features(self) -> frozenset[str]:
        feats = {"image", "img2img", "inpaint", "outpaint", "loras", "upscale",
                 "face_restore", "detail", "interpolate"}
        if settings.enable_controlnet:
            feats.add("controlnet")
        return frozenset(feats)

    def has_resource(self, digest: str) -> bool:
        """Everything on this machine is already here."""
        return True


# Probed once, in the background, and cached. None means "not probed yet".
#
# Deliberately NOT probed on demand from health(): that would import torch into
# the web request path, and the whole reason heavy imports are lazy is that the
# server starts instantly and CI runs with no CUDA stack at all. A settings page
# must not be the thing that pulls in several hundred megabytes of libraries.
_DEVICE: tuple[str, float | None] | None = None


def probe_device() -> tuple[str, float | None]:
    """Detect the accelerator and cache the answer. Safe to call repeatedly.

    Called from the startup background thread, which already pays the torch
    import for warm-up. Returns ("", None) on any failure, because a machine
    with no GPU is an ordinary configuration to report rather than an error.
    """
    global _DEVICE
    if _DEVICE is not None:
        return _DEVICE
    try:
        import torch

        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            _DEVICE = (props.name, round(props.total_memory / 1024 ** 3, 1))
        else:
            # None is reserved for "the background probe has not answered".
            # Zero is a completed, negative probe and selects the CPU profile.
            _DEVICE = ("CPU only", 0.0)
    except Exception:  # noqa: BLE001 — absence of a GPU is a normal answer here
        _DEVICE = ("CPU only", 0.0)
    return _DEVICE


def _device_info() -> tuple[str, float | None]:
    """The cached probe result, or ("", None) if it has not run yet.

    Never imports anything. If torch happens to already be loaded — a generation
    has run — take the opportunity to fill the cache for free.
    """
    if _DEVICE is None and "torch" in sys.modules:
        return probe_device()
    return _DEVICE or ("", None)


def unavailable_reason() -> str:
    """Why local diffusion cannot run, or ``""`` while usable/unknown.

    Startup is optimistic until the background probe answers. A missing answer
    is not evidence of missing hardware; a completed zero-VRAM answer is. This
    cheap helper never imports torch and is shared by the registry and request
    preflight so the visible disabled state matches the API's behavior.
    """
    if _DEVICE is None:
        return ""
    _device, vram = _device_info()
    if vram == 0:
        return ("No CUDA GPU was detected. Local generation needs a supported NVIDIA GPU; "
                "connect a Remote GPU or check the NVIDIA driver.")
    return ""


def _resident() -> tuple[str, ...]:
    try:
        from ..generators.local_image import resident_model

        m = resident_model()
        return (m,) if m else ()
    except Exception:  # noqa: BLE001
        return ()


def _free_gb() -> float | None:
    import shutil

    try:
        return round(shutil.disk_usage(settings.output_dir).free / 1024 ** 3, 1)
    except OSError:
        return None


def _cache_status(model: str) -> tuple[str, float | None]:
    """(ready/partial/absent, downloaded GB) without network or heavy imports.

    A directory alone is not readiness: Hugging Face creates it before the
    first blob arrives. Conversely, an old `.incomplete` beside a complete
    snapshot does not make that snapshot partial. A loadable-looking snapshot
    (config plus at least one weight) therefore wins.
    """
    stem = "models--" + model.replace("/", "--")
    root = settings.hf_hub_path / stem
    if not root.exists():
        return "absent", None
    blobs = root / "blobs"
    size = 0
    try:
        size = sum(p.stat().st_size for p in blobs.iterdir()
                   if p.is_file() and not p.name.endswith(".incomplete")) if blobs.exists() else 0
        files = [p for p in (root / "snapshots").rglob("*") if p.is_file()]
    except OSError:
        return "partial", round(size / 1024 ** 3, 2) if size else None
    names = {p.name for p in files}
    has_config = bool(names & {"config.json", "model_index.json"})
    has_weights = any(p.suffix.lower() in {".safetensors", ".bin"} for p in files)
    status = "ready" if has_config and has_weights else "partial"
    return status, round(size / 1024 ** 3, 2) if size else None


def _cached(model: str) -> bool:
    """Back-compatible boolean view for callers that only need readiness."""
    return _cache_status(model)[0] == "ready"
