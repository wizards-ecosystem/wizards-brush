"""Content-addressed resource storage on the worker: manifest, ensure, receive.

The app prepares a resource here before any job needs it, as a separate step
from inference. Identity is the sha256 of the bytes: the app never sends a
path, and nothing it sends is read as one. A resource lands as
`<root>/<aa>/<sha256>.safetensors` only after its full hash is verified, from a
partial file under `<root>/.partial/` that a retried transfer resumes at the
offset this module reports.

Standard library, FastAPI and pydantic only, so it is importable and testable
without torch; `install_routes` is what the worker app mounts. Route types are
imported at module level because postponed annotations are resolved there.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, Field

SHA256 = re.compile(r"^[0-9a-f]{64}$")
KINDS = ("adapter",)
MAX_BYTES = 8 << 30
CHUNK_BYTES = 32 << 20
DISK_MARGIN = 4 << 30
_NAME = re.compile(r"[^A-Za-z0-9._ -]")


class StoreError(Exception):
    """A refusal with an HTTP status and a stable code for the app."""

    def __init__(self, status: int, code: str, detail: str, **state: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.state = state


class Store:
    def __init__(self, root: Path, *, max_bytes: int = MAX_BYTES,
                 disk_margin: int = DISK_MARGIN) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.disk_margin = disk_margin

    # ---- places ------------------------------------------------------------
    def _check(self, sha256: str) -> str:
        if not SHA256.fullmatch(sha256 or ""):
            raise StoreError(400, "invalid_identifier", "sha256 must be 64 lowercase hex characters")
        return sha256

    def _final(self, sha256: str) -> Path:
        return self.root / sha256[:2] / f"{sha256}.safetensors"

    def _partial(self, sha256: str) -> Path:
        return self.root / ".partial" / f"{sha256}.part"

    def _index(self, sha256: str) -> Path:
        return self.root / sha256[:2] / f"{sha256}.json"

    def path_for(self, sha256: str) -> Path | None:
        """The verified file for a hash, or None. Never derived from input text
        other than the validated hash itself."""
        self._check(sha256)
        final = self._final(sha256)
        return final if final.is_file() else None

    # ---- the protocol --------------------------------------------------------
    def manifest(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.root.exists():
            return out
        for index in sorted(self.root.glob("??/*.json")):
            try:
                entry = json.loads(index.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if self._final(str(entry.get("sha256", ""))).is_file():
                out.append(entry)
        return out

    def ensure(self, sha256: str, size: int, kind: str, *, family: str = "unknown",
               name: str = "") -> dict[str, Any]:
        """Start or resume receiving a resource; report where to continue."""
        self._check(sha256)
        if kind not in KINDS:
            raise StoreError(400, "unsupported_kind", f"this worker stores {', '.join(KINDS)} "
                             f"resources, not {kind!r}")
        if not isinstance(size, int) or not 0 < size <= self.max_bytes:
            raise StoreError(400, "too_large", f"size must be 1 to {self.max_bytes} bytes")
        final = self._final(sha256)
        if final.is_file():
            if final.stat().st_size != size:
                raise StoreError(409, "conflict", "a stored file with this hash has another size")
            return {"sha256": sha256, "state": "already_present", "received": size, "size": size}
        partial = self._partial(sha256)
        partial.parent.mkdir(parents=True, exist_ok=True)
        received = partial.stat().st_size if partial.exists() else 0
        if received > size:
            partial.unlink()
            received = 0
        needed = size - received + self.disk_margin
        free = shutil.disk_usage(partial.parent).free
        if free < needed:
            raise StoreError(507, "insufficient_disk",
                             f"{needed >> 20} MB is needed and {free >> 20} MB is free")
        if not partial.exists():
            partial.write_bytes(b"")
        meta = {"sha256": sha256, "size": size, "kind": kind,
                "family": str(family or "unknown")[:40],
                "name": _NAME.sub("", str(name or ""))[:120], "started_at": time.time()}
        partial.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")
        return {"sha256": sha256, "state": "receiving", "received": received, "size": size,
                "chunk_bytes": CHUNK_BYTES}

    def append(self, sha256: str, offset: int, data: bytes) -> dict[str, Any]:
        self._check(sha256)
        partial = self._partial(sha256)
        meta_path = partial.with_suffix(".json")
        if not partial.exists() or not meta_path.exists():
            raise StoreError(404, "not_found", "no transfer is in progress for this hash")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        received = partial.stat().st_size
        if offset != received:
            raise StoreError(409, "offset_mismatch", f"expected offset {received}",
                             received=received)
        if len(data) > CHUNK_BYTES or received + len(data) > int(meta["size"]):
            raise StoreError(400, "too_large", "the chunk exceeds the chunk or declared size")
        with partial.open("ab") as handle:
            handle.write(data)
        return {"sha256": sha256, "state": "receiving", "received": received + len(data),
                "size": int(meta["size"])}

    def complete(self, sha256: str) -> dict[str, Any]:
        self._check(sha256)
        if self._final(sha256).is_file():
            return {"sha256": sha256, "state": "already_present"}
        partial = self._partial(sha256)
        meta_path = partial.with_suffix(".json")
        if not partial.exists() or not meta_path.exists():
            raise StoreError(404, "not_found", "no transfer is in progress for this hash")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        received = partial.stat().st_size
        if received != int(meta["size"]):
            raise StoreError(409, "upload_incomplete",
                             f"received {received} of {meta['size']} bytes", received=received)
        digest = hashlib.sha256()
        with partial.open("rb") as handle:
            while chunk := handle.read(4 << 20):
                digest.update(chunk)
        if digest.hexdigest() != sha256:
            partial.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            raise StoreError(400, "hash_mismatch",
                             "the received bytes do not match their sha256; discarded")
        final = self._final(sha256)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial, final)
        meta.update(verified_at=time.time())
        self._index(sha256).write_text(json.dumps(meta), encoding="utf-8")
        meta_path.unlink(missing_ok=True)
        return {"sha256": sha256, "state": "ready", "size": int(meta["size"])}

    def cancel(self, sha256: str) -> bool:
        self._check(sha256)
        partial = self._partial(sha256)
        existed = partial.exists()
        partial.unlink(missing_ok=True)
        partial.with_suffix(".json").unlink(missing_ok=True)
        return existed


# ---- HTTP -------------------------------------------------------------------------
class EnsureRequest(BaseModel):
    # Module level, not inside install_routes: with postponed annotations
    # FastAPI resolves the parameter's type from this module's globals, and a
    # local class there silently became a required query parameter.
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(gt=0)
    kind: str = Field(max_length=20)
    family: str = Field(default="unknown", max_length=40)
    name: str = Field(default="", max_length=200)


def install_routes(app: Any, store: Store, auth: Any) -> None:
    """Mount the resource protocol on `app`. `auth(secret)` raises on a bad
    secret; the ingress middleware has already checked it once."""

    def call(fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except StoreError as error:
            raise HTTPException(status_code=error.status, detail={
                "code": error.code, "detail": error.detail, **error.state}) from None

    @app.get("/resources/manifest")
    def resources_manifest(x_gen_secret: str | None = Header(default=None)) -> dict[str, Any]:
        auth(x_gen_secret)
        return {"resources": store.manifest()}

    @app.post("/resources/ensure")
    def resources_ensure(req: EnsureRequest,
                         x_gen_secret: str | None = Header(default=None)) -> Any:
        auth(x_gen_secret)
        return call(store.ensure, req.sha256, req.size, req.kind, family=req.family,
                    name=req.name)

    @app.put("/resources/{sha256}/chunk")
    async def resources_chunk(sha256: str, offset: int, request: Request,
                              x_gen_secret: str | None = Header(default=None)) -> Any:
        auth(x_gen_secret)
        data = await request.body()
        return call(store.append, sha256, offset, data)

    @app.post("/resources/{sha256}/complete")
    def resources_complete(sha256: str, x_gen_secret: str | None = Header(default=None)) -> Any:
        auth(x_gen_secret)
        return call(store.complete, sha256)

    @app.delete("/resources/{sha256}/partial")
    def resources_cancel(sha256: str,
                         x_gen_secret: str | None = Header(default=None)) -> dict[str, bool]:
        auth(x_gen_secret)
        return {"canceled": call(store.cancel, sha256)}
