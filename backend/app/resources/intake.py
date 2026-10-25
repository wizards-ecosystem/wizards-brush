"""Safe local intake: upload receipts and the import pipeline.

The four checks are kept apart because each answers a different question:

1. **Safe file handling.** Only `.safetensors` is imported. Pickle-based
   formats (`.ckpt`, `.pt`, `.pth`, `.bin`, `.pkl`) can execute code when
   loaded and are refused before a byte is accepted; archives are refused
   rather than expanded. Bytes land in a quarantine directory beside their
   destination and are promoted with an atomic rename, so a failure never
   leaves a half-imported file in the library.
2. **Recognised format and family.** The safetensors header is read with a
   bounded read and checked structurally (dtypes, shapes, offsets inside the
   file, no overlap) before anything trusts it; the tensor names give the
   family (modelprobe).
3. **Runtime compatibility** is a later question (the resolver and a load
   probe). Importing a file does not claim it will load.
4. **Curated quality** is never inferred from an import.

A checkpoint is not a LoRA because both end in `.safetensors`: importing a
full model as an adapter, or an adapter as a model, is refused by what the
tensors are.

Browsers and remote clients upload through resumable chunked sessions and
import the finished upload by its receipt id. They never name a path on this
machine. `import_path` exists for the local command line only.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import secrets
import shutil
import struct
import time
import unicodedata
from pathlib import Path
from typing import Any, Literal

from .. import log
from ..config import settings
from ..problems import RequestError
from ..studio.contracts import resource_id
from . import catalogue, store

logger = log.get("intake")

Kind = Literal["adapter", "model"]
KINDS: tuple[str, ...] = ("adapter", "model")
# Serializations that can run code on load. Never imported by this path.
UNSAFE_SUFFIXES = frozenset({".ckpt", ".pt", ".pth", ".bin", ".pkl", ".pickle", ".pyc",
                             ".py", ".exe", ".dll", ".so", ".dylib", ".sh", ".bat", ".ps1"})
ARCHIVE_SUFFIXES = frozenset({".zip", ".tar", ".gz", ".tgz", ".xz", ".bz2", ".7z", ".rar"})
MAX_BYTES: dict[str, int] = {"adapter": 4 << 30, "model": 40 << 30}
# Each chunk stays far under the request cap (security.MAX_REQUEST_BYTES).
CHUNK_BYTES = 32 << 20
DISK_MARGIN = 2 << 30
UPLOAD_TTL_S = 24 * 3600
MAX_HEADER_BYTES = 16 * 1024 * 1024
_UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^(sha256:)?[0-9a-f]{64}$")
_DTYPE_BYTES = {"F64": 8, "F32": 4, "F16": 2, "BF16": 2, "I64": 8, "I32": 4, "I16": 2,
                "I8": 1, "U8": 1, "U16": 2, "U32": 4, "U64": 8, "BOOL": 1,
                "F8_E4M3": 1, "F8_E5M2": 1, "F8_E8M0": 1, "F4": 1, "F6_E2M3": 1,
                "F6_E3M2": 1}
_WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul",
                               *(f"com{i}" for i in range(1, 10)),
                               *(f"lpt{i}" for i in range(1, 10))})


def _refuse(status: int, detail: str, code: str, field: str | None = None,
            **extra: Any) -> RequestError:
    return RequestError(status, detail, code=code, field=field, **extra)


# ---- names and places ------------------------------------------------------------------
def safe_filename(name: str, *, fallback: str = "resource.safetensors") -> str:
    """A filename that is safe on Linux and Windows, keeping what a person wrote.

    Drops any directory part (either separator), control characters and the
    characters Windows forbids, trims trailing dots and spaces, avoids device
    names (CON, NUL, COM1...), and bounds the length while keeping the suffix.
    Letters in any script and spaces survive.
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    text = re.split(r"[\\/]", text)[-1]
    text = "".join(ch for ch in text if ch.isprintable() and ch not in '<>:"|?*')
    text = text.strip().rstrip(". ")
    stem, dot, suffix = text.rpartition(".")
    if not dot:
        stem, suffix = text, ""
    stem = stem.strip().rstrip(". ") or Path(fallback).stem
    if stem.lower().split(".")[0] in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    suffix = suffix.lower()[:16]
    budget = 120 - (len(suffix) + 1 if suffix else 0)
    stem = stem[:budget].rstrip(". ") or "resource"
    return f"{stem}.{suffix}" if suffix else stem


def _root_name(kind: str) -> str:
    return "loras" if kind == "adapter" else "weights"


def _root(kind: str) -> Path:
    return settings.loras_dir if kind == "adapter" else settings.weights_path


def _incoming(kind: str) -> Path:
    path = _root(kind) / ".incoming"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _destination(kind: str) -> Path:
    path = _root(kind) / ("imported" if kind == "adapter" else "checkpoints")
    path.mkdir(parents=True, exist_ok=True)
    return path


def check_format(filename: str) -> str:
    """Refuse by name before any bytes are taken. Returns the safe name."""
    name = safe_filename(filename)
    suffix = Path(name).suffix.lower()
    if suffix in UNSAFE_SUFFIXES:
        raise _refuse(
            400, f"{suffix} files can run code when they are loaded and are not imported",
            "unsafe_format", "filename",
            suggestions=["Use the .safetensors version of this model or adapter."],
        )
    if suffix in ARCHIVE_SUFFIXES:
        raise _refuse(400, "archives are not imported; extract the .safetensors file first",
                      "unsupported_format", "filename")
    if suffix != ".safetensors":
        raise _refuse(400, f"only .safetensors files are imported, not {suffix or 'no suffix'}",
                      "unsupported_format", "filename")
    return name


def _check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise _refuse(400, "kind must be adapter or model", "invalid_request", "kind",
                      valid_options=list(KINDS))
    return kind


# ---- structural validation -------------------------------------------------------------
def validate_safetensors(path: Path) -> dict[str, Any]:
    """The header of a structurally valid safetensors file, or a refusal.

    Bounded: reads the 8-byte length and at most 16 MB of JSON, never the
    tensors. Every tensor entry must name a known dtype and a shape whose byte
    size equals its offsets, inside the file, without overlapping another.
    """
    size = path.stat().st_size
    with path.open("rb") as handle:
        raw = handle.read(8)
        if len(raw) < 8:
            raise _refuse(400, "the file is too short to be safetensors", "invalid_safetensors")
        (length,) = struct.unpack("<Q", raw)
        if not 0 < length <= MAX_HEADER_BYTES or 8 + length > size:
            raise _refuse(400, "the safetensors header length is not plausible",
                          "invalid_safetensors")
        blob = handle.read(length)
    try:
        header = json.loads(blob)
    except ValueError:
        raise _refuse(400, "the safetensors header is not JSON", "invalid_safetensors") from None
    if not isinstance(header, dict):
        raise _refuse(400, "the safetensors header is not an object", "invalid_safetensors")
    data_len = size - 8 - length
    spans: list[tuple[int, int]] = []
    for name, entry in header.items():
        if name == "__metadata__":
            if not isinstance(entry, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in entry.items()):
                raise _refuse(400, "safetensors __metadata__ must map strings to strings",
                              "invalid_safetensors")
            continue
        if not isinstance(entry, dict):
            raise _refuse(400, f"tensor {name[:80]!r} has no description", "invalid_safetensors")
        dtype, shape, offsets = entry.get("dtype"), entry.get("shape"), entry.get("data_offsets")
        width = _DTYPE_BYTES.get(str(dtype))
        if width is None:
            raise _refuse(400, f"tensor {name[:80]!r} has an unknown dtype {dtype!r}",
                          "invalid_safetensors")
        if not isinstance(shape, list) or not all(
                isinstance(d, int) and not isinstance(d, bool) and d >= 0 for d in shape):
            raise _refuse(400, f"tensor {name[:80]!r} has an invalid shape", "invalid_safetensors")
        if (not isinstance(offsets, list) or len(offsets) != 2
                or not all(isinstance(o, int) and not isinstance(o, bool) for o in offsets)
                or not 0 <= offsets[0] <= offsets[1] <= data_len):
            raise _refuse(400, f"tensor {name[:80]!r} points outside the file",
                          "invalid_safetensors")
        count = 1
        for dim in shape:
            count *= dim
        if str(dtype) not in {"F4", "F6_E2M3", "F6_E3M2"} and count * width != offsets[1] - offsets[0]:
            raise _refuse(400, f"tensor {name[:80]!r} size does not match its shape",
                          "invalid_safetensors")
        spans.append((offsets[0], offsets[1]))
    spans.sort()
    for (_, end), (start, _) in itertools.pairwise(spans):
        if start < end:
            raise _refuse(400, "two tensors overlap in the file", "invalid_safetensors")
    return header


# ---- upload sessions (receipts) ---------------------------------------------------------
def _session_path(kind: str, upload_id: str) -> tuple[Path, Path]:
    incoming = _incoming(kind)
    return incoming / f"{upload_id}.json", incoming / f"{upload_id}.part"


def _find_session(upload_id: str) -> tuple[dict[str, Any], Path, Path]:
    if not _UPLOAD_ID.fullmatch(upload_id or ""):
        raise _refuse(404, "no such upload", "upload_not_found", "upload_id")
    for kind in KINDS:
        meta, part = _session_path(kind, upload_id)
        if meta.exists():
            try:
                return json.loads(meta.read_text(encoding="utf-8")), meta, part
            except ValueError:
                break
    raise _refuse(404, "no such upload", "upload_not_found", "upload_id")


def _save_session(meta_path: Path, session: dict[str, Any]) -> None:
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(session), encoding="utf-8")
    os.replace(tmp, meta_path)


def _public_session(session: dict[str, Any]) -> dict[str, Any]:
    return {key: session[key] for key in ("upload_id", "filename", "kind", "size", "received",
                                          "state", "sha256", "chunk_bytes")}


def create_upload(filename: str, size: int, kind: str, sha256: str | None = None) -> dict[str, Any]:
    _check_kind(kind)
    name = check_format(filename)
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BYTES[kind]:
        raise _refuse(400, f"size must be 1 to {MAX_BYTES[kind] >> 30} GB for a {kind}",
                      "too_large" if isinstance(size, int) and size > 0 else "invalid_request",
                      "size")
    if sha256 is not None and not _SHA256.fullmatch(sha256.lower()):
        raise _refuse(400, "sha256 must be 64 hex characters", "invalid_request", "sha256")
    incoming = _incoming(kind)
    free = shutil.disk_usage(incoming).free
    if free < size + DISK_MARGIN:
        raise _refuse(
            507, f"not enough disk space: {size >> 20} MB plus a {DISK_MARGIN >> 30} GB margin "
                 f"is needed and {free >> 20} MB is free",
            "insufficient_disk", "size", retryable=True,
        )
    upload_id = secrets.token_hex(16)
    meta, part = _session_path(kind, upload_id)
    part.write_bytes(b"")
    session = {"upload_id": upload_id, "filename": name, "kind": kind, "size": size,
               "received": 0, "state": "receiving", "chunk_bytes": CHUNK_BYTES,
               "sha256": (f"sha256:{sha256.lower().removeprefix('sha256:')}" if sha256 else None),
               "declared_sha256": bool(sha256), "created_at": time.time()}
    _save_session(meta, session)
    return _public_session(session)


def append_chunk(upload_id: str, offset: int, data: bytes) -> dict[str, Any]:
    session, meta, part = _find_session(upload_id)
    if session["state"] != "receiving":
        raise _refuse(409, f"the upload is {session['state']}", "conflict", "upload_id")
    if offset != session["received"]:
        raise _refuse(409, f"expected offset {session['received']}, got {offset}",
                      "offset_mismatch", "offset", retryable=True,
                      state=_public_session(session))
    if len(data) > CHUNK_BYTES or session["received"] + len(data) > session["size"]:
        raise _refuse(400, "the chunk is larger than allowed or than the declared size",
                      "too_large", "body")
    with part.open("r+b") as handle:
        handle.seek(offset)
        handle.write(data)
    session["received"] += len(data)
    _save_session(meta, session)
    return _public_session(session)


def complete_upload(upload_id: str) -> dict[str, Any]:
    session, meta, part = _find_session(upload_id)
    if session["state"] == "complete":
        return _public_session(session)
    if session["received"] != session["size"] or part.stat().st_size != session["size"]:
        raise _refuse(409, f"received {session['received']} of {session['size']} bytes",
                      "upload_incomplete", "upload_id", retryable=True,
                      state=_public_session(session))
    digest = catalogue.sha256_file(part)
    if digest is None:
        raise _refuse(500, "the upload could not be read back", "upload_failed", "upload_id")
    if session.get("declared_sha256") and digest != session["sha256"]:
        cancel_upload(upload_id)
        raise _refuse(400, "the file does not match the sha256 it was declared with; it was "
                           "discarded", "hash_mismatch", "sha256")
    session.update(state="complete", sha256=digest)
    _save_session(meta, session)
    return _public_session(session)


def cancel_upload(upload_id: str) -> bool:
    try:
        _session, meta, part = _find_session(upload_id)
    except RequestError:
        return False
    part.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    return True


def sweep_uploads(max_age_s: float = UPLOAD_TTL_S) -> int:
    """Remove abandoned uploads, and quarantine files nothing owns."""
    removed = 0
    cutoff = time.time() - max_age_s
    for kind in KINDS:
        incoming = _root(kind) / ".incoming"
        if not incoming.exists():
            continue
        for path in incoming.iterdir():
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


# ---- import ----------------------------------------------------------------------------
def import_receipt(upload_id: str, *, kind: str | None = None, label: str = "") -> dict[str, Any]:
    """Import a completed upload. The upload is consumed either way."""
    session, meta, part = _find_session(upload_id)
    if session["state"] != "complete":
        raise _refuse(409, "finish the upload before importing it", "upload_incomplete",
                      "receipt")
    wanted = _check_kind(kind or session["kind"])
    if wanted != session["kind"]:
        raise _refuse(400, f"this upload was declared as a {session['kind']}",
                      "invalid_request", "kind")
    try:
        return import_file(part, kind=wanted, original_name=session["filename"],
                           digest=session["sha256"], label=label)
    finally:
        part.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)


def import_path(source: Path, *, kind: str, label: str = "") -> dict[str, Any]:
    """Import a file from this machine (the local command line only).

    The source is copied, never moved or modified, through the same
    quarantine and checks as an upload.
    """
    _check_kind(kind)
    name = check_format(source.name)
    if source.is_symlink() or not source.is_file():
        raise _refuse(400, "the source must be a regular file", "invalid_request", "path")
    size = source.stat().st_size
    if size > MAX_BYTES[kind]:
        raise _refuse(400, f"the file is larger than {MAX_BYTES[kind] >> 30} GB", "too_large",
                      "path")
    incoming = _incoming(kind)
    if shutil.disk_usage(incoming).free < size + DISK_MARGIN:
        raise _refuse(507, "not enough disk space for the import", "insufficient_disk", "path")
    tmp = incoming / f"{secrets.token_hex(16)}.part"
    try:
        shutil.copyfile(source, tmp)
        digest = catalogue.sha256_file(tmp)
        if digest is None:
            raise _refuse(500, "the copy could not be read back", "upload_failed", "path")
        return import_file(tmp, kind=kind, original_name=name, digest=digest, label=label)
    finally:
        tmp.unlink(missing_ok=True)


def import_file(tmp: Path, *, kind: str, original_name: str, digest: str,
                label: str = "") -> dict[str, Any]:
    """Validate a quarantined file and promote it, or refuse and leave nothing.

    `tmp` must already be inside the kind's quarantine directory, so the
    promotion is a same-filesystem atomic rename. Content already installed
    is not imported twice: the existing resource is returned.
    """
    from ..modelprobe import probe_header

    name = check_format(original_name)
    header = validate_safetensors(tmp)
    probe = probe_header(header)
    if kind == "adapter" and not probe.is_adapter:
        raise _refuse(
            400, "this file holds model weights, not an adapter; import it as a model",
            "not_an_adapter", "kind",
            suggestions=["A checkpoint is not a LoRA even though both are .safetensors."],
        )
    if kind == "model" and probe.is_adapter:
        raise _refuse(400, "this file is an adapter (LoRA), not a model; import it as an adapter",
                      "not_a_model", "kind")
    rid = resource_id("local", digest.removeprefix("sha256:"))
    if store.get(rid) is not None and catalogue.local_file(rid) is not None:
        logger.info("import of %s skipped: the same content is already installed", name)
        return {"status": "already_present", "resource": catalogue.view(store.get(rid))}
    destination = _destination(kind)
    target = destination / name
    if target.exists():
        target = destination / f"{Path(name).stem}-{digest[-64:][:8]}{Path(name).suffix}"
    os.replace(tmp, target)
    stat = target.stat()
    resource = catalogue.register_file(
        target, digest, location=catalogue.location_for(_root_name(kind), target), stat=stat,
        original_name=name, label=label,
    )
    logger.info("imported %s as %s", name, rid)
    return {"status": "imported", "resource": catalogue.view(resource)}
