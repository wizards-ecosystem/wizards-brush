"""Register, verify and resolve resources.

Two sources feed the catalogue, and neither is trusted to be the identity:

* **Configured model slots** (`.env` variants, the edit and video models).
  Each becomes `hf:<repo>@<pinned commit>` when the repo has a reviewed pin,
  or `@unpinned` when it follows whatever the identifier serves. They are
  registered `legacy_unverified`: their weights are never hashed here, and a
  plan that names one says the revision is unverified rather than exact.
* **The LoRA library.** Each `.safetensors` file is identified by the sha256 of
  its bytes, hashed in bounded background work, never during a migration.
  Two copies of one file are one resource with two installs; a file that
  disappears leaves its install marked `missing` instead of vanishing.

Locations are private and contained. An install's location is
"<root>:<relative posix path>" under a managed root (`loras`, `weights`) or
"hf:<repo>" for a Hub cache snapshot. `path_for` resolves one with a
containment check that also refuses symlinks escaping the root.
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import log
from ..config import settings
from ..studio.contracts import resource_id
from . import store

logger = log.get("resources")

CHUNK = 4 << 20
# Directories under a managed root that are the app's own working space.
PRIVATE_DIRS = frozenset({".incoming"})


def _roots() -> dict[str, Path]:
    return {"loras": settings.loras_dir, "weights": settings.weights_path}


def location_for(root: str, path: Path) -> str:
    """The stored location of a file under a managed root."""
    base = _roots()[root].resolve()
    relative = path.resolve().relative_to(base)
    return f"{root}:{relative.as_posix()}"


def path_for(location: str) -> Path | None:
    """The file a location names, or None when it is not a contained file.

    Refuses traversal and symlinks that resolve outside the root: a location
    is stored data and is replayed, so this check runs on every use.
    """
    root, _, relative = location.partition(":")
    base = _roots().get(root)
    if base is None or not relative or relative.startswith(("/", "\\")):
        return None
    base = base.resolve()
    try:
        candidate = (base / relative).resolve()
        candidate.relative_to(base)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def public_location(location: str) -> dict[str, str]:
    """What a client may see of an install: the root and a relative path."""
    root, _, relative = location.partition(":")
    return {"root": root, "path": relative}


def sha256_file(path: Path, *, cancel: Callable[[], bool] | None = None) -> str | None:
    """"sha256:<hex>" of a file, streaming. None if it cannot be read or the
    caller canceled midway."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK):
                if cancel is not None and cancel():
                    return None
                digest.update(chunk)
    except OSError:
        return None
    return f"sha256:{digest.hexdigest()}"


# ---- configured model slots ------------------------------------------------------------
@dataclass(frozen=True)
class Slot:
    """A place the app is configured to use a model from."""

    lane: str        # "local" | "remote"
    role: str        # "image" | "edit" | "video"
    variant: str     # the model_variant name, or the role for single-model slots
    repo: str
    gated: bool = False


def configured_slots() -> list[Slot]:
    from ..generators import variants

    slots = [Slot("local" if v.lane == "local" else "remote", "image", v.name,
                  variants.repo_of(v), v.gated)
             for v in variants.VARIANTS if variants.repo_of(v)]
    for role, repo in (("edit", settings.qwen_edit_model), ("video", settings.video_model),
                       ("video_hunyuan", settings.hunyuan_video_model),
                       ("video_ltx", settings.ltx_video_model)):
        if (repo or "").strip():
            slots.append(Slot("remote", role.split("_")[0], role, repo.strip()))
    return slots


def model_resource_id(repo: str) -> str:
    from ..model_sources import revision_for

    return resource_id("hf", repo, revision_for(repo))


def register_configured_models() -> int:
    """Record every configured slot's model. No network, no hashing."""
    from ..backends.local import _cache_status
    from ..modelprobe import family_of_model

    by_repo: dict[str, list[Slot]] = {}
    for slot in configured_slots():
        by_repo.setdefault(slot.repo, []).append(slot)
    for repo, slots in by_repo.items():
        rid = model_resource_id(repo)
        existing = store.get(rid)
        revision = rid.rsplit("@", 1)[1]
        store.upsert(
            rid, kind="model", family=family_of_model(repo), provider="hf", provider_ref=repo,
            revision="" if revision == "unpinned" else revision, format="diffusers",
            label=repo.rsplit("/", 1)[-1],
            access="gated" if any(s.gated for s in slots) else (
                existing.access if existing else "unknown"),
            verification=(existing.verification if existing else "legacy_unverified"),
            constraints={"slots": [{"lane": s.lane, "role": s.role, "variant": s.variant}
                                   for s in slots]},
        )
        if any(s.lane == "local" for s in slots):
            status, size_gb = _cache_status(repo)
            if status == "ready":
                store.upsert_install(rid, "local", f"hf:{repo}", state="installed",
                                     size_bytes=int((size_gb or 0) * 1024 ** 3), error="")
                if not existing or existing.state in {"known", "unavailable"}:
                    store.upsert(rid, state="installed")
            elif existing is None or existing.state == "known":
                store.upsert(rid, state="known")
    return len(by_repo)


# ---- the LoRA library ------------------------------------------------------------------
def _library_files() -> list[Path]:
    root = settings.loras_dir
    if not root.exists():
        return []
    out = []
    for path in root.rglob("*"):
        if any(part in PRIVATE_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_file() and path.suffix.lower() == ".safetensors":
            out.append(path)
    return sorted(out)


def _metadata(header: dict[str, Any]) -> dict[str, Any]:
    meta = header.get("__metadata__")
    return meta if isinstance(meta, dict) else {}


def _license_from(header: dict[str, Any]) -> dict[str, Any]:
    meta = _metadata(header)
    value = str(meta.get("modelspec.license") or meta.get("license") or "").strip()
    return ({"id": value[:120], "source": "file metadata", "reviewed": False}
            if value else {"id": "unknown", "source": "none", "reviewed": False})


def describe_file(path: Path) -> dict[str, Any]:
    """What a bounded header read says about a safetensors file."""
    from ..loras import pinned_variants, recommended_weight, sidecar_config
    from ..modelprobe import probe_header, read_header

    header = read_header(path) or {}
    probe = probe_header(header) if header else None
    declared = ""
    meta = _metadata(header)
    if meta:
        declared = str(meta.get("ss_base_model_version") or meta.get("modelspec.architecture")
                       or "")[:80]
    config = sidecar_config(path)
    is_adapter = bool(probe and probe.is_adapter)
    # Which fact named the family: the tensor names themselves, or only the
    # trainer's declared metadata (which is often a stale default).
    by_names = probe_header({k: v for k, v in header.items() if k != "__metadata__"}) \
        if header else None
    if by_names is not None and by_names.family != "unknown":
        evidence = "tensor-names"
    elif probe is not None and probe.family != "unknown":
        evidence = "declared"
    else:
        evidence = "none"
    return {
        "kind": "adapter" if is_adapter else "model",
        "family": probe.family if probe else "unknown",
        "format": "safetensors" if is_adapter else "single_file",
        "license": _license_from(header) if header else {"id": "unknown", "source": "none"},
        "compat": {
            "probe": ({"display": probe.display, "adapter_type": probe.adapter_type,
                       "rank": probe.rank, "keys": probe.keys, "error": probe.error}
                      if probe else {"error": "not a readable safetensors file"}),
            "declared_base": declared,
            "evidence": evidence,
        },
        "constraints": ({"base_family": probe.family if probe else "unknown",
                         "variants": pinned_variants(path, config),
                         "recommended_weight": recommended_weight(path, config)}
                        if is_adapter else {}),
        "readable": bool(header),
    }


_SCAN_LOCK = threading.Lock()


def scan_library(*, budget_bytes: int | None = None,
                 cancel: Callable[[], bool] | None = None) -> dict[str, int]:
    """Bring the LoRA library's installs in line with the files on disk.

    Files whose size and mtime match their install are not re-read. Others
    are hashed, up to `budget_bytes` per call so a startup scan of a large
    library finishes in bounded time and the rest waits for the next pass.
    """
    counts = {"files": 0, "hashed": 0, "unchanged": 0, "deferred": 0, "missing": 0}
    with _SCAN_LOCK:
        seen: set[str] = set()
        spent = 0
        for path in _library_files():
            counts["files"] += 1
            try:
                stat = path.stat()
                location = location_for("loras", path)
            except (OSError, ValueError):
                continue
            seen.add(location)
            known = store.install_at("local", location)
            if (known is not None and known.size_bytes == stat.st_size
                    and known.mtime_ns == stat.st_mtime_ns and known.state != "missing"):
                counts["unchanged"] += 1
                continue
            if budget_bytes is not None and spent + stat.st_size > budget_bytes and spent:
                counts["deferred"] += 1
                continue
            digest = sha256_file(path, cancel=cancel)
            if digest is None:
                counts["deferred"] += 1
                continue
            spent += stat.st_size
            counts["hashed"] += 1
            register_file(path, digest, location=location, stat=stat,
                          original_name=path.name)
        for install in store.installs(target="local"):
            if install.location.startswith("loras:") and install.location not in seen \
                    and install.state != "missing" and install.id is not None:
                store.mark_installs([install.id], state="missing",
                                    error="the file is no longer in the LoRA library")
                counts["missing"] += 1
                _refresh_state(install.resource_id)
    if counts["hashed"] or counts["missing"]:
        logger.info("LoRA library: %s", counts)
    return counts


def register_file(path: Path, digest: str, *, location: str, stat: os.stat_result,
                  original_name: str, label: str = "") -> Any:
    """Record a managed file as an install of the resource its bytes identify."""
    facts = describe_file(path)
    rid = resource_id("local", digest.removeprefix("sha256:"))
    existing = store.get(rid)
    now = datetime.now(UTC)
    store.upsert(
        rid, kind=facts["kind"], family=facts["family"], provider="local",
        provider_ref=existing.provider_ref if existing and existing.provider_ref else original_name,
        content_hash=digest, format=facts["format"], size_bytes=stat.st_size,
        label=label or (existing.label if existing and existing.label else Path(original_name).stem),
        license=(existing.license if existing and existing.license.get("reviewed")
                 else facts["license"]),
        compat=facts["compat"], constraints=facts["constraints"],
        verification="verified", error="" if facts["readable"] else "not a readable safetensors file",
    )
    store.upsert_install(rid, "local", location, state="verified", size_bytes=stat.st_size,
                         mtime_ns=stat.st_mtime_ns, verified_hash=digest, verified_at=now,
                         error="")
    _refresh_state(rid)
    return store.get(rid)


def _refresh_state(rid: str) -> None:
    """A resource is installed while any local copy is present, else unavailable."""
    resource = store.get(rid)
    if resource is None:
        return
    live = [i for i in store.installs(rid) if i.state in {"installed", "verified"}]
    if live:
        if resource.kind == "model" and resource.format == "single_file":
            # Present, but no loader runs a single-file checkpoint yet.
            store.upsert(rid, state="installed",
                         error="single-file checkpoints are catalogued but not loadable yet")
        elif resource.state in {"known", "unavailable", "failed"} or resource.error:
            store.upsert(rid, state="installed", error="")
    elif resource.provider == "local":
        store.upsert(rid, state="unavailable", error="no copy of this file is installed")


# ---- lookups ---------------------------------------------------------------------------
def local_file(rid: str) -> Path | None:
    """A present, contained local copy of a file resource, or None."""
    for install in store.installs(rid, target="local"):
        if install.state not in {"installed", "verified"}:
            continue
        path = path_for(install.location)
        if path is not None:
            return path
    return None


def library_path(rid: str) -> str | None:
    """The LoRA-library relative path the existing loader takes, or None."""
    for install in store.installs(rid, target="local"):
        if install.location.startswith("loras:") and install.state in {"installed", "verified"} \
                and path_for(install.location) is not None:
            return install.location.removeprefix("loras:")
    return None


def resource_for_library_path(relative: str) -> str | None:
    install = store.install_at("local", f"loras:{Path(relative).as_posix()}")
    return install.resource_id if install is not None else None


def view(resource: Any) -> dict[str, Any]:
    installs = store.installs(resource.id)
    return {
        "id": resource.id, "kind": resource.kind, "family": resource.family,
        "provider": resource.provider, "provider_ref": resource.provider_ref,
        "revision": resource.revision, "content_hash": resource.content_hash,
        "format": resource.format, "size_bytes": resource.size_bytes, "label": resource.label,
        "license": resource.license, "access": resource.access, "state": resource.state,
        "verification": resource.verification, "compat": resource.compat,
        "constraints": resource.constraints, "error": resource.error,
        "installs": [{"target": i.target, **public_location(i.location), "state": i.state,
                      "size_bytes": i.size_bytes, "error": i.error} for i in installs],
        "updated_at": resource.updated_at,
    }


def refresh(*, budget_bytes: int | None = 4 << 30) -> dict[str, int]:
    """Startup and rescan: configured models, then a bounded library pass."""
    models = register_configured_models()
    counts = scan_library(budget_bytes=budget_bytes)
    return {"models": models, **counts}
