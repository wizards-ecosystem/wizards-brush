"""Local image generator for a 16 GB-class GPU — model-agnostic.

Loads whatever model id it's asked for — the `model_variant` control picks one
from the catalogue in `variants.py` (Z-Image Turbo/base, Chroma, FLUX.1-dev) — by
resolving its pipeline class from the repo's model_index.json, quantizing to
4-bit at the pipeline level
(bitsandbytes) and enabling CPU offload so a multi-billion-param DiT fits in 16 GB.
Img2img/inpaint reuse the same loaded components. Only ONE model is resident at a
time — switching variants drops the old one first (a swap costs a full reload).

Heavy imports are lazy so the web process starts instantly.
"""
from __future__ import annotations

import contextlib
import gc
import inspect
import json
import math
import threading
from collections.abc import Callable
from typing import Any, NamedTuple

from huggingface_hub import hf_hub_download
from PIL import Image

from .. import log
from ..config import settings
from ..errors import is_oom, raise_non_oom
from ..model_sources import revision_for
from ..modelprobe import short_name
from ..utils.io import fit_image
from . import offload
from .base import GPU_LOCK, LoadReporter, res_for, setup_hf_env

logger = log.get("local_image")

# model id -> {"components": ..., "classes": ..., "how": ...}. One entry max.
_STATE: dict[str, dict] = {}
# Guards the lazy load so a startup warm-up and a first user request can't both
# trigger a (very expensive) double load.
_LOAD_LOCK = threading.Lock()
# Cache resolution and blob writes do not use CUDA. Keep their serialization
# separate so a first-run transfer never occupies the global device mutex.
_DOWNLOAD_LOCK = threading.Lock()


class _BasePrefetchRequired(RuntimeError):
    """Nunchaku could not materialize; fetch the ordinary transformer off-lock."""


def _release_to_os() -> None:
    """Hand freed heap pages back to the kernel.

    `gc.collect()` makes Python drop the objects, but glibc keeps the arenas —
    so after evicting a 16 GB model the process RSS barely moves and
    MemAvailable does not recover. Anything that then *measures* free memory to
    decide whether a load fits reads the memory we just released as still taken,
    and refuses a load that would have fitted comfortably.
    """
    import ctypes

    with contextlib.suppress(Exception):        # not glibc, or no malloc_trim
        ctypes.CDLL("libc.so.6").malloc_trim(0)


def _free() -> None:
    import torch

    gc.collect()
    if torch.cuda.is_available():
        # empty_cache cannot return blocks still referenced by in-flight
        # kernels. Every upstream allocator cleaner synchronizes first for this
        # reason; without it this function frees less than its name promises.
        with contextlib.suppress(Exception):
            torch.cuda.synchronize()
        torch.cuda.empty_cache()
    _release_to_os()


def _should_empty_cuda_cache(reserved: int, active: int, driver_free: int) -> bool:
    """Whether reclaimable allocator blocks are a meaningful share of capacity."""
    cached = max(0, int(reserved) - int(active))
    reclaimable_free = max(0, int(driver_free)) + cached
    return cached > 0 and cached * 4 > reclaimable_free


def _soft_free_if_pressure() -> None:
    """Return an unusually large CUDA cache without walking the Python heap.

    Normal generations should reuse allocator blocks; a full gc/empty/trim on
    every item makes the next item allocate them all again. Only return cached
    blocks when they exceed 25% of the genuinely reclaimable free memory.
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return
        stats = torch.cuda.memory_stats()
        reserved = int(stats.get("reserved_bytes.all.current", 0))
        active = int(stats.get("active_bytes.all.current", 0))
        driver_free, _total = torch.cuda.mem_get_info()
        if _should_empty_cuda_cache(reserved, active, int(driver_free)):
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 — allocator telemetry is best effort
        return


def resolve_model(variant: str | None) -> str:
    """Map a UI variant name to a model id. See `variants.py` for the catalogue."""
    from . import variants

    return variants.resolve(variant)


def free_all() -> None:
    """Drop every resident pipeline and release the GPU allocator's cache.

    Called after an out-of-memory failure. Without it the next job inherits a
    fragmented allocator and a model still holding VRAM, so it fails too and the
    problem looks like the app rather than the one oversized request.
    """
    # A remote-lane error used to clear this dict between a local `_get()` and
    # `_apply_loras()`.  The latter recreated an empty stub with setdefault(),
    # and every later job then failed on `entry["components"]` until restart.
    # The shared device lock makes state eviction atomic with local generation;
    # it is an RLock so the local OOM path can safely enter it again.
    with GPU_LOCK:
        _STATE.clear()
        _CONTROLNET.clear()
        _free()
    logger.info("released resident pipelines after a memory failure")


def resident_model() -> str | None:
    """The model id currently loaded on the GPU, or None if nothing is.

    `_STATE` holds at most one entry by construction — `_get()` clears it before
    loading a different model — so this is the whole answer, not a sample of it.

    Read without the load lock on purpose. The scheduler uses this to decide
    which queued job to run next, and blocking every dequeue behind a lock held
    across a 90-second model load would be far worse than acting on a value that
    is a moment stale. A wrong answer costs one avoidable swap; a held lock
    costs the whole queue.
    """
    return next(iter(_STATE), None)


def resolve_classes(model: str, *, local_files_only: bool = False):
    """Return (base, img2img, inpaint) pipeline classes for the model by reading
    its declared _class_name and substituting the task variants.

    Public because `quant.preflight` runs it in a subprocess: a kernel mismatch
    SIGABRTs the process, so the check happens somewhere expendable. Reaching
    across for a private name would have made that a hidden coupling between two
    modules that already depend on each other.
    """
    import diffusers

    with open(
        hf_hub_download(
            model,
            "model_index.json",
            token=settings.hf_token or None,
            cache_dir=settings.hf_hub_path,
            local_files_only=local_files_only,
            revision=revision_for(model),
        )
    ) as fh:
        idx = json.load(fh)
    base_name = idx.get("_class_name", "DiffusionPipeline")
    base = getattr(diffusers, base_name)
    img2img = getattr(diffusers, base_name.replace("Pipeline", "Img2ImgPipeline"), None)
    inpaint = getattr(diffusers, base_name.replace("Pipeline", "InpaintPipeline"), None)
    return base, img2img, inpaint


def _is_missing_checkpoint(e: BaseException) -> bool:
    """Whether a nunchaku load failed because no SVDQuant checkpoint exists.

    That is a legitimate reason to fall back — the model simply has not been
    published in that format — and is distinct from the wheel being wrong, which
    is a setup bug the user needs to see.
    """
    msg = str(e).lower()
    return ("404" in msg or "not found" in msg or "does not appear to have"
            in msg or "no published nunchaku checkpoint" in msg)


# Being short of memory is a moment, not a verdict. The lane is serialized, so
# whatever is holding the RAM is on its way out; giving it a little time is the
# difference between a queue that drains and a queue that throws away work the
# user asked for. Six tries at five seconds covers an eviction settling.
_RAM_WAIT_TRIES = 6
_RAM_WAIT_SECONDS = 5.0


def _await_host_ram(model: str, quant: str, rep: LoadReporter) -> None:
    """Block until the host can back this load. Raise only if it never can.

    Failing the job on the first look was wrong: a model swap frees gigabytes a
    moment later, and a job that is merely early would be marked failed forever
    for a condition that had already passed. So this retries, releasing memory
    between attempts, and reports the wait so it is visible rather than looking
    like a hang.

    It still gives up eventually. Waiting without limit on memory that is never
    coming back is a stalled queue with no explanation, which is worse than a
    job that failed for a stated reason.
    """
    import time

    from . import vram

    for attempt in range(_RAM_WAIT_TRIES):
        short = vram.host_ram_shortfall(model, quant)
        if short is None:
            return
        need, free = short
        if attempt == 0:
            logger.info("%s needs ~%.1f GB and %.1f GB is free — waiting for memory",
                        short_name(model), need / 1024 ** 3, free / 1024 ** 3)
        rep.stage("waiting", f"for system memory ({free / 1024 ** 3:.1f} of "
                             f"{need / 1024 ** 3:.1f} GB free)")
        _free()                                  # includes malloc_trim
        time.sleep(_RAM_WAIT_SECONDS)

    need, free = vram.host_ram_shortfall(model, quant) or (0, 0)
    raise RuntimeError(
        f"{short_name(model)} needs about {need / 1024 ** 3:.1f} GB of system RAM to load "
        f"with {quant} and only {free / 1024 ** 3:.1f} GB came free after waiting "
        f"{int(_RAM_WAIT_TRIES * _RAM_WAIT_SECONDS)}s. Close what else is running, pick a "
        f"smaller model, or give the VM more memory.")


def _assert_download_space(model: str, rep: LoadReporter) -> None:
    """Refuse a cold model fetch before torch, the Hub, or a file is opened."""
    from . import vram

    shortfall = vram.model_download_shortfall(model)
    if shortfall is None:
        return
    required, free = shortfall
    reserve = vram.DISK_RESERVE_BYTES
    rep.stage("resolving", f"checking free disk for {short_name(model)}")
    raise RuntimeError(
        f"Not enough free disk to start downloading {short_name(model)}: "
        f"{free / 1024 ** 3:.1f} GB is free, but at least "
        f"{(required - reserve) / 1024 ** 3:.0f} GB plus a "
        f"{reserve / 1024 ** 3:.0f} GB safety reserve is required. "
        "Free space in the Hugging Face cache volume, then retry.")


def _prefetch_model(model: str, rep: LoadReporter, *, force_base: bool = False) -> bool:
    """Cache exactly the files the planned pipeline load will consume.

    This runs before ``GPU_LOCK``. ``DiffusionPipeline.download`` applies the
    same component filtering as ``from_pretrained``; using ``snapshot_download``
    directly would fetch every precision and format in some 30+ GB repositories.

    Returns True when a configured Nunchaku load must use the ordinary 4-bit
    transformer instead (missing checkpoint, or a prior materialization OOM).
    """
    if _complete_entry(_STATE.get(model)):
        return force_base
    with _DOWNLOAD_LOCK:
        if _complete_entry(_STATE.get(model)):
            return force_base

        from . import quant as q
        from . import variants

        if ((variant := variants.by_repo(model))
                and (reason := variants.access_reason(variant.name))):
            raise RuntimeError(reason)
        setup_hf_env()
        _assert_download_space(model, rep)
        base_cls, _img2img, _inpaint = resolve_classes(model)
        resolved_quant = q.resolve_backend(model)
        use_base = force_base and resolved_quant == "nunchaku"
        passed_components: dict[str, object] = {}

        if resolved_quant == "nunchaku" and not use_base:
            rep.stage("downloading", f"SVDQuant transformer for {short_name(model)}",
                      hint="first use of a model downloads several GB")
            try:
                q.download_transformer(model)
            except Exception as e:
                if not _is_missing_checkpoint(e):
                    raise
                logger.warning("no Nunchaku checkpoint for %s; prefetching 4-bit base", model)
                use_base = True
            else:
                # Tells Diffusers not to fetch the repository's full transformer:
                # Nunchaku supplies that component from its separate checkpoint.
                passed_components["transformer"] = object()

        rep.stage("downloading", f"pipeline files for {short_name(model)}",
                  hint="first use of a model downloads several GB")
        base_cls.download(
            model,
            cache_dir=str(settings.hf_hub_path),
            token=settings.hf_token or None,
            revision=revision_for(model),
            **_pipeline_policy(base_cls),
            **passed_components,
        )
        return use_base


@contextlib.contextmanager
def _model_locked(model: str, rep: LoadReporter):
    """Yield cached components while holding the device, never during download."""
    force_base = False
    while True:
        force_base = _prefetch_model(model, rep, force_base=force_base)
        GPU_LOCK.acquire()
        try:
            components, classes = _get(
                model,
                rep,
                local_files_only=True,
                force_base=force_base,
            )
        except _BasePrefetchRequired:
            GPU_LOCK.release()
            force_base = True
            continue
        try:
            yield components, classes
        finally:
            GPU_LOCK.release()
        return


def _load_base(
    model: str,
    report: LoadReporter | None = None,
    *,
    local_files_only: bool = False,
    force_base: bool = False,
) -> tuple[dict, tuple]:
    from . import variants

    if (variant := variants.by_repo(model)) and (reason := variants.access_reason(variant.name)):
        raise RuntimeError(reason)
    setup_hf_env()
    rep = report or LoadReporter(None)
    rep.stage("resolving", model)
    _assert_download_space(model, rep)

    import torch

    from . import quant as q

    resolved_quant = q.resolve_backend(model)
    quant = "4bit" if force_base and resolved_quant == "nunchaku" else resolved_quant

    # Before anything is read from disk. Quantizing shrinks what ends up on the
    # card, not what streams through host RAM on the way there — a 4-bit load of
    # a 12B model still peaked at 16.4 GB here and took the swap file with it.
    # `from_pretrained` does not raise when the host runs out; the kernel OOM
    # killer fires and, under WSL, takes the VM down. So the question has to be
    # asked before the load, not caught after it.
    _await_host_ram(model, quant, rep)
    base_cls, img2img_cls, inpaint_cls = resolve_classes(
        model, local_files_only=local_files_only)
    how = ""
    rep.stage("downloading", f"{model} ({quant})",
              hint="first use of a model downloads several GB")

    pipe = None
    # SVDQuant: load the pre-quantized transformer separately and hand it to the
    # pipeline, with the text encoder on bitsandbytes NF4 so the pair fits
    # resident. This is the path that lets us skip CPU offload entirely.
    if quant == "nunchaku":
        try:
            rep.stage("quantizing", "SVDQuant transformer")
            transformer = q.load_transformer(model, local_files_only=local_files_only)
            pipe = base_cls.from_pretrained(
                model, transformer=transformer, torch_dtype=torch.bfloat16,
                quantization_config=q.text_encoder_config(),
                cache_dir=str(settings.hf_hub_path),
                local_files_only=local_files_only,
                revision=revision_for(model),
                **_pipeline_policy(base_cls),
            )
            how = f"nunchaku-{q.precision()}"
        except Exception as e:
            # Nunchaku has two distinct failure modes and only one is a reason
            # to degrade: the card being too small. A missing checkpoint, a bad
            # model id or a wheel built for the wrong torch are all bugs, and
            # falling back on them would hide the cause behind slower output.
            if not is_oom(e) and not _is_missing_checkpoint(e):
                raise
            logger.warning("nunchaku unavailable (%s); falling back to 4bit.", e)
            # The ordinary transformer was deliberately omitted from the
            # prefetch. Release any partially materialized CUDA module, leave
            # the device mutex, fetch that component, then re-enter cache-only.
            if "transformer" in locals():
                del transformer
            _free()
            if local_files_only:
                raise _BasePrefetchRequired(str(e)) from e
            pipe, quant = None, "4bit"

    if pipe is None and quant in ("4bit", "fp8"):
        try:
            rep.stage("quantizing", f"bitsandbytes {quant}")
            from diffusers.quantizers import PipelineQuantizationConfig

            backend = "bitsandbytes_4bit" if quant == "4bit" else "bitsandbytes_8bit"
            kwargs = (
                {"load_in_4bit": True, "bnb_4bit_quant_type": "nf4", "bnb_4bit_compute_dtype": torch.bfloat16}
                if quant == "4bit" else {"load_in_8bit": True}
            )
            qc = PipelineQuantizationConfig(
                quant_backend=backend, quant_kwargs=kwargs,  # type: ignore[arg-type]
                components_to_quantize=_quant_components(model),
            )
            pipe = base_cls.from_pretrained(
                model,
                torch_dtype=torch.bfloat16,
                quantization_config=qc,
                cache_dir=str(settings.hf_hub_path),
                local_files_only=local_files_only,
                revision=revision_for(model),
                **_pipeline_policy(base_cls),
            )
            how = quant
        except Exception as e:  # noqa: BLE001 — narrowed by raise_non_oom below
            raise_non_oom(e)
            logger.warning("%s needs more memory than is free (%s); "
                           "falling back to bf16+offload.", quant, e)
            pipe = None

    if pipe is None:
        # The last resort, and the one that can take the whole machine with it.
        # Full-precision weights land in HOST RAM before anything reaches the
        # GPU, and with LOCAL_OFFLOAD they stay there for the entire session.
        # When the host cannot back that, `from_pretrained` does not raise: the
        # kernel OOM killer fires, takes the process, and on WSL the VM with it.
        # That is how a recoverable CUDA OOM turned into a repeated hard crash.
        # Refuse with something the user can act on instead.
        # Re-checked at bf16's own (much higher) requirement: we may have got
        # here because a quantized load failed, and bf16 needs roughly twice
        # what 4-bit did. Same wait-then-give-up as above.
        _await_host_ram(model, "bf16", rep)
        rep.stage("loading", "bf16 weights")
        pipe = base_cls.from_pretrained(
            model,
            torch_dtype=torch.bfloat16,
            cache_dir=str(settings.hf_hub_path),
            local_files_only=local_files_only,
            revision=revision_for(model),
            **_pipeline_policy(base_cls),
        )
        how = how or "bf16"

    # Nunchaku's transformer raises on offload, and does not need it: an INT4
    # transformer plus an NF4 text encoder sits comfortably inside 16 GB, and
    # avoiding the CPU<->GPU shuffle is where most of the speedup comes from.
    rep.stage("placing", "moving weights to the GPU")
    if quant == "nunchaku":
        if settings.local_offload:
            logger.info("LOCAL_OFFLOAD ignored: nunchaku keeps the transformer resident.")
        pipe.to("cuda")
    elif settings.local_offload:
        pipe.enable_model_cpu_offload()
        how += "+offload"
    else:
        pipe.to("cuda")

    # First-block cache: worthwhile at >=20 steps (the quality variant); at the
    # distilled Turbo's 6-9 steps it degrades output for ~no win, so it's opt-in.
    if settings.fbcache_threshold > 0:
        try:
            from diffusers.hooks import FirstBlockCacheConfig

            pipe.transformer.enable_cache(FirstBlockCacheConfig(threshold=settings.fbcache_threshold))
            how += "+fbcache"
        except Exception as e:  # noqa: BLE001 — cache is a bonus, never fatal
            logger.warning("fbcache skipped: %s", e)

    # Slicing (one batch item at a time) is always worth it: it costs nothing and
    # removes batch size from the decode's peak. Tiling is decided per generation
    # instead — see `_set_vae_tiling` — because it costs quality at the seams and
    # is only needed when a full-frame decode genuinely will not fit.
    if hasattr(pipe, "vae"):
        with contextlib.suppress(Exception):  # optional, never fatal
            pipe.vae.enable_slicing()
        # channels-last helps the conv-heavy VAE; harmless if unsupported
        with contextlib.suppress(Exception):
            pipe.vae.to(memory_format=torch.channels_last)

    rep.stage("ready", f"{model} [{how}]")
    logger.info("loaded %s [%s] (%s)", model, how, base_cls.__name__)
    return dict(pipe.components), (base_cls, img2img_cls, inpaint_cls)


def _get(
    model: str,
    report: LoadReporter | None = None,
    *,
    local_files_only: bool = False,
    force_base: bool = False,
) -> tuple[dict, tuple]:
    # Double-checked under the load lock: the fast path skips locking once loaded.
    if not _complete_entry(_STATE.get(model)):
        with _LOAD_LOCK:
            if not _complete_entry(_STATE.get(model)):
                old = next(iter(_STATE), None)
                if old is not None:  # a different model is resident — drop it first
                    logger.info("swapping local model %s → %s", old, model)
                    if report is not None:
                        report.stage("swapping", f"{short_name(old)} → {short_name(model)}")
                    _STATE.clear()
                    # The optional ControlNet is tied to the Turbo components.
                    # Keeping it resident after a switch to SDXL/Flux wastes
                    # VRAM on a model with which it can never be used.
                    _CONTROLNET.clear()
                _free()
                comp, classes = _load_base(
                    model,
                    report,
                    local_files_only=local_files_only,
                    force_base=force_base,
                )
                _STATE[model] = {"components": comp, "classes": classes}
    entry = _STATE[model]
    return entry["components"], entry["classes"]


def _complete_entry(entry: dict | None) -> bool:
    """Whether a resident-cache row is usable, not merely present."""
    return bool(entry and "components" in entry and "classes" in entry)


def _pipeline_policy(cls: type) -> dict[str, object]:
    """Keep model safety defaults while making watermark behavior deterministic.

    A repository-provided safety component must remain enabled. The optional
    invisible watermarker is disabled only when the pipeline exposes that
    constructor argument, keeping identical seeds byte-stable across installs.
    """
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        return {}
    return {"add_watermarker": False} if "add_watermarker" in params else {}


def _quant_components(model: str) -> list[str]:
    """Architecture-specific component names understood by Diffusers."""
    from ..modelprobe import family_of_model

    if family_of_model(model) == "sdxl":
        return ["unet", "text_encoder_2"]
    return ["transformer", "text_encoder"]


def _supported_call_kwargs(pipe: Any, values: dict[str, Any]) -> dict[str, Any]:
    """Do not send a model-specific keyword to a pipeline that lacks it."""
    try:
        parameters = inspect.signature(pipe.__call__).parameters
    except (TypeError, ValueError):
        return values
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return values
    return {name: value for name, value in values.items() if name in parameters}


def _cfg_call_kwargs(pipe: Any, guidance: float, negative_prompt: str) -> dict[str, Any]:
    """Wire CFG without silently discarding Flux-family negative prompts.

    Recent Flux pipelines have two distinct controls: ``guidance_scale`` is the
    model's distilled guidance embedding, while ``true_cfg_scale`` enables the
    conditional/unconditional branch that actually encodes ``negative_prompt``.
    Keep the former tuned value and add the latter only when the explicit
    pipeline signature supports it. Conventional pipelines receive the one
    guidance keyword they have always used.
    """
    values: dict[str, Any] = {"guidance_scale": guidance}
    if not negative_prompt:
        return values
    try:
        parameters = inspect.signature(pipe.__call__).parameters
    except (TypeError, ValueError):
        return values
    if "true_cfg_scale" in parameters:
        values["true_cfg_scale"] = guidance
    return values


class LoraResult(NamedTuple):
    """What actually happened when adapters were applied.

    `skipped` is the point of this type. Every failure below is per-file and
    non-fatal — a missing adapter, a corrupt one, a set the pipeline refuses —
    and the job still produces images. Swallowing that into a log line means the
    user gets output that quietly ignored the style they asked for, with nothing
    on screen to explain why it looks wrong. The list travels out so the caller
    can say so.
    """

    desc: str
    skipped: list[str]


def _lora_activation_weights(
    pipe: Any,
    names: list[str],
    weights: list[float],
    text_weights: list[float],
    target_family: str,
) -> list[Any]:
    """Per-component SDXL weights; ordinary adapters keep the scalar path."""
    if target_family != "sdxl" or weights == text_weights:
        return list(weights)
    try:
        listed = pipe.get_list_adapters()
    except (AttributeError, TypeError):
        listed = {}
    modules = list(getattr(
        pipe, "_lora_loadable_modules", ("unet", "text_encoder", "text_encoder_2")))
    out: list[Any] = []
    for name, weight, text_weight in zip(names, weights, text_weights, strict=True):
        present = [
            module for module, adapters in listed.items()
            if isinstance(adapters, (list, tuple, set)) and name in adapters
        ]
        components = present or modules
        out.append({
            component: text_weight if component.startswith("text_encoder") else weight
            for component in components
        })
    return out


def _apply_loras(pipe, model: str, loras: list[dict]) -> LoraResult:
    """Activate `loras` on the resident pipeline. Returns a description for the
    log plus the adapters that were requested and did not apply.

    Adapters attach to the shared transformer that `_pipe_for` hands out, so
    they persist between generations and MUST be reset every call — otherwise
    the next prompt silently inherits the previous one's style. Loaded files are
    cached per resident model; a model swap clears _STATE and takes them with it.
    """
    from .. import loras as lora_lib
    from ..modelprobe import compatible, family_of_model
    from . import loraconv, variants

    entry = _STATE.get(model)
    if entry is None or not _complete_entry(entry):
        raise RuntimeError("resident model state vanished before LoRAs could be applied")
    loaded: dict[str, tuple[str, int, int]] = entry.setdefault("loras", {})
    target_family = family_of_model(model)

    prepared = []
    for item in loras:
        rel = item["path"]
        path = lora_lib.resolve_path(rel)
        signature: tuple[str, int, int]
        if path is None:
            signature = (rel, -1, -1)
        else:
            try:
                stat = path.stat()
                signature = (rel, stat.st_mtime_ns, stat.st_size)
            except OSError:
                path = None
                signature = (rel, -1, -1)
        prepared.append((item, path, signature))

    # What is currently active on the shared transformer, recorded on the state
    # entry rather than inferred. A batch of eight, or a grid sweeping seeds,
    # requests the same adapter set every item; without this we disable, re-set
    # and re-enable on all of them. Storing applied state makes the repeat a
    # no-op, which is SD.Next's `applied_tome` pattern.
    wanted = tuple((signature, float(item["weight"]),
                    float(item.get("te_weight", item["weight"])))
                   for item, _path, signature in prepared)
    if entry.get("applied_loras") == wanted:
        # The skipped list is cached with the desc on purpose: a batch of eight
        # applies once and reuses the result seven times, so a warning that
        # lived only on the first pass would be reported for one image out of
        # eight that all equally ignored the adapter.
        return LoraResult(entry.get("applied_loras_desc", ""),
                          list(entry.get("applied_loras_skipped", [])))

    # Anything else means the set changed: start from a clean slate. Adapters
    # attach to the shared transformer that `_pipe_for` hands out, so a stale one
    # would silently style the next prompt.
    if loaded:
        try:
            pipe.disable_lora()
        except Exception as e:
            # Continuing would let the prior prompt's style contaminate the
            # new one. Fail this item and let the queue evict the poisoned
            # resident pipeline instead of silently returning the wrong image.
            entry["applied_loras"] = None
            raise RuntimeError(f"could not disable prior LoRAs: {_why(e)}") from e

    # Then drop the adapters that are not wanted any more. A registered adapter
    # keeps its weights on the transformer, so leaving every LoRA ever selected
    # attached leaks VRAM for the rest of the session.
    #
    # Two things this must not do, both of which a single blanket
    # `delete_adapters(list(loaded))` inside the suppress() above did:
    #   - share a suppress() with `disable_lora()`, because then a raising
    #     delete skips the disable and a stale adapter silently styles the next
    #     prompt, which is the exact failure this reset exists to prevent;
    #   - drop `loaded` bookkeeping for an adapter that is still attached, or
    #     keep it for one that is gone. Either way the reload below is skipped
    #     and `set_adapters` is handed a name the pipeline does not have — which
    #     is what re-running the same LoRA at a different weight does.
    # So: per adapter, and `loaded` follows only a delete that actually worked.
    keep = {lora_lib.adapter_name(item["path"]) for item in loras}
    for name in [n for n in loaded if n not in keep]:
        if not hasattr(pipe, "delete_adapters"):
            break
        try:
            pipe.delete_adapters([name])
        except Exception:  # an undeletable adapter is inert, not fatal
            logger.debug("could not drop stale adapter %s", name, exc_info=True)
            continue
        loaded.pop(name, None)
    entry["applied_loras"] = None
    if not loras:
        entry["applied_loras"] = wanted
        entry["applied_loras_desc"] = ""
        entry["applied_loras_skipped"] = []
        return LoraResult("", [])

    names, weights, text_weights, skipped = [], [], [], []
    for item, path, signature in prepared:
        rel, weight = item["path"], float(item["weight"])
        text_weight = float(item.get("te_weight", weight))
        name = lora_lib.adapter_name(rel)
        if path is None:
            logger.warning("LoRA %r vanished since it was queued — skipping", rel)
            skipped.append(f"{_lora_label(rel)} (file is gone)")
            continue
        # Two different questions, and the family one cannot answer the second.
        # Family says "this is a Z-Image adapter"; it cannot say WHICH Z-Image,
        # and Turbo and base are different checkpoints whose tensors line up
        # exactly. An adapter trained on the distilled Turbo, loaded into base
        # and amplified by its CFG 4.0, renders near-black or as noise — with no
        # error anywhere, because nothing was structurally wrong.
        pins = lora_lib.pinned_variants(path)
        if pins and (this := variants.name_of_repo(model)) and this not in pins:
            logger.warning("skipping LoRA %r: pinned to %s, this model is %r",
                           rel, "/".join(sorted(pins)), this)
            skipped.append(f"{_lora_label(rel)} (for the {'/'.join(sorted(pins))} model)")
            continue
        try:
            probe = lora_lib.probe_cached(path)
            if not compatible(probe.family, target_family):
                logger.warning("skipping incompatible LoRA %r for model family %s (%s)",
                               rel, target_family, probe.family)
                skipped.append(f"{_lora_label(rel)} (for {probe.family} model)")
                continue
        except Exception:  # noqa: BLE001
            pass
        if loaded.get(name) != signature:
            try:
                # Never let a historical queued job or a local file swap route a
                # pickle-backed adapter into diffusers' in-process loader.
                if path.suffix.lower() != ".safetensors":
                    logger.warning("skipping unsafe non-safetensors LoRA %r", rel)
                    skipped.append(f"{_lora_label(rel)} (only safetensors is accepted)")
                    continue
                # Drop a stale adapter registered under the same name first.
                if name in loaded:
                    try:
                        pipe.delete_adapters([name])
                    except Exception as e:  # noqa: BLE001 — one stale adapter is per-file
                        logger.warning("LoRA %r changed but its old weights could not be "
                                       "removed (%s) — skipping", rel, e)
                        skipped.append(f"{_lora_label(rel)} (updated file could not replace old weights)")
                        continue
                    loaded.pop(name, None)
                # Adapters that diffusers accepts as-is keep taking the file
                # path; only the ones it would reject get rewritten first.
                fixed = loraconv.state_dict_for(path)
                if fixed is None:
                    pipe.load_lora_weights(str(path.parent), weight_name=path.name,
                                           adapter_name=name)
                else:
                    pipe.load_lora_weights(fixed, adapter_name=name)
                loaded[name] = signature
            except Exception as e:  # noqa: BLE001 — one bad file must not fail the job
                logger.warning("LoRA %r failed to load (%s) — skipping", rel, e)
                skipped.append(f"{_lora_label(rel)} ({_why(e)})")
                continue
        names.append(name)
        weights.append(weight)
        text_weights.append(text_weight)

    if not names:
        # Nothing loaded, but the request is still fully described by `wanted`,
        # so cache it: retrying the same broken set on every image of a batch
        # would re-read every missing file eight times to reach the same answer.
        entry["applied_loras"] = wanted
        entry["applied_loras_desc"] = ""
        entry["applied_loras_skipped"] = skipped
        return LoraResult("", skipped)
    try:
        activation_weights = _lora_activation_weights(
            pipe, names, weights, text_weights, target_family)
        pipe.set_adapters(names, adapter_weights=activation_weights)
        pipe.enable_lora()
    except Exception as e:  # noqa: BLE001
        logger.warning("could not activate LoRAs %s (%s)", names, e)
        with contextlib.suppress(Exception):
            pipe.disable_lora()
        # Leave applied state unknown so the next call re-attempts rather than
        # assuming the failed set is live.
        entry["applied_loras"] = None
        return LoraResult("", [f"{', '.join(names)} ({_why(e)})"])
    desc = ", ".join(
        f"{name}@{weight:g}"
        + (f" (text {text_weight:g})" if text_weight != weight else "")
        for name, weight, text_weight in zip(names, weights, text_weights, strict=True)
    )
    # Only recorded once the adapters are genuinely active. If some files were
    # skipped, `wanted` still describes the request, which is the right key:
    # re-requesting the same (partly broken) set should stay a no-op.
    entry["applied_loras"] = wanted
    entry["applied_loras_desc"] = desc
    entry["applied_loras_skipped"] = skipped
    return LoraResult(desc, skipped)


def _lora_label(rel: str) -> str:
    """The adapter's filename, which is what the picker showed the user."""
    return rel.rsplit("/", 1)[-1].rsplit(".", 1)[0] or rel


def _why(e: BaseException) -> str:
    """A one-line reason, short enough to sit inside a warning sentence."""
    return str(e).strip().splitlines()[0][:120] or type(e).__name__


def _warn_degenerate_output(image: Image.Image, warnings: list[str] | None) -> None:
    """Surface the common precision/adapter failure that decodes to one color."""
    if warnings is None:
        return
    try:
        extrema = image.convert("RGB").getextrema()
    except Exception:  # noqa: BLE001 — output diagnostics never fail a valid image
        return
    channels = [channel for channel in extrema if isinstance(channel, tuple)]
    if len(channels) == 3 and all(low == high for low, high in channels):
        warnings.append(
            "The model returned a single flat color. This can indicate an incompatible LoRA "
            "or a precision failure; inspect the result before reusing it.")


def _warn_prompt_truncation(
    pipe: Any, prompt: str, negative_prompt: str, warnings: list[str] | None,
) -> None:
    """Surface encoder truncation using the loaded tokenizer's real limit."""
    if warnings is None:
        return
    tokenizer = getattr(pipe, "tokenizer", None)
    limit = int(getattr(tokenizer, "model_max_length", 0) or 0)
    # Tokenizers use very large sentinels when they have no fixed limit.
    if tokenizer is None or limit <= 0 or limit > 1_000_000:
        return
    for label, text in (("Prompt", prompt), ("Negative prompt", negative_prompt)):
        if not text:
            continue
        try:
            encoded = tokenizer(text, truncation=False, add_special_tokens=True)
            ids = encoded["input_ids"] if isinstance(encoded, dict) else encoded.input_ids
            if hasattr(ids, "shape"):
                length = int(ids.shape[-1])
            else:
                if ids and isinstance(ids[0], (list, tuple)):
                    ids = ids[0]
                length = len(ids)
        except Exception:  # noqa: BLE001 — a diagnostic must not break a render
            continue
        if length > limit:
            warnings.append(
                f"{label} is {length} tokens, but this model reads only {limit}; "
                "the remaining text was ignored. Shorten it or move the most important words first."
            )


def _merge_metrics(target: dict[str, float] | None, measured: dict[str, float]) -> None:
    """Keep the high-water mark when one job runs several GPU stages."""
    if target is None:
        return
    for name, value in measured.items():
        target[name] = max(float(target.get(name, 0.0)), float(value))


def warm_up() -> None:
    """Load and run one tiny forward so the first real job is genuinely warm."""
    try:
        generate(
            mode="txt2img", prompt="warm-up", steps=1, guidance=0.0, seed=0,
            width=256, height=256, model=settings.local_image_model,
            sampler="default", loras=[], warnings=[],
        )
    except Exception as e:  # noqa: BLE001 — warm-up is best-effort
        logger.warning("warm-up skipped: %s", e)


# ---- ControlNet (Z-Image Fun Union; experimental, ENABLE_CONTROLNET) -------
_CONTROLNET: dict[str, object] = {}
CONTROLNET_WEIGHT_FILE = "Z-Image-Turbo-Fun-Controlnet-Union-2.1-8steps.safetensors"


def _prefetch_controlnet(rep: LoadReporter) -> None:
    """Cache ControlNet bytes before entering the shared device section."""
    if "model" in _CONTROLNET:
        return
    with _DOWNLOAD_LOCK:
        if "model" in _CONTROLNET:
            return
        rep.stage("downloading", f"ControlNet union weights ({CONTROLNET_WEIGHT_FILE})")
        hf_hub_download(
            settings.controlnet_model,
            CONTROLNET_WEIGHT_FILE,
            token=settings.hf_token or None,
            cache_dir=settings.hf_hub_path,
            revision=revision_for(settings.controlnet_model),
        )


def _get_controlnet(report: LoadReporter | None = None, *, local_files_only: bool = False):
    if "model" not in _CONTROLNET:
        import torch
        from diffusers import ZImageControlNetModel

        rep = report or LoadReporter(None)
        rep.stage("downloading", f"ControlNet union weights ({CONTROLNET_WEIGHT_FILE})")
        path = hf_hub_download(
            settings.controlnet_model,
            CONTROLNET_WEIGHT_FILE,
            token=settings.hf_token or None,
            cache_dir=settings.hf_hub_path,
            local_files_only=local_files_only,
            revision=revision_for(settings.controlnet_model),
        )
        logger.info("loading ControlNet %s", CONTROLNET_WEIGHT_FILE)
        rep.stage("loading", "ControlNet union weights")
        _CONTROLNET["model"] = ZImageControlNetModel.from_single_file(
            path, torch_dtype=torch.bfloat16)
        rep.stage("ready", "ControlNet union weights")
    return _CONTROLNET["model"]


def generate_control(
    *, prompt: str, negative_prompt: str = "", steps: int = 8, guidance: float = 1.0,
    seed: int, width: int, height: int, control_image: Image.Image,
    control_weight: float = 0.7,
    progress_cb: Callable[..., None] | None = None,
    metrics: dict[str, float] | None = None,
    report: LoadReporter | None = None,
) -> Image.Image:
    """Txt2img guided by a control map (canny/depth/pose). Local GPU, Turbo model."""
    # Checked before anything is imported or loaded: ControlNet is incompatible
    # with CPU offload here, and the failure is not survivable. accelerate's
    # hooks move the transformer in and out while the ControlNet stays resident,
    # and the mismatch trips a CUDA illegal memory access that poisons the
    # context for the whole process — it cannot be caught, only not attempted.
    #
    # The placement was also simply inverted before: the .to("cuda") ran only
    # when offload was ON, and never when it was OFF, so ControlNet failed both
    # ways — illegal access one way, "tensors on different devices" the other.
    # It has never actually worked until now.
    rehook = settings.controlnet_offload.strip().lower() == "rehook"
    if settings.local_offload and not rehook:
        raise RuntimeError(
            "ControlNet needs LOCAL_OFFLOAD=false. With offload on, the offload "
            "hooks and the resident ControlNet disagree about device placement "
            "and CUDA aborts the process. A 4-bit base model plus ControlNet "
            "fits comfortably in 16 GB without offload. Set CONTROLNET_OFFLOAD="
            "rehook to use the experimental path that reconciles the two."
        )

    import torch
    from diffusers import ZImageControlNetPipeline

    model = settings.local_image_model
    if progress_cb:
        progress_cb(0.0, f"loading local model {model} (first run may download weights)")
    with (report or LoadReporter(None)) as rep:
        _prefetch_controlnet(rep)
        with _model_locked(model, rep) as loaded:
            comp, _classes = loaded
            cn = _get_controlnet(rep, local_files_only=True)
            pipe: Any
            pipe, how = offload.prepare_controlnet_pipeline(
                comp, cn, ZImageControlNetPipeline,
                offload=settings.local_offload and rehook)
            logger.info("controlnet pipeline: %s", how)
            gen = torch.Generator(device="cpu").manual_seed(int(seed))
            kwargs: dict = {
                "prompt": prompt, "num_inference_steps": steps, "guidance_scale": guidance,
                "width": width, "height": height, "generator": gen,
                "control_image": fit_image(control_image, width, height),
                "controlnet_conditioning_scale": float(control_weight),
                "callback_on_step_end": _callback(progress_cb, steps, width, height, model),
            }
            if negative_prompt:
                kwargs["negative_prompt"] = negative_prompt
            if progress_cb:
                progress_cb(0.0, "starting denoising")
            from . import vram

            vram.reset_peak_vram()
            try:
                out = pipe(**_supported_call_kwargs(pipe, kwargs))
            except Exception:
                _merge_metrics(metrics, vram.peak_vram())
                # The rehook path mutated the borrowed components — their accelerate
                # hooks are gone. Leaving them cached would hand the next ordinary
                # generation a pipeline that believes it is offloaded and is not, so
                # the cache is dropped and the base model reloads clean. That costs
                # a reload after each ControlNet run and is the price of correctness.
                if settings.local_offload and rehook:
                    _STATE.clear()
                _free()
                raise
            _merge_metrics(metrics, vram.peak_vram())
            if settings.local_offload and rehook:
                _STATE.clear()
            _soft_free_if_pressure()
            return out.images[0]


def _set_vae_tiling(pipe: Any, width: int, height: int) -> None:
    """Turn VAE tiling on only when the decode would not otherwise fit.

    Tiling used to be unconditional, which meant every generation paid for seam
    artefacts and extra passes to survive the largest case. The peak is
    predictable from the output area (see generators/vram.py), so it can be a
    decision instead of a default.

    Entirely best-effort: a pipeline without these methods, or a card we cannot
    query, keeps whatever it had.
    """
    vae = getattr(pipe, "vae", None)
    if vae is None:
        return
    from . import vram

    want_tiling = vram.should_tile_decode(width, height)
    method = "enable_tiling" if want_tiling else "disable_tiling"
    with contextlib.suppress(Exception):
        if want_tiling:
            # Match the estimator's exact sample-space tile. Diffusers derives
            # its latent tile from the VAE compression ratio; setting all three
            # attributes prevents a repo's arbitrary sample_size default from
            # invalidating the memory calculation.
            compression = max(1, int(getattr(pipe, "vae_scale_factor", 8) or 8))
            if hasattr(vae, "tile_sample_min_size"):
                vae.tile_sample_min_size = vram.VAE_TILE_SIZE
            if hasattr(vae, "tile_latent_min_size"):
                vae.tile_latent_min_size = max(1, vram.VAE_TILE_SIZE // compression)
            if hasattr(vae, "tile_overlap_factor"):
                vae.tile_overlap_factor = vram.VAE_TILE_OVERLAP
        getattr(vae, method)()


def _callback(progress_cb: Callable[..., None] | None, steps: int, width: int,
              height: int, model: str | None = None):
    if progress_cb is None:
        return None
    every = max(0, int(settings.preview_every))

    def cb(pipe, step, timestep, kw):
        prev = None
        if every and (step + 1) % every == 0 and (step + 1) < steps:
            # Preview conversion performs a device→host sync while GPU_LOCK is
            # held. With the UI closed there is nobody to receive the JPEG, so
            # consult the hub before paying for it. Progress itself still emits.
            from ..queue import hub

            if hub.has_subscribers:
                from . import preview as pv

                prev = pv.latents_to_jpeg(pipe, kw.get("latents"), width=width,
                                          height=height, model=model)
        progress_cb((step + 1) / max(1, steps), f"step {step + 1}/{steps}", preview=prev)
        return kw

    return cb


def effective_step_count(mode: str, steps: int, strength: float, model: str | None = None) -> int:
    """How many denoise callbacks diffusers will actually execute.

    Partial-denoise pipelines truncate the schedule by strength. SDXL floors
    `steps * strength`; the flow-family pipelines shipped here subtract an
    integer start index, which is equivalent to a ceiling. Keep this pure and
    explicit so metadata, progress, and tests share one answer.
    """
    requested = max(1, int(steps))
    if mode == "txt2img":
        return requested
    scaled = requested * max(0.0, min(float(strength), 1.0))
    from ..modelprobe import family_of_model

    if family_of_model(model or "") == "sdxl":
        return max(1, min(requested, int(scaled)))
    return max(1, min(requested, math.ceil(scaled)))


# What each mode cannot run without. Stated as data so the check and the message
# cannot drift apart.
_REQUIRED_INPUTS: dict[str, tuple[str, ...]] = {
    "txt2img": (),
    "img2img": ("image",),
    "inpaint": ("image", "mask"),
}


def _missing_message(mode: str, missing: list[str]) -> str:
    """The one place the wording lives, so the two checks below cannot diverge."""
    needs = " and ".join(_REQUIRED_INPUTS.get(mode, ())) or "no inputs"
    named = " and ".join(missing)
    was = "was" if len(missing) == 1 else "were"
    return (f"{mode} needs {needs}, but {named} {was} not attached. "
            "If this is a rerun, the original upload may have been swept.")


def _require_inputs(mode: str, image: Image.Image | None,
                    mask: Image.Image | None) -> None:
    """Refuse a request whose required inputs are missing, before anything costly.

    A real exception rather than an `assert`, for two reasons. `python -O` strips
    asserts, which would turn a missing image into an AttributeError somewhere
    inside diffusers in exactly the build where a clear message matters most. And
    an AssertionError is not something a user can act on: this names the input,
    and names the likeliest cause, which is a rerun whose original upload was
    swept by the orphan sweeper.

    Checked before the GPU lock is taken, so a bad request never waits behind a
    90-second model load only to be told it was never going to run.
    """
    have = {"image": image, "mask": mask}
    missing = [name for name in _REQUIRED_INPUTS.get(mode, ())
               if have.get(name) is None]
    if missing:
        raise ValueError(_missing_message(mode, missing))


def _need(value: Image.Image | None, what: str, mode: str) -> Image.Image:
    """The same guarantee at the point of use, where the type checker can see it.

    `_require_inputs` has already run, so this never fires. It exists because a
    guarantee established thirty lines earlier is invisible to mypy, and the
    honest way to close that gap is to check again rather than to cast or assert
    the checker into silence. The message comes from the same function, so the
    two can never disagree.
    """
    if value is None:
        raise ValueError(_missing_message(mode, [what]))
    return value


def generate(
    *, mode: str, prompt: str, negative_prompt: str = "", steps: int = 8, guidance: float = 1.0,
    seed: int, width: int = 0, height: int = 0, resolution: str = "720p", orientation: str = "portrait",
    image: Image.Image | None = None, mask: Image.Image | None = None, strength: float = 0.6,
    model: str | None = None, loras: list[dict] | None = None, sampler: str = "default",
    progress_cb: Callable[..., None] | None = None,
    report: LoadReporter | None = None, warnings: list[str] | None = None,
    metrics: dict[str, float] | None = None,
) -> Image.Image:
    """Run one local image generation. Serialized by the shared GPU lock.

    `report`, when given, narrates a model load — the 60-120 second stall that
    happens when this job needs a model the GPU is not currently holding.

    `warnings`, when given, collects things that went wrong without failing the
    job — an adapter that could not be applied, so far. The images still exist,
    which is exactly why this cannot be an exception, and exactly why it must
    not be only a log line either.
    """
    import torch

    _require_inputs(mode, image, mask)
    if not width or not height:
        width, height = res_for(resolution, orientation)
    model = model or settings.local_image_model

    if progress_cb:
        progress_cb(0.0, f"loading local model {model} (first run may download weights)")
    with (report or LoadReporter(None)) as rep, _model_locked(model, rep) as loaded:
        comp, (base_cls, img2img_cls, inpaint_cls) = loaded
        cls = {"txt2img": base_cls, "img2img": img2img_cls, "inpaint": inpaint_cls}[mode]
        if cls is None:
            raise RuntimeError(f"{model} has no pipeline for {mode}")
        pipe = cls(**comp)
        _warn_prompt_truncation(pipe, prompt, negative_prompt, warnings)
        # Scheduler is per-call: _pipe_for builds a fresh pipeline object each
        # time, so this cannot leak into the next generation the way an adapter
        # would. Raises on an unusable choice rather than silently ignoring it.
        from . import schedulers
        schedulers.apply(pipe, sampler, model=model)
        applied = _apply_loras(pipe, model, loras or [])
        if applied.desc:
            logger.info("LoRAs active: %s", applied.desc)
        if applied.skipped and warnings is not None:
            warnings.append("These LoRAs were not applied: "
                            + "; ".join(applied.skipped))
        gen = torch.Generator(device="cpu").manual_seed(int(seed))
        actual_steps = effective_step_count(mode, steps, strength, model)
        kwargs: dict = {
            "prompt": prompt, "num_inference_steps": steps,
            "generator": gen,
            "callback_on_step_end": _callback(progress_cb, actual_steps, width, height, model),
        }
        kwargs.update(_cfg_call_kwargs(pipe, guidance, negative_prompt))
        from . import variants

        if ((variant := variants.by_repo(model))
                and variant.cfg_truncation < 1.0 and guidance > 0):
            kwargs["cfg_truncation"] = variant.cfg_truncation
        if negative_prompt:
            kwargs["negative_prompt"] = negative_prompt
            if kwargs.get("true_cfg_scale", 2.0) <= 1.0 and warnings is not None:
                warnings.append(
                    "This model needs Guidance above 1 for a negative prompt to take effect; "
                    "the negative prompt was ignored at the selected value."
                )
        if mode == "txt2img":
            kwargs.update(width=width, height=height)
        elif mode == "img2img":
            # Cover-fit (scale + center-crop) — never stretch the input's aspect.
            kwargs.update(image=fit_image(_need(image, "image", mode), width, height),
                          strength=strength, width=width, height=height)
        elif mode == "inpaint":
            # Image and mask share dimensions, so the identical cover-fit keeps them aligned.
            kwargs.update(
                image=fit_image(_need(image, "image", mode), width, height),
                mask_image=fit_image(_need(mask, "mask", mode), width, height).convert("L"),
                strength=strength, width=width, height=height)

        if progress_cb:
            progress_cb(0.0, "starting denoising")
        from . import vram

        # Take the free-memory reading as late as the pipeline API permits,
        # after scheduler and adapter setup. The decode happens inside __call__,
        # so there is no safe hook between denoising and VAE decode.
        _set_vae_tiling(pipe, width, height)
        vram.reset_peak_vram()
        try:
            out = pipe(**_supported_call_kwargs(pipe, kwargs))
        except Exception:
            _merge_metrics(metrics, vram.peak_vram())
            # Cancellation and failed kernels may leave live temporaries. The
            # heavy path is justified here; successful items keep reusable
            # allocator blocks unless `_soft_free_if_pressure` sees real hoarding.
            _free()
            raise
        _merge_metrics(metrics, vram.peak_vram())
        _soft_free_if_pressure()
        image_out = out.images[0]
        _warn_degenerate_output(image_out, warnings)
        return image_out
