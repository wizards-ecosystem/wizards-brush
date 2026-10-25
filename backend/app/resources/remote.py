"""Prepare a resource on the Remote GPU worker, before any job needs it.

The worker stores files by the sha256 of their bytes (worker/resources.py).
Preparing one is: ask the worker to ensure it, stream the bytes from the
offset the worker reports (a retried or interrupted transfer resumes), then
ask it to verify the full hash and promote the file. Only bytes and their hash
cross the wire. A local path is never sent and nothing the worker returns is
used as a path here.

This is resource preparation, separate from inference: a prepared adapter is
stored on the worker, which is not the same as the worker applying it to a
job. Until a worker advertises that it applies user adapters, plans that need
them on the remote target stay blocked.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .. import log
from ..problems import RequestError
from . import catalogue, store

logger = log.get("remote-resources")

TIMEOUT = 60.0
CHUNK_BYTES = 32 << 20
CHUNK_TRIES = 3
_TASKS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _refuse(status: int, detail: str, code: str, **extra: Any) -> RequestError:
    from ..remote_gpu_client import redacted

    return RequestError(status, redacted(detail), code=code, field="resource_id", **extra)


def _target_identity() -> str:
    from ..remote_gpu_client import last_health

    build = str(last_health().get("build") or "")
    return f"remote:{build}" if build else "remote"


def _check(resource_id: str) -> tuple[Any, Path, str]:
    from ..remote_gpu_client import last_health

    row = store.get(resource_id)
    if row is None:
        raise _refuse(404, f"resource '{resource_id}' is not known here", "unknown_resource")
    if row.provider != "local" or not row.content_hash.startswith("sha256:"):
        raise _refuse(400, "only imported files are transferred; a Hub model is fetched by the "
                           "worker from its provider", "unsupported_source")
    if row.kind != "adapter":
        raise _refuse(400, f"the worker stores adapters, not {row.kind} files, for now",
                      "unsupported_kind")
    path = catalogue.local_file(resource_id)
    if path is None:
        raise _refuse(409, "no local copy of this resource is installed", "resource_not_ready")
    health = last_health()
    if not health.get("connected"):
        raise _refuse(503, "the Remote GPU is not connected", "target_unavailable",
                      retryable=True)
    if "resources" not in set(health.get("features") or []):
        raise _refuse(409, "the connected worker does not support resource transfer; it needs "
                           "a newer build", "unsupported_protocol")
    return row, path, row.content_hash.removeprefix("sha256:")


def _call(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    from ..remote_gpu_client import redacted

    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError as error:
        raise _refuse(503, f"the worker did not answer: {error}", "target_unavailable",
                      retryable=True) from error
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:300]
        body = detail if isinstance(detail, dict) else {"detail": str(detail)}
        raise RequestError(
            response.status_code if response.status_code in {400, 404, 409, 507} else 502,
            redacted(str(body.get("detail") or "the worker refused")),
            code=str(body.get("code") or "remote_error"), field="resource_id",
            state={k: v for k, v in body.items() if k not in {"detail", "code"}},
        )
    return response.json()


def prepare(resource_id: str, *, cancel: Callable[[], bool] | None = None,
            progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """Make the worker hold this resource. Returns the final state."""
    from ..remote_gpu_client import connection

    row, path, sha256 = _check(resource_id)
    base, secret = connection()
    size = path.stat().st_size
    headers = {"X-Gen-Secret": secret, "User-Agent": "wizards-brush"}
    with httpx.Client(base_url=base.rstrip("/"), headers=headers, timeout=TIMEOUT) as client:
        ensured = _call(client, "POST", "/resources/ensure", json={
            "sha256": sha256, "size": size, "kind": row.kind, "family": row.family,
            "name": Path(row.provider_ref or "").name,
        })
        if ensured.get("state") != "already_present":
            offset = int(ensured.get("received") or 0)
            chunk = min(int(ensured.get("chunk_bytes") or CHUNK_BYTES), CHUNK_BYTES)
            with path.open("rb") as handle:
                while offset < size:
                    if cancel is not None and cancel():
                        _call(client, "DELETE", f"/resources/{sha256}/partial")
                        raise _refuse(409, "the transfer was canceled", "canceled")
                    handle.seek(offset)
                    data = handle.read(chunk)
                    offset = _send(client, sha256, offset, data)
                    if progress is not None:
                        progress(offset, size)
            _call(client, "POST", f"/resources/{sha256}/complete")
    store.upsert_install(resource_id, _target_identity(), f"remote:sha256:{sha256}",
                         state="verified", size_bytes=size, verified_hash=row.content_hash,
                         verified_at=datetime.now(UTC), error="")
    state = "already_present" if ensured.get("state") == "already_present" else "ready"
    logger.info("prepared %s on the Remote GPU (%s)", resource_id, state)
    return {"state": state, "resource": catalogue.view(store.get(resource_id))}


def _send(client: httpx.Client, sha256: str, offset: int, data: bytes) -> int:
    """Send one chunk; resume at the worker's offset when it disagrees."""
    last: RequestError | None = None
    for _attempt in range(CHUNK_TRIES):
        try:
            answer = _call(client, "PUT", f"/resources/{sha256}/chunk",
                           params={"offset": offset}, content=data)
            return int(answer["received"])
        except RequestError as error:
            if error.code == "offset_mismatch" and isinstance(error.state, dict):
                # The worker already has more (an earlier answer was lost), or
                # less: continue from what it says it holds.
                return int(error.state.get("received", offset))
            if not error.retryable:
                raise
            last = error
    assert last is not None
    raise last


# ---- background preparation, visible while it runs --------------------------------------
def start(resource_id: str) -> dict[str, Any]:
    """Start preparing in the background (validated first); idempotent."""
    _check(resource_id)
    with _LOCK:
        task = _TASKS.get(resource_id)
        if task and task["state"] == "running":
            return dict(task)
        task = {"resource_id": resource_id, "state": "running", "sent": 0, "size": 0,
                "error": "", "cancel": False}
        _TASKS[resource_id] = task

    def run() -> None:
        try:
            result = prepare(resource_id, cancel=lambda: bool(task["cancel"]),
                             progress=lambda sent, size: task.update(sent=sent, size=size))
            task.update(state=result["state"])
        except RequestError as error:
            task.update(state="canceled" if error.code == "canceled" else "failed",
                        error=str(error.detail))
        except Exception as error:  # noqa: BLE001 — a background transfer reports, never crashes
            from ..remote_gpu_client import redacted

            task.update(state="failed", error=redacted(str(error)))

    threading.Thread(target=run, name=f"prepare-{resource_id[-12:]}", daemon=True).start()
    return {k: v for k, v in task.items() if k != "cancel"}


def status(resource_id: str) -> dict[str, Any] | None:
    task = _TASKS.get(resource_id)
    return None if task is None else {k: v for k, v in task.items() if k != "cancel"}


def cancel(resource_id: str) -> bool:
    task = _TASKS.get(resource_id)
    if task is None or task["state"] != "running":
        return False
    task["cancel"] = True
    return True
