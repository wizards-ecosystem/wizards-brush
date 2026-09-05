"""Shared generator helpers: HF env setup and the resolution table.

The `_RES` table and `res_for()` are shared with the Remote GPU worker so
local + remote pipelines agree on dimensions.
"""
from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Callable

from ..config import settings

# Single mutex for ALL local-GPU work (generate + upscale + face-restore).
# The local and remote job lanes run in parallel, but only one GPU op may run at
# a time — this prevents two pipelines colliding on the 16 GB card. Remote A100
# jobs hold no GPU, so they never contend for this lock.
# Re-entrant because a local generation may classify its own OOM and call the
# global cleanup while it already owns the device.  Cleanup from the remote
# lane waits here instead of clearing the resident state underneath an active
# local generation.
GPU_LOCK = threading.RLock()

# (resolution, orientation) -> (width, height). From videogenerator.ipynb.
# Still used by video (Wan has specific supported sizes) and for legacy job rerun.
_RES = {
    ("720p", "landscape"): (1280, 704), ("720p", "portrait"): (704, 1280), ("720p", "square"): (704, 704),
    ("480p", "landscape"): (832, 480), ("480p", "portrait"): (480, 832), ("480p", "square"): (512, 512),
}


def res_for(resolution: str, orientation: str) -> tuple[int, int]:
    return _RES.get((resolution, orientation), (1024, 1024))


# ---- aspect-ratio + quality-tier sizing (images) --------------------------
# Aspect label -> (ratio_w, ratio_h). Frontend renders these as a visual picker.
ASPECTS: dict[str, tuple[int, int]] = {
    "1:1": (1, 1), "3:4": (3, 4), "4:3": (4, 3), "9:16": (9, 16),
    "16:9": (16, 9), "2:3": (2, 3), "3:2": (3, 2),
}

# Megapixel target per quality tier, per device.
#
# The "local" row is now a fallback: `dims_for` asks hardware.active() for the
# real numbers, so an 8 GB card gets smaller targets and a 24 GB card gets larger
# ones instead of everyone inheriting values tuned for one 16 GB machine. The
# a100 row stays fixed because that device is known.
# A "<device>_<family>" key overrides the plain device row for one architecture;
# see dims_for.
#
# The SDXL row exists because SDXL is area-bucketed, not resolution-scalable.
# Every bucket it was trained on is ~1.05 MP — 1024x1024, 1152x896, 1216x832,
# 1344x768 — varying the ASPECT at constant area rather than the area itself.
# Past that it does not render more detail, it renders the subject twice: a
# second torso, a third arm, a mirrored face. The generic a100 row targets 1.60
# MP at High, which is right for Qwen-Image (native 1328px) and actively broken
# for an SDXL checkpoint. So SDXL's High and Standard share an area on purpose —
# its High tier buys more steps, not more pixels, and extra resolution comes from
# upscaling afterwards, which is what the "Finishing" control is for.
_TIER_MP: dict[str, dict[str, float]] = {
    "local": {"Draft": 0.30, "Standard": 0.60, "High": 0.92},
    "a100": {"Draft": 0.55, "Standard": 1.05, "High": 1.60},
    # Qwen-Image-2512's official 2:3 example is exactly 1056x1584 (1.673 MP).
    # It is the image model's documented quality bucket, so use it instead of
    # rounding the generic A100 budget down to 1040x1552.
    "a100_qwen": {"Draft": 0.55, "Standard": 1.05, "High": 1.673},
    "a100_sdxl": {"Draft": 0.65, "Standard": 1.05, "High": 1.05},
    # Not derived from VRAM. The hardware row is tuned for a quantized 6-12B
    # DiT and tops out at 0.92 MP on a 16 GB card; SDXL is a 2.6B UNet that fits
    # several times over, so that budget would put it BELOW its trained bucket
    # (softer, worse composition) for no memory reason at all.
    "local_sdxl": {"Draft": 0.65, "Standard": 1.05, "High": 1.05},
}
# Hard per-side ceilings so an extreme aspect can't blow past VRAM.
_MAX_SIDE: dict[str, int] = {"local": 1280, "a100": 1664,
                             "a100_qwen": 1664, "a100_sdxl": 1536, "local_sdxl": 1536}


def _local_limits() -> tuple[dict[str, float], int]:
    """(tier megapixels, max side) for this machine.

    Falls back to the table above if the hardware layer is unavailable for any
    reason — sizing must always produce numbers, because refusing to pick a
    resolution means refusing to generate.
    """
    try:
        from ..hardware import active

        p = active()
        return p.tier_mp or _TIER_MP["local"], p.max_side
    except Exception:  # noqa: BLE001 — sizing must never fail
        return _TIER_MP["local"], _MAX_SIDE["local"]


def _snap(x: float, multiple: int = 16) -> int:
    return max(multiple, round(x / multiple) * multiple)


def _cap_preserve(w: float, h: float, cap: int) -> tuple[int, int]:
    """Snap to /16 and cap each side, scaling both together if either exceeds the
    cap so the aspect ratio is preserved rather than distorted."""
    m = max(w, h)
    if m > cap:
        w, h = w * cap / m, h * cap / m
    return _snap(w), _snap(h)


def _dims_for_exact_ratio(
    rw: int, rh: int, tier_mp: dict[str, float], tier: str, cap: int,
) -> tuple[int, int]:
    """Fit a named integer ratio without distorting it during /16 snapping.

    Snapping width and height independently turns 16:9 into 1040:576. Instead,
    solve one shared /16 scale: `(rw * k, rh * k)`. Both sides remain legal
    latent dimensions and the displayed aspect is the delivered aspect.
    """
    rw, rh = max(1, int(rw)), max(1, int(rh))
    target = tier_mp.get(tier, 0.6) * 1_000_000
    ideal_scale = (target / (rw * rh)) ** 0.5
    max_scale = cap / max(rw, rh)
    scale = min(ideal_scale, max_scale)
    # A shared multiple of 16 makes both dimensions /16 for every integer
    # picker ratio. If the nearest value crosses the hard side ceiling, floor.
    units = max(1, round(scale / 16))
    if 16 * units * max(rw, rh) > cap:
        units = max(1, int(max_scale // 16))
    shared = 16 * units
    return rw * shared, rh * shared


def dims_for(
    *, aspect: str = "1:1", tier: str = "Standard", device: str = "local",
    width: int = 0, height: int = 0, family: str = "",
) -> tuple[int, int]:
    """Resolve output width/height from an aspect ratio + quality tier, snapped to
    /16 and capped for the device. Explicit width/height (advanced/custom) win.

    `family` is the architecture of the model that will actually run (see
    modelprobe.family_of_model). A device row describes the hardware's budget; a
    family row describes what the architecture can render without falling apart,
    and where both exist the family wins because VRAM headroom does not make
    SDXL stop duplicating the subject.
    """
    key = f"{device}_{family}" if family else ""
    if key in _TIER_MP:
        # An architecture's own limits beat both the device row and the probed
        # hardware budget: more VRAM does not make SDXL stop duplicating the
        # subject, and less VRAM is not why it should render below its bucket.
        tier_mp, cap = _TIER_MP[key], _MAX_SIDE.get(key, 1280)
    elif device == "local":
        tier_mp, cap = _local_limits()
    else:
        tier_mp, cap = _TIER_MP.get(device, _TIER_MP["local"]), _MAX_SIDE.get(device, 1280)
    if width and height:
        return _cap_preserve(width, height, cap)
    rw, rh = ASPECTS.get(aspect, (1, 1))
    return _dims_for_exact_ratio(rw, rh, tier_mp, tier, cap)


def _dims_for_ratio(
    rw: float, rh: float, tier_mp: dict[str, float], tier: str, cap: int,
) -> tuple[int, int]:
    """Fit an arbitrary aspect into one model/device quality bucket."""
    rw, rh = max(float(rw), 1.0), max(float(rh), 1.0)
    mp = tier_mp.get(tier, 0.6)
    ratio = rw / rh
    h = (mp * 1_000_000 / ratio) ** 0.5
    return _cap_preserve(h * ratio, h, cap)


def dims_for_ratio(
    source_width: int, source_height: int, *, tier: str = "Standard",
    device: str = "local", family: str = "",
) -> tuple[int, int]:
    """Resolve model-safe dimensions while retaining an uploaded image ratio.

    This is deliberately separate from explicit Custom width/height. Custom is
    an exact size request (subject only to the hard side ceiling); matching an
    input is an aspect request and should still honor the selected quality
    tier's pixel budget.
    """
    key = f"{device}_{family}" if family else ""
    if key in _TIER_MP:
        tier_mp, cap = _TIER_MP[key], _MAX_SIDE.get(key, 1280)
    elif device == "local":
        tier_mp, cap = _local_limits()
    else:
        tier_mp, cap = _TIER_MP.get(device, _TIER_MP["local"]), _MAX_SIDE.get(device, 1280)
    return _dims_for_ratio(source_width, source_height, tier_mp, tier, cap)


def setup_hf_env() -> None:
    """Point HF at the project cache and authenticate downloads."""
    hf_home = str(settings.hf_home_path)
    # Assign rather than setdefault: a host-level HF_HOME must never redirect a
    # The Wizard's Brush model download outside the checkout.
    os.environ["HF_HOME"] = hf_home
    os.environ["HF_HUB_CACHE"] = str(settings.hf_hub_path)
    os.environ["HF_XET_CACHE"] = str(settings.hf_home_path / "xet")
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(settings.hf_hub_path)
    # Local-first means model fetches only.  Do not let the hub library add
    # telemetry to an explicitly requested download.
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    if settings.hf_token:
        os.environ["HF_TOKEN"] = settings.hf_token
        os.environ.setdefault("HUGGING_FACE_HUB_TOKEN", settings.hf_token)


# ---- long-load reporting --------------------------------------------------
class LoadReporter:
    """Reports named stages during a model load, with a heartbeat.

    A local model swap costs 60-120 seconds, during which the old code emitted
    nothing at all. The UI could not distinguish "loading a very large model"
    from "the process has wedged", and the honest-looking fix — a progress bar —
    would be a lie, because nothing in the load path knows how far along it is.

    So: named stages for the boundaries we actually control, and a heartbeat that
    re-sends the current stage every couple of seconds. The message does not
    change; the arrival does. That is enough for a UI to animate a spinner while
    events keep arriving and warn when they stop, which is the real question the
    user is asking.

    The heartbeat runs on its own daemon thread and emits *directly*, never
    through a job's progress callback. Progress callbacks raise CancelledJob to
    stop a running job, and raising that from a timer thread would surface in
    the wrong stack with nothing to catch it.
    """

    def __init__(self, emit: Callable[[str, str, dict], None] | None,
                 interval: float = 2.0) -> None:
        self._emit = emit
        self._interval = interval
        self._stage = ""
        self._detail = ""
        self._extra: dict = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # The stage that means "nothing is loading any more". Reported once, then the
    # heartbeat goes quiet: `generate()` holds this reporter open for the whole
    # generation, so a beat that kept firing after the load finished would send a
    # model-load event every two seconds for the entire job — and those events
    # carry no `status`, so the hub treats them as must-deliver state transitions
    # and they would evict droppable preview frames under backlog.
    TERMINAL_STAGE = "ready"

    def stage(self, name: str, detail: str = "", **extra: object) -> None:
        with self._lock:
            self._stage, self._detail, self._extra = name, detail, dict(extra)
        self._send(beat=False)
        if name == self.TERMINAL_STAGE:
            with self._lock:
                self._stage = ""  # silence the heartbeat; the load is over

    def _send(self, *, beat: bool) -> None:
        if self._emit is None:
            return
        with self._lock:
            stage, detail, extra = self._stage, self._detail, dict(self._extra)
        if not stage:
            return
        with contextlib.suppress(Exception):  # telemetry must never break a load
            self._emit(stage, detail, {**extra, "beat": beat})

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._send(beat=True)

    def __enter__(self) -> LoadReporter:
        if self._emit is not None:
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="model-load-heartbeat")
            self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 0.5)
        self._thread = None


def batch_for(width: int, height: int, *, device: str = "local",
              requested: int = 1) -> int:
    """Clamp output count, never by an imaginary simultaneous-memory budget.

    Every image handler loops over single-image pipeline calls, and the Remote GPU
    endpoint does the same. Resolution changes runtime, not peak batch memory.
    The width/height/device parameters remain for API compatibility with stored
    call sites; a batch is simply up to eight serial outputs.
    """
    del width, height
    hard_cap = 8
    if device == "local":
        try:
            from ..hardware import active

            hard_cap = active().max_batch
        except Exception:  # noqa: BLE001 — count clamping must remain total
            pass
    return max(1, min(int(requested), hard_cap))
