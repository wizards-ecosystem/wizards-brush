"""Async gallery enrichment: auto-captions/tags.

A daemon worker drains a queue of asset ids so generation is NEVER blocked:
`enqueue()` is fire-and-forget from persist_image/persist_video. The model runs
on CPU on purpose — a few hundred ms per asset is invisible here and the GPU
stays free for generation.

Captions: Florence-2-base (transformers-native) when ENRICH_CAPTIONS is on;
keyword tags derived from the caption feed the gallery's full-text search.
"""
from __future__ import annotations

import queue
import re
import threading
from pathlib import Path
from typing import Any

from . import db, log
from .config import settings
from .model_sources import revision_for

logger = log.get("enrich")

_Q: queue.Queue[int] = queue.Queue()
_STARTED = threading.Event()
_START_LOCK = threading.Lock()

_CAPTION: dict[str, Any] = {"model": None, "processor": None}

_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "with", "and", "or", "is", "are", "was",
    "were", "to", "from", "by", "for", "its", "his", "her", "their", "this", "that",
    "there", "image", "picture", "photo", "shows", "showing", "features", "depicts",
}


def enqueue(asset_id: int) -> None:
    """Fire-and-forget; starts the worker on first use."""
    if not settings.enrich_captions:
        return
    start()
    _Q.put(asset_id)


def start() -> None:
    # Locked: both lane threads can hit this at once — two workers would each
    # load their own copy of the CPU model.
    with _START_LOCK:
        if not _STARTED.is_set():
            _STARTED.set()
            threading.Thread(target=_worker, daemon=True, name="enrichment").start()


def backlog() -> int:
    return _Q.qsize()


def _worker() -> None:
    from .queue import hub

    while True:
        asset_id = _Q.get()
        try:
            _enrich_one(asset_id)
            hub.emit({"type": "asset", "id": asset_id})
        except Exception as e:  # noqa: BLE001 — enrichment must never crash the app
            logger.warning("asset %s enrichment failed: %s", asset_id, e)
        finally:
            _Q.task_done()


# Enrichment is a ladder, not a flag. Each rung commits before the next starts,
# so a crash partway through leaves the asset at the last rung it completed and
# the worker resumes there instead of redoing everything.
#
# Dimensions and the content hash are NOT rungs: both are free at write time
# (the bytes and the image object are already in hand), so staging them would be
# pretending work is expensive when it is not. Only the model-dependent steps
# are here.
LEVEL_SAVED = 0
LEVEL_CAPTIONED = 1
LEVEL_TAGGED = 2
LEVEL_EMBEDDED = 3


def _enrich_one(asset_id: int) -> None:
    from PIL import Image

    a = db.get_asset(asset_id)
    if not a:
        return
    src = Path(a.path)
    if not src.exists():
        # The file went away. Record that rather than silently doing nothing, so
        # the gallery can show it and offer to clean up.
        db.mark_missing(asset_id, True)
        return
    if a.kind == "video":
        # Use the poster thumb as the frame to caption.
        if not a.thumb:
            return
        src = settings.thumbs_dir / a.thumb
        if not src.exists():
            return

    level = int(a.enrich_level or 0)

    # Rung 0: content identity, for rows created before hashing existed. Cheap
    # (one streamed read), and everything about deduplication depends on it.
    if a.content_id is None:
        _attach_hash(asset_id, Path(a.path))

    img = Image.open(src).convert("RGB")

    # Rung 1: caption.
    if level < LEVEL_CAPTIONED and settings.enrich_captions:
        caption = generate_caption(img)
        if caption:
            db.merge_asset_tags(asset_id, [], caption=caption[:500],
                                enrich_level=LEVEL_CAPTIONED)
            level = LEVEL_CAPTIONED

    # Rung 2: tags derived from the caption. Separate from rung 1 so a crash
    # between them does not cost the caption that already succeeded.
    if level < LEVEL_TAGGED:
        current = db.get_asset(asset_id)
        caption = (current.caption if current else "") or ""
        if caption:
            db.merge_asset_tags(asset_id, _keywords(caption),
                                enrich_level=LEVEL_TAGGED)
            level = LEVEL_TAGGED

    # Rung 3: search embedding. Optional and heaviest, so it is last.
    if level < LEVEL_EMBEDDED and settings.enrich_embeddings:
        vector = embed_image(img)
        if vector is not None:
            db.update_asset(asset_id, embedding=vector, enrich_level=LEVEL_EMBEDDED)


def _attach_hash(asset_id: int, path: Path) -> None:
    """Give an older row a content identity, in the background.

    Rows written before content addressing existed have content_id NULL. This
    fills them in over time rather than blocking a migration on hashing every
    video in the library — the gallery stays fully usable while the backlog
    drains.
    """
    from .utils.hashing import hash_file

    digest = hash_file(path)
    if digest is None:
        db.mark_missing(asset_id, True)
        return
    try:
        st = path.stat()
        db.attach_content(asset_id, digest, size_bytes=st.st_size,
                          mime_type=_mime_for(path), mtime_ns=st.st_mtime_ns)
    except OSError:
        db.mark_missing(asset_id, True)


def _mime_for(path: Path) -> str:
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".mp4": "video/mp4"}.get(path.suffix.lower(), "")


def backfill_hashes(limit: int = 200) -> int:
    """Queue unhashed assets for enrichment. Returns how many were queued.

    Called at startup. Idempotent: an asset that already has a content row is
    not returned by the query.
    """
    ids = db.assets_needing_hash(limit)
    for aid in ids:
        start()
        _Q.put(aid)
    return len(ids)


# ---- captioner (Florence-2-base, CPU) ---------------------------------------
def generate_caption(img: Any) -> str | None:
    try:
        model = _caption_model()
        if model is None:
            return None
        import torch

        processor = _CAPTION["processor"]
        inputs = processor(text="<CAPTION>", images=img, return_tensors="pt")
        with torch.no_grad():
            ids = model.generate(input_ids=inputs["input_ids"],
                                 pixel_values=inputs["pixel_values"],
                                 max_new_tokens=96, num_beams=1, do_sample=False)
        text = processor.batch_decode(ids, skip_special_tokens=False)[0]
        parsed = processor.post_process_generation(text, task="<CAPTION>",
                                                   image_size=(img.width, img.height))
        return str(parsed.get("<CAPTION>", "")).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("caption failed: %s", e)
        return None


_CAPTION_RETRY_S = 300  # after a load failure, wait this long before trying again


def _caption_model():
    import time

    if _CAPTION["model"] is False:
        # Disabled after a failed load — but a failure can be transient (an HF
        # download hiccup), so retry after a cooldown instead of latching off for
        # the whole process lifetime.
        if time.monotonic() < _CAPTION.get("retry_after", 0.0):
            return None
        _CAPTION["model"] = None
    if _CAPTION["model"] is None:
        try:
            # transformers >=5 ships Florence-2 natively; the florence-community
            # repos are converted for it (microsoft's originals need the old
            # remote code, which transformers 5.x can't run).
            from transformers import AutoProcessor, Florence2ForConditionalGeneration

            name = "florence-community/Florence-2-base-ft"
            cache = str(settings.hf_hub_path)
            _CAPTION["processor"] = AutoProcessor.from_pretrained(
                name, cache_dir=cache, revision=revision_for(name) or "main"
            )
            _CAPTION["model"] = Florence2ForConditionalGeneration.from_pretrained(
                name, cache_dir=cache, revision=revision_for(name) or "main"
            ).eval()
            logger.info("Florence-2 captioner loaded (CPU)")
        except Exception as e:  # noqa: BLE001
            _CAPTION["model"] = False
            _CAPTION["retry_after"] = time.monotonic() + _CAPTION_RETRY_S
            logger.warning("captioner unavailable (retry in %ds): %s", _CAPTION_RETRY_S, e)
    return _CAPTION["model"] or None


def _keywords(caption: str, limit: int = 6) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z\-]{2,}", caption.lower())
    out: list[str] = []
    for w in words:
        if w in _STOPWORDS or w in out:
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


# ---- search embeddings (CLIP, CPU) -----------------------------------------
_EMBED: dict[str, Any] = {"model": None, "processor": None}
EMBED_DIM = 512


def _embed_model():
    """Load the CLIP image encoder once, on CPU.

    Lazy and cached exactly like the captioner. A few hundred milliseconds per
    asset on CPU is invisible in a background worker, and it keeps the GPU free
    for the thing the user is actually waiting on.
    """
    if _EMBED["model"] is None:
        from transformers import CLIPImageProcessor, CLIPVisionModelWithProjection

        name = settings.embed_model
        logger.info("loading embedding model %s (CPU)", name)
        _EMBED["processor"] = CLIPImageProcessor.from_pretrained(
            name, cache_dir=str(settings.hf_hub_path), revision=revision_for(name) or "main")
        _EMBED["model"] = CLIPVisionModelWithProjection.from_pretrained(
            name, cache_dir=str(settings.hf_hub_path), revision=revision_for(name) or "main").eval()
    return _EMBED["model"], _EMBED["processor"]


def embed_image(img: Any) -> bytes | None:
    """A normalised float32 image embedding as raw bytes, or None on failure.

    Normalised at write time so search is a plain dot product rather than a
    cosine computed per comparison — the same ranking, less arithmetic per query.

    Returns None rather than raising: enrichment is a background nicety and must
    never be able to take down the worker or block a save.
    """
    try:
        import numpy as np
        import torch

        model, processor = _embed_model()
        with torch.no_grad():
            inputs = processor(images=img, return_tensors="pt")
            vec = model(**inputs).image_embeds[0].float()
        vec = vec / (vec.norm() + 1e-9)
        return np.asarray(vec.numpy(), dtype=np.float32).tobytes()
    except Exception as e:  # noqa: BLE001 — search is optional, generation is not
        logger.warning("embedding failed: %s", e)
        return None


def embed_text(text: str) -> bytes | None:
    """The same embedding space, for a search query.

    A different encoder head from `embed_image`, projecting into a shared space —
    which is the whole point of CLIP and the reason text can rank images at all.
    """
    try:
        import numpy as np
        import torch

        # CLIPTokenizer rather than CLIPTokenizerFast: the fast variant is not
        # re-exported at the package root in every transformers version, and the
        # tokenizer runs once per query — the speed difference is irrelevant here.
        from transformers import CLIPTextModelWithProjection, CLIPTokenizer

        name = settings.embed_model
        if _EMBED.get("text_model") is None:
            _EMBED["tokenizer"] = CLIPTokenizer.from_pretrained(
                name, cache_dir=str(settings.hf_hub_path), revision=revision_for(name) or "main")
            _EMBED["text_model"] = CLIPTextModelWithProjection.from_pretrained(
                name, cache_dir=str(settings.hf_hub_path), revision=revision_for(name) or "main").eval()
        with torch.no_grad():
            tokens = _EMBED["tokenizer"]([text], padding=True, truncation=True,
                                         return_tensors="pt")
            vec = _EMBED["text_model"](**tokens).text_embeds[0].float()
        vec = vec / (vec.norm() + 1e-9)
        return np.asarray(vec.numpy(), dtype=np.float32).tobytes()
    except Exception as e:  # noqa: BLE001
        logger.warning("query embedding failed: %s", e)
        return None
