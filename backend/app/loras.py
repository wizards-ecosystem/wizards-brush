"""Local LoRA catalogue.

LoRAs are plain `.safetensors` files the user drops into `models/loras/`.
There is no download manager and no registry of known adapters: the community
ecosystem is too large and too fast-moving to curate, and a directory the user
controls is both simpler and harder to get wrong.

Kept free of heavy imports so the registry and API can list adapters without
touching torch — the same convention as everything else outside generators/.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

from . import log
from .config import settings

if TYPE_CHECKING:  # `Probe` is only needed for annotations — keep modelprobe lazy.
    from .modelprobe import Probe

logger = log.get("loras")

SUFFIXES = (".safetensors",)
# Adapter names reach peft, which uses them as dict keys and in module paths.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
MAX_ACTIVE = 4          # stacking more than this rarely helps and costs VRAM
WEIGHT_MIN, WEIGHT_MAX = -2.0, 2.0   # negatives are a legitimate technique


def _pretty(stem: str) -> str:
    """A readable label from a filename, without destroying deliberate casing."""
    return re.sub(r"[_-]+", " ", stem).strip() or stem


def adapter_name(path_or_name: str) -> str:
    """peft adapter key for a file. Stable across restarts (it is the stem), and
    sanitized because peft embeds it in module names."""
    # Include the whole relative path: `portraits/style.safetensors` and
    # `landscapes/style.safetensors` are distinct catalogue entries and must not
    # collide on the same PEFT key. Preserve the old short name for root files.
    raw = str(path_or_name).replace("\\", "/")
    stem = raw.rsplit(".", 1)[0]
    safe = _SAFE_NAME.sub("_", stem.replace("/", "_")) or "lora"
    if len(safe) <= 64:
        return safe
    import hashlib

    return f"{safe[:55]}_{hashlib.sha256(raw.encode()).hexdigest()[:8]}"


# (path, mtime_ns, size) -> Probe. A probe reads a few hundred KB and parses
# JSON; the picker asks for the whole catalogue on every open, and a library of
# a few hundred adapters would otherwise re-read all of them each time.
# Keyed on mtime and size so editing or replacing a file invalidates its entry.
_PROBE_CACHE: OrderedDict[tuple[str, int, int], Probe] = OrderedDict()
_SIDECAR_CACHE: OrderedDict[tuple[str, int, int], dict] = OrderedDict()
_CACHE_LIMIT = 512


def probe_cached(path: Path) -> Probe:
    from .modelprobe import probe_file

    try:
        st = path.stat()
    except OSError:
        return probe_file(path)
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _PROBE_CACHE.get(key)
    if cached is None:
        cached = probe_file(path)
        _PROBE_CACHE[key] = cached
        while len(_PROBE_CACHE) > _CACHE_LIMIT:
            _PROBE_CACHE.popitem(last=False)
    else:
        _PROBE_CACHE.move_to_end(key)
    return cached


def sidecar_config(path: Path) -> dict:
    """Small, portable configuration stored next to an adapter.

    Adapter tensors identify an architecture, not the exact checkpoint or its
    preferred strength.  The sidecar is therefore the one place a local library
    can record facts learned from the author's card or a reproducible test.
    Bad sidecars deliberately degrade to an empty mapping: a typo in a note must
    never make an otherwise usable LoRA disappear.
    """
    import json

    side = path.with_suffix(".json")
    try:
        stat = side.stat()
    except FileNotFoundError:
        return {}
    except OSError:
        logger.warning("could not read adapter configuration %s", side.name)
        return {}
    key = (str(side), stat.st_mtime_ns, stat.st_size)
    if key in _SIDECAR_CACHE:
        _SIDECAR_CACHE.move_to_end(key)
        return dict(_SIDECAR_CACHE[key])
    try:
        raw = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("could not read adapter configuration %s", side.name)
        return {}
    config = raw if isinstance(raw, dict) else {}
    _SIDECAR_CACHE[key] = config
    while len(_SIDECAR_CACHE) > _CACHE_LIMIT:
        _SIDECAR_CACHE.popitem(last=False)
    return dict(config)


def pinned_variants(path: Path, config: dict | None = None) -> list[str]:
    """Model variants this adapter declares it is for; empty means "any".

    Read from a sidecar next to the file — `stylename.json` beside
    `stylename.safetensors` — holding `{"variants": ["turbo"]}`.

    This exists because an adapter is trained against a specific CHECKPOINT, and
    nothing in the file reliably says which. Family matching is too coarse to
    catch it: Z-Image-Turbo and Z-Image base are both family "zimage", every
    tensor shape lines up, so an adapter for one loads happily into the other
    and silently produces garbage — a distilled-model adapter amplified by the
    base model's CFG 4.0 renders as near-black or noise rather than as an error.

    A sidecar rather than a database: it travels with the file, survives a
    re-scan, and can be written by hand the moment the user learns which model
    an adapter belongs to.
    """
    names = (config if config is not None else sidecar_config(path)).get("variants")
    if not isinstance(names, list):
        return []
    return [str(n).strip().lower() for n in names if str(n).strip()]


def recommended_weight(path: Path, config: dict | None = None) -> float | None:
    """An author's or measured safe starting weight, when the sidecar has one."""
    raw = (config if config is not None else sidecar_config(path)).get("recommended_weight")
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    return max(WEIGHT_MIN, min(value, WEIGHT_MAX))


def list_loras(model: str | None = None) -> list[dict]:
    """Every adapter in LORA_DIR. Missing dir is not an error — it simply means
    the user has not added any yet.

    Each entry carries the architecture read from the file's own header, and —
    when `model` is given — whether it can be applied to that model. The picker
    uses this to group rather than to hide: a file the user put there is always
    listed, even when we think it will not work.
    """
    from .modelprobe import compatible, family_of_model

    d = settings.loras_dir
    if not d.exists():
        return []
    target = family_of_model(model) if model else "unknown"
    out = []
    for p in sorted(d.rglob("*"), key=lambda f: f.name.lower()):
        if not p.is_file() or p.suffix.lower() not in SUFFIXES:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        probe = probe_cached(p)
        config = sidecar_config(p)
        relative = p.relative_to(d)
        confidently_not_adapter = (
            p.suffix.lower() == ".safetensors" and not probe.is_adapter and not probe.error
        )
        out.append({
            "name": adapter_name(str(relative)),
            "label": (_pretty(p.stem) if relative.parent == Path(".")
                      else f"{_pretty(p.stem)} · {relative.parent.as_posix()}"),
            "filename": p.name,
            # Relative so the value persisted in job params stays valid if the
            # library moves; resolve_path() turns it back into a real path.
            "path": str(relative),
            "size_bytes": size,
            "family": probe.family,
            "arch": probe.display,
            "rank": probe.rank,
            "is_adapter": probe.is_adapter,
            "adapter_type": probe.adapter_type,
            "selectable": not confidently_not_adapter,
            "unavailable_reason": (
                "This safetensors file contains model weights, not a recognized adapter. "
                "Move checkpoints, VAEs, and text encoders out of the LoRA folder."
                if confidently_not_adapter else ""
            ),
            # True also when either side is unknown: absence of evidence is not
            # evidence of a mismatch, and we do not warn on a guess.
            "compatible": compatible(probe.family, target),
            # Retained in the API for older frontends. Only safetensors are now
            # catalogued, so this compatibility field is always false.
            "pickled": False,
            # Empty = usable anywhere its family matches. See pinned_variants().
            "variants": pinned_variants(p, config),
            # Kept with the adapter rather than globally hard-coded. A rank-256
            # LoRA and a rank-4 LoRA should not silently begin at the same
            # strength just because their tensor family matches.
            "recommended_weight": recommended_weight(p, config),
            "why": str(config.get("why") or "").strip(),
        })
    return out


def resolve_path(rel: str) -> Path | None:
    """Absolute path for a catalogue entry, or None if it escaped LORA_DIR.

    Job params are user-supplied and are replayed on rerun, so this is a real
    containment check rather than a formality."""
    d = settings.loras_dir.resolve()
    try:
        p = (d / rel).resolve()
        p.relative_to(d)
    except (OSError, ValueError):
        return None
    return p if p.is_file() and p.suffix.lower() in SUFFIXES else None


def sanitize(raw: object) -> list[dict]:
    """Normalize the `loras` job param into [{path, weight}], dropping anything
    that does not resolve. Never raises: a stale LoRA reference in a reran job
    should cost you that adapter, not the whole generation."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"path": item, "weight": 1.0}
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or item.get("name") or "").strip()
        path = resolve_path(rel) if rel else None
        if not rel or rel in seen or path is None:
            if rel:
                logger.warning("skipping unknown LoRA %r", rel)
            continue
        probe = probe_cached(path)
        if path.suffix.lower() == ".safetensors" and not probe.is_adapter and not probe.error:
            logger.warning("skipping non-adapter safetensors file %r", rel)
            continue
        try:
            weight = float(item.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        normalized = {"path": rel, "weight": max(WEIGHT_MIN, min(weight, WEIGHT_MAX))}
        if "te_weight" in item:
            try:
                te_weight = float(str(item.get("te_weight")))
            except (TypeError, ValueError):
                te_weight = weight
            normalized["te_weight"] = max(WEIGHT_MIN, min(te_weight, WEIGHT_MAX))
        seen.add(rel)
        out.append(normalized)
        if len(out) >= MAX_ACTIVE:
            break
    return out
