"""Shared route helpers: multipart parsing, job submission, post-processing."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps

from .. import db, log
from ..config import settings
from ..models import AssetKind
from ..prompt_engine import MAX_PROMPT_CHARS, prepare_prompt, seed_for
from ..queue import ProgressCb
from ..queue import submit as enqueue
from ..utils.io import save_image, stamp_name

logger = log.get("post")

MAX_UPLOAD_BYTES = 40 * 1024 * 1024  # generous for a local single-user app
MAX_UPLOAD_PIXELS = 64_000_000
MAX_UPLOAD_SIDE = 16_384
MAX_PROMPT = MAX_PROMPT_CHARS  # public route alias; one cap in prompt_engine
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def normalize_request_id(raw: object) -> str | None:
    """A bounded opaque client submission id, or None for legacy callers."""
    value = str(raw or "").strip()
    if not value:
        return None
    if not _REQUEST_ID.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail="request_id must be 16-128 letters, numbers, underscores, or hyphens",
        )
    return value


def parse_payload(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001 — degrade quietly; the caller must not fail here
        # Silently mapping garbage to {} would generate a default 1024² image
        # with an empty prompt instead of telling the caller what went wrong.
        raise HTTPException(status_code=400, detail="payload is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    return parsed


def as_int(v: Any, default: int, lo: int, hi: int) -> int:
    """Clamped int coercion for schemaless params — a bad value falls back to the
    default instead of 500ing the route or blowing up later in the handler."""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        n = default
    return max(lo, min(n, hi))


def as_float(v: Any, default: float, lo: float, hi: float) -> float:
    try:
        n = float(v)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(n, hi))


def snap_scale(v: Any, default: int = 4) -> int:
    """The registry offers ×2 or ×4 for upscale/interpolation — snap anything else."""
    return 2 if as_int(v, default, 1, 4) <= 2 else 4


# ---- generation helpers (shared by image/video handlers) ------------------
def item_seed_prompt(params: dict[str, Any], i: int) -> tuple[int, str]:
    """The per-batch-item (seed, expanded prompt) pair. Every image handler derives
    both the same way — keep it in one place so seed_mode / wildcard expansion can't
    drift between txt2img, img2img, ControlNet, and the Remote GPU batch path."""
    seed = seed_for(int(params.get("seed", 0) or 0), i, params.get("seed_mode", "increment"))
    prompt = prepare_prompt(
        params.get("prompt", ""), i, seed=seed,
        syntax=str(params.get("prompt_syntax", "literal")),
    )
    return seed, prompt


def item_seed_prompts(params: dict[str, Any], i: int) -> tuple[int, str, str]:
    """Positive and negative use the same documented expansion and syntax."""
    seed, prompt = item_seed_prompt(params, i)
    negative = prepare_prompt(
        params.get("negative_prompt", ""), i, seed=seed,
        syntax=str(params.get("prompt_syntax", "literal")),
    )
    return seed, prompt, negative


_DERIVATIVE_SUMMARY_KEYS = frozenset({
    "upscaled", "face_restored", "detailed", "interpolated", "extended_from",
})


def _size_record(size: tuple[int, int]) -> dict[str, int]:
    return {"width": max(0, int(size[0])), "height": max(0, int(size[1]))}


def _legacy_post_record(value: Any) -> dict[str, Any] | None:
    """Turn the pre-v1 string form into the structured, append-only form.

    Old rows used strings such as ``upscaled_x4`` and copied a tool's summary
    keys over the previous tool's. Keeping those strings readable lets a newly
    derived asset preserve the whole ancestry without a destructive DB
    migration.
    """
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("upscaled_x"):
        scale = value.removeprefix("upscaled_x")
        return {
            "operation": "upscale",
            **({"scale": int(scale)} if scale.isdigit() else {}),
        }
    names = {"detailed": "detail", "face_restored": "face_restore"}
    return {"operation": names.get(value, value)}


def derivative_meta(
    src_meta: dict[str, Any] | None,
    *,
    operations: str | list[str] | list[dict[str, Any]],
    input_size: tuple[int, int],
    output_size: tuple[int, int],
    details: dict[str, Any] | None = None,
    inherit_diagnostics: bool = False,
) -> dict[str, Any]:
    """Build portable metadata for pixels derived from an existing result.

    ``width``/``height`` always describe *this file*. The ancestor's diffusion
    size is retained separately as ``generation_width``/``generation_height``;
    :func:`metadata.parameters_text` deliberately uses those values so its
    A1111 recipe remains the ancestor's recipe. ``post`` is an append-only list
    of records, so a second upscale cannot erase the first.

    Diagnostics are local to the job that emitted them. A standalone derivative
    therefore drops inherited warnings/ignored controls, while inline finishing
    can retain diagnostics produced by the same generation job.
    """
    meta = dict(src_meta or {})
    if not inherit_diagnostics:
        meta.pop("warnings", None)
        meta.pop("ignored_params", None)
    for key in _DERIVATIVE_SUMMARY_KEYS:
        meta.pop(key, None)

    in_w, in_h = (max(0, int(input_size[0])), max(0, int(input_size[1])))
    out_w, out_h = (max(0, int(output_size[0])), max(0, int(output_size[1])))
    generation_w = int(meta.get("generation_width") or meta.get("width") or in_w)
    generation_h = int(meta.get("generation_height") or meta.get("height") or in_h)
    meta.update({
        "generation_width": generation_w,
        "generation_height": generation_h,
        "width": out_w,
        "height": out_h,
    })

    history: list[dict[str, Any]] = []
    prior = meta.pop("post", [])
    if not isinstance(prior, list):
        prior = [prior]
    for value in prior:
        if record := _legacy_post_record(value):
            history.append(record)

    requested: list[Any] = operations if isinstance(operations, list) else [operations]
    current = (in_w, in_h)
    for index, value in enumerate(requested):
        record = _legacy_post_record(value)
        if not record:
            continue
        operation = str(record.get("operation") or "postprocess")
        next_size = current
        if operation == "upscale" and record.get("scale"):
            scale = max(1, int(record["scale"]))
            next_size = (current[0] * scale, current[1] * scale)
        if index == len(requested) - 1:
            # The encoder's actual result is authoritative over an advertised
            # scale (and over rounding performed by a particular tool).
            next_size = (out_w, out_h)
        record.update({
            "input_size": _size_record(current),
            "output_size": _size_record(next_size),
        })
        if details and len(requested) == 1:
            record.update(details)
        history.append(record)
        current = next_size
    meta["post"] = history
    return meta


def image_meta(params: dict[str, Any], img: Image.Image, *, prompt: str, seed: int,
               **extra: Any) -> dict[str, Any]:
    """Common metadata for a saved image. Handlers add their own keys (mode, model,
    control_mode, remote, …) via **extra — this fixes the shared keys so they can't
    silently diverge across generators.

    An empty `warnings` list is dropped rather than recorded. "Nothing went
    wrong" is the normal case, and a key that is present and empty on every
    successful image trains people to stop reading it.
    """
    from ..models import PARAM_SCHEMA_VERSION

    meta = {
        "_v": params.get("_v", PARAM_SCHEMA_VERSION),
        "prompt": prompt,
        "negative_prompt": params.get("negative_prompt", ""),
        "seed": seed,
        "steps": params.get("steps"),
        "guidance": params.get("guidance"),
        "width": img.width,
        "height": img.height,
        "quality": params.get("quality", ""),
        "raw_prompt": params.get("raw_prompt", prompt),
        "style_ids": params.get("style_ids") or [],
        "model_variant": params.get("model_variant", ""),
        "strength": params.get("strength"),
        "sampler": params.get("sampler", ""),
        "finish": params.get("finish", ""),
        **extra,
    }
    if not meta.get("warnings"):
        meta.pop("warnings", None)
    # Inline finishing is still a derivative. Split its file dimensions from
    # the diffusion recipe in the same form used by standalone tools.
    post = meta.get("post")
    if isinstance(post, list) and post:
        input_size = (int(meta.get("width") or img.width), int(meta.get("height") or img.height))
        meta = derivative_meta(
            {k: v for k, v in meta.items() if k != "post"},
            operations=post,
            input_size=input_size,
            output_size=img.size,
            inherit_diagnostics=True,
        )
    return meta


def video_meta_base(params: dict[str, Any]) -> dict[str, Any]:
    """The render settings shared by every video meta dict (single clip + long
    stitched). Handlers add prompt/seed/model/engine/kind around it."""
    return {
        "num_frames": params["num_frames"],
        "steps": params["steps"],
        "guidance": params["guidance"],
        "fps": params["fps"],
        "quality": params["quality"],
        "resolution": params["resolution"],
        "orientation": params["orientation"],
    }


def require_upload(path: str | None, what: str = "input image") -> str:
    """400 for a registry-required image that wasn't attached — otherwise a job is
    created that can only error minutes later with a traceback."""
    if not path:
        raise HTTPException(status_code=400, detail=f"{what} required")
    return path


async def save_upload(f: UploadFile | None) -> str | None:
    if f is None:
        return None
    settings.ensure_dirs()
    data = await f.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400,
                            detail=f"upload too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    name = stamp_name(f"up_{Path(f.filename or 'img').stem}", "png")
    dest = settings.uploads_dir / name

    def _decode_and_save() -> None:
        # PIL decode + PNG re-encode are CPU-bound on payloads up to 40 MB; keep
        # them off the event loop so an upload never stalls the jobs WebSocket.
        try:
            img = load_uploaded_image(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except Exception:  # noqa: BLE001 - all decoder failures become a safe 400
            raise HTTPException(status_code=400, detail="upload is not a valid image") from None
        img.save(dest, "PNG")

    await asyncio.to_thread(_decode_and_save)
    return str(dest)


def validate_upload_dimensions(data: bytes) -> tuple[int, int]:
    """Inspect an untrusted image before a decoder can allocate its full canvas."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except Exception as exc:
        raise ValueError("upload is not a valid image") from exc
    if (width <= 0 or height <= 0 or width > MAX_UPLOAD_SIDE or height > MAX_UPLOAD_SIDE
            or width * height > MAX_UPLOAD_PIXELS):
        raise ValueError(
            f"image dimensions exceed {MAX_UPLOAD_SIDE}px per side or "
            f"{MAX_UPLOAD_PIXELS:,} pixels"
        )
    return width, height


def load_uploaded_image(data: bytes) -> Image.Image:
    validate_upload_dimensions(data)
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            return ImageOps.exif_transpose(opened).convert("RGB")
    except Exception as exc:
        raise ValueError("upload is not a valid image") from exc


# Kinds whose work runs through a resident diffusion pipeline that has to be
# swapped to serve a different model. Only these can make their lane pay a
# reload, so only these get a model_key.
#
# The A100 is here for the same reason the local card is, and more urgently: it
# holds one image model at a time and they are 35-58 GB, so an interleaved queue
# jobs alternating between its two models would reload between every pair.
_MODEL_KEYED_KINDS = {"image_local", "img2img", "inpaint", "outpaint", "control_local"}
_REMOTE_MODEL_KEYED_KINDS = {"image_colab"}


def model_key_for(kind: str, params: dict[str, Any]) -> str | None:
    """Which model this job will need, resolved once at creation.

    Returns the resolved model *id* rather than the UI variant name, because the
    id is what the pipeline caches are keyed by — comparing variants would go
    wrong the moment two variants resolve to the same model (which is exactly
    what happens when LOCAL_IMAGE_MODEL_HQ is unset).

    None means the job never makes anything reload: the remote lanes with no
    model choice, plus tool jobs like upscale and face-restore.
    """
    try:
        from ..generators import variants

        if kind in _MODEL_KEYED_KINDS:
            return variants.resolve(params.get("model_variant"), lane="local")
        if kind in _REMOTE_MODEL_KEYED_KINDS:
            return variants.resolve(params.get("model_variant"), lane="colab")
    except Exception:  # noqa: BLE001 — a key we cannot resolve costs one swap, not a job
        return None
    return None


def _submission_response(job) -> dict[str, Any]:
    """The original single/fan-out identity for a duplicate submission."""
    if job.group_id:
        siblings = db.list_jobs_by_group(job.group_id)
        ids = [int(sibling.id) for sibling in siblings if sibling.id is not None]
        return {"job_id": ids[0], "job_ids": ids, "group_id": job.group_id}
    return {"job_id": int(job.id)}


async def submit(kind: str, params: dict[str, Any], handler: Callable,
                 group_id: str | None = None, request_id: str | None = None) -> dict:
    """Create or recover one idempotent job, enqueueing only a new row."""
    params = dict(params)
    embedded_request_id = params.pop("request_id", None)
    request_id = normalize_request_id(request_id or embedded_request_id)
    job, created = db.create_job_once(
        kind, params, group_id=group_id,
        model_key=model_key_for(kind, params), request_id=request_id,
    )
    register_handler(kind, handler)  # remember how to re-run this kind
    if not created:
        return _submission_response(job)
    # Record the prompt for history/recall (no-op for tool jobs with no prompt).
    with contextlib.suppress(Exception):  # history is best-effort, never block a job
        db.add_history(kind, params.get("prompt", ""), params.get("negative_prompt", ""), params)
    assert job.id is not None  # committed row always has a pk
    await enqueue(kind, job.id, handler)
    return {"job_id": job.id}


async def submit_fanout(kind: str, base_params: dict[str, Any], handler: Callable,
                        variants: list[dict[str, Any]], request_id: str | None = None) -> dict:
    """Fan one request out into N sibling jobs sharing a group_id (combinatorial
    prompts, X/Y grids). Each child is an ordinary job — individually cancelable,
    rerunnable, and reorderable."""
    import uuid

    base_params = dict(base_params)
    request_id = normalize_request_id(
        request_id
        or base_params.pop("request_id", None)
        or (variants[0].get("request_id") if variants else None)
    )
    if request_id and (existing := db.job_for_request(request_id)) is not None:
        return _submission_response(existing)

    group_id = uuid.uuid4().hex
    job_ids: list[int] = []
    for index, variant in enumerate(variants):
        params = {**base_params, **variant, "grid_id": group_id}
        params.pop("request_id", None)  # only the first child owns the group identity
        r = await submit(
            kind, params, handler, group_id=group_id,
            request_id=request_id if index == 0 else None,
        )
        if "job_ids" in r:  # concurrent retry recovered the original group
            return r
        job_ids.append(r["job_id"])
    return {"job_id": job_ids[0], "job_ids": job_ids, "group_id": group_id}


# kind -> handler, so a stored job can be re-enqueued by id (retry / generate-more).
_HANDLERS: dict[str, Callable] = {}


def register_handler(kind: str, handler: Callable) -> None:
    _HANDLERS[kind] = handler


def get_handler(kind: str) -> Callable | None:
    return _HANDLERS.get(kind)


# ---- finishing presets ----------------------------------------------------
# The UI asks one question ("Finishing") where it used to ask four. This is the
# expansion: preset -> (detail, face, upscale).
#
# The individual post_* flags remain the wire format, so nothing downstream had
# to learn about presets and a stored job still describes exactly which steps
# ran. Upscale is deliberately absent from "faces": it is by far the most
# expensive step and quadruples the file, which is a choice, not a polish.
_FINISH_STEPS: dict[str, tuple[bool, bool, bool]] = {
    "none": (False, False, False),
    "faces": (True, True, False),
    "upscale": (False, False, True),
    "faces + upscale": (True, True, True),
}


def resolve_finish(p: dict) -> tuple[str, bool, bool, bool]:
    """(normalised finish, detail, face, upscale) for a submitted request.

    An unrecognised value — including the absent key on a job saved before this
    control existed — means "custom", and the individual post_* flags decide.
    That is what keeps a rerun of an old job identical to its first run: those
    jobs carry post_upscale/post_face and no `finish` at all, and reading them
    as the "none" preset would silently drop the steps they were queued with.
    """
    name = str(p.get("finish", "") or "").strip().lower()
    steps = _FINISH_STEPS.get(name)
    if steps is None:
        return "custom", bool(p.get("post_detail")), bool(p.get("post_face")), bool(p.get("post_upscale"))
    return name, *steps


# ---- post-processing applied inline after a generation --------------------
def apply_image_post(
    img: Image.Image, params: dict, cb: ProgressCb, *, start: float = 0.85, end: float = 1.0,
    metrics: dict[str, float] | None = None,
) -> tuple[Image.Image, list[Any]]:
    """Run optional detailer / upscale / face-restore on a freshly generated image.

    Returns (image, applied) — the names of the steps that actually ran, so the
    metadata can record what happened to it. Without that, an upscaled asset is
    indistinguishable from a natively large one, and "reuse all" reads the
    post-processed dimensions back as generation dimensions.

    Every step catches broadly (not just ToolUnavailable): the expensive part —
    the generation — already succeeded, so a post step crashing (CUDA OOM, a
    mediapipe runtime error) must degrade to the un-postprocessed image, never
    discard it. Cancel/skip still propagates via the progress callback."""
    applied: list[Any] = []
    if not (params.get("post_detail") or params.get("post_face") or params.get("post_upscale")):
        return img, applied  # common path: no post steps — skip importing the tool stack

    from ..generators import postprocess as pp
    from ..queue import CancelledJob, SkipItem

    enabled = [
        name for name, on in (
            ("detail", params.get("post_detail")),
            ("face", params.get("post_face")),
            ("upscale", params.get("post_upscale")),
        ) if on
    ]

    def emit(fraction: float, message: str, preview: bytes | None = None) -> None:
        cb(start + (end - start) * max(0.0, min(fraction, 1.0)), message, preview=preview)

    def stage(name: str) -> tuple[float, float]:
        index = enabled.index(name)
        return index / len(enabled), (index + 1) / len(enabled)

    if params.get("post_detail"):
        lo, hi = stage("detail")
        emit(lo, "auto-detailing")
        try:
            from ..generators import detailer, local_image, variants

            # Match the job's model variant — refining with the other variant
            # forces a full (minutes-long) pipeline swap per batch item.
            model = local_image.resolve_model(params.get("model_variant"))
            recipe = variants.by_repo(model)
            img, _n = detailer.refine(img, prompt=params.get("detail_prompt", ""),
                                      negative=params.get("negative_prompt", ""),
                                      steps=recipe.refine_steps if recipe else None,
                                      guidance=recipe.guidance if recipe else None,
                                      seed=int(params.get("seed", 0) or 0),
                                      model=model,
                                      metrics=metrics,
                                      progress_cb=lambda f, m, preview=None: emit(
                                          lo + (hi - lo) * f, m, preview),
                                      )
            applied.append("detailed")
        except (CancelledJob, SkipItem):
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("auto-detail skipped: %s", e)
            emit(hi, f"auto-detail skipped: {e}")
        else:
            emit(hi, "auto-detail complete")
    if params.get("post_face"):
        lo, hi = stage("face")
        emit(lo, "restoring faces")
        try:
            img = pp.restore_faces(
                img, progress_cb=lambda f, m: emit(lo + (hi - lo) * f, m))
            applied.append("face_restored")
        except (CancelledJob, SkipItem):
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("face restore skipped: %s", e)
            emit(hi, f"face restore skipped: {e}")
        else:
            emit(hi, "face restore complete")
    if params.get("post_upscale"):
        lo, hi = stage("upscale")
        scale = snap_scale(params.get("post_scale"))
        emit(lo, f"upscaling ×{scale}")
        try:
            source_size = img.size
            img = pp.upscale_image(
                img, scale=scale,
                progress_cb=lambda f, m: emit(lo + (hi - lo) * f, m),
            )
            applied.append({
                "operation": "upscale",
                "scale": scale,
                "requested_size": _size_record(
                    (source_size[0] * scale, source_size[1] * scale)
                ),
                "clamped": img.size != (source_size[0] * scale, source_size[1] * scale),
                "max_side": pp.MAX_UPSCALE_SIDE,
            })
        except (CancelledJob, SkipItem):
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("upscale skipped: %s", e)
            emit(hi, f"upscale skipped: {e}")
        else:
            emit(hi, "upscale complete")
    # `applied` only lists steps that SUCCEEDED — a step that degraded to a
    # warning must not claim credit in the metadata.
    return img, applied


def image_progress_window(params: dict, index: int, batch: int) -> tuple[float, float, float]:
    """(item start, denoise end, item end) with real room for finishing."""
    count = max(1, int(batch))
    item_start, item_end = index / count, (index + 1) / count
    has_post = bool(params.get("post_detail") or params.get("post_face") or params.get("post_upscale"))
    denoise_end = item_start + (item_end - item_start) * (0.85 if has_post else 1.0)
    return item_start, denoise_end, item_end


def _announce_asset(asset_id: int, job_id: int, kind: str) -> None:
    """Tell subscribers a finished asset exists, before the job that made it ends.

    A batch is ONE job, so a client that only refreshes on the job's terminal
    event sees nothing until the last image of eight is done — the first seven
    were already on disk and in the database, just unannounced. This is the
    missing edge.

    Deliberately carries ids only, not the asset row: the client refetches
    through the normal endpoint, so there is one serialization path for an asset
    instead of two that can disagree. Never fatal — an announcement that fails
    costs a late refresh, and the terminal job event still forces one.
    """
    try:
        from ..queue import hub

        hub.emit({"type": "asset", "id": asset_id, "job_id": job_id, "kind": kind})
    except Exception:  # a notification must never fail an asset already on disk
        logger.debug("could not announce asset %s", asset_id, exc_info=True)


def persist_image(img: Image.Image, *, job_id: int, generator: str, meta: dict, tag: str,
                  params: dict[str, Any] | None = None) -> int:
    """Save an image, record it, and queue enrichment.

    `params` is the job's parameter dict as the handler received it. Pass it and
    the saved metadata reports which controls were sent but never read; omit it
    and nothing is reported. It is a separate argument because `meta` is a
    *derived* dict built by `image_meta` — it has no read history of its own, so
    handing it to the tracker asks the wrong object what happened.
    """
    meta = _honest_meta(meta, job_id, params)
    # Embedded in the file itself, not only in the database: provenance and the
    # A1111-style parameters block travel with the image when it is exported.
    saved = save_image(img, tag, meta=meta, kind=generator)
    if saved.metadata_error:
        warning = (
            "Portable metadata could not be embedded in this PNG; its recipe remains "
            "available in The Wizard's Brush library."
        )
        current = list(meta.get("warnings") or [])
        if warning not in current:
            current.append(warning)
        meta = {**meta, "warnings": current,
                "metadata_embed_error": saved.metadata_error}
    asset = db.add_asset(AssetKind.image.value, saved.path, thumb=saved.thumb,
                         width=saved.width, height=saved.height,
                         job_id=job_id, generator=generator, meta=meta,
                         content_hash=saved.content_hash, size_bytes=saved.size_bytes,
                         mime_type="image/png")
    assert asset.id is not None
    _enrich(asset.id)
    _announce_asset(asset.id, job_id, "image")
    return asset.id


def _honest_meta(meta: dict, job_id: int, params: dict[str, Any] | None = None) -> dict:
    """Strip parameters the handler never read, and record which they were.

    Metadata that lists a setting which had no effect is worse than metadata that
    omits it: re-running from it produces something different and nothing says
    why. See app/params.py.

    The read history lives on `params` — the TrackedParams the queue handed the
    handler — not on `meta`, which `image_meta` built fresh. So the unused set is
    computed from `params`, and any key of that name is then dropped from `meta`
    too, since a value the handler never looked at did not shape this image.
    """
    from ..params import split_metadata

    _kept, ignored = split_metadata(params if params is not None else meta)
    # A remote worker can supply its own fixed adapter. That adapter is derived
    # by the handler (rather than read from the public request's loras
    # control), so it must remain provenance even when the unused-control audit
    # sees an empty request-side loras value.
    if meta.get("loras"):
        ignored = [key for key in ignored if key != "loras"]
    if not ignored:
        return meta
    dropped = set(ignored)
    kept = {k: v for k, v in meta.items() if k not in dropped}
    kept["ignored_params"] = ignored
    logger.debug("job %s: params sent but never read: %s", job_id, ignored)
    return kept


def _enrich(asset_id: int) -> None:
    """Queue async captioning; must never block or fail a save."""
    try:
        from .. import enrichment

        enrichment.enqueue(asset_id)
    except Exception:  # noqa: BLE001
        pass


def persist_video(data: bytes, *, job_id: int, generator: str, meta: dict, tag: str,
                  post_interpolate: bool, cb: ProgressCb) -> int:
    """Persist raw mp4 bytes (Remote GPU downloads arrive in memory)."""
    from ..utils.io import stamp_name

    settings.ensure_dirs()
    tmp = settings.videos_dir / f"_persist_{stamp_name(tag, 'mp4')}"
    tmp.write_bytes(data)
    return persist_video_file(tmp, job_id=job_id, generator=generator, meta=meta, tag=tag,
                              post_interpolate=post_interpolate, cb=cb)


def persist_video_file(src: Path, *, job_id: int, generator: str, meta: dict, tag: str,
                       post_interpolate: bool, cb: ProgressCb) -> int:
    """Persist a finished video that already lives on disk — MOVED (not copied)
    into videos_dir, so a multi-hundred-MB stitched clip never round-trips
    through RAM just to be written back out."""
    import shutil

    from ..generators import postprocess as pp
    from ..utils.io import require_video_output_space, stamp_name, video_thumb

    settings.ensure_dirs()
    path = settings.videos_dir / stamp_name(tag, "mp4")
    shutil.move(str(src), path)
    thumb, w, h = video_thumb(path, path.name)
    if post_interpolate:
        cb(0.95, "interpolating frames")
        try:
            out = path.with_name(path.stem + "_smooth.mp4")
            require_video_output_space(path.parent, [path])
            pp.interpolate_video(path, out, factor=2)
            new_thumb, nw, nh = video_thumb(out, out.name)  # refresh poster + dims
            if new_thumb:
                thumb, w, h = new_thumb, nw, nh
            path.unlink(missing_ok=True)
            path = out
        except Exception as e:  # noqa: BLE001
            logger.warning("interpolate skipped: %s", e)
            cb(0.97, f"interpolate skipped: {e}")
    # The video is moved into place rather than written here (a stitched clip can
    # be hundreds of MB and must not round-trip through RAM), so it is hashed by
    # streaming the file rather than the write-and-hash path images take.
    from ..utils.hashing import hash_file

    digest = hash_file(path) or ""
    asset = db.add_asset(AssetKind.video.value, path, thumb=thumb, width=w, height=h,
                         job_id=job_id, generator=generator, meta=meta,
                         content_hash=digest,
                         size_bytes=path.stat().st_size if path.exists() else 0,
                         mime_type="video/mp4")
    assert asset.id is not None
    if settings.effective_bool("embed_metadata"):
        _write_video_sidecar(path, asset_id=asset.id, job_id=job_id, generator=generator,
                             width=w, height=h, content_hash=digest, meta=meta)
    _enrich(asset.id)
    _announce_asset(asset.id, job_id, "video")
    return asset.id


def _write_video_sidecar(
    path: Path, *, asset_id: int, job_id: int, generator: str, width: int, height: int,
    content_hash: str, meta: dict,
) -> None:
    """Write portable metadata beside MP4 without re-encoding a large clip."""
    import json

    from ..version import get_version

    payload = {
        "schema": "wizards-brush-video/v1",
        "app": {"name": "The Wizard's Brush", "version": get_version()},
        "asset_id": asset_id,
        "job_id": job_id,
        "filename": path.name,
        "generator": generator,
        "width": width,
        "height": height,
        "content_hash": content_hash,
        "generation": meta,
    }
    sidecar = path.with_suffix(path.suffix + ".json")
    temporary = sidecar.with_suffix(sidecar.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(sidecar)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        logger.warning("video metadata sidecar skipped: %s", error)
