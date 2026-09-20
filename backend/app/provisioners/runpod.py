"""Rent a GPU pod from Runpod and give the app its URL.

Plain REST over httpx, which the app already depends on. A vendor SDK in a
local-first application would be the wrong signal for a feature that is off by
default, and the whole surface used here is four calls.

The pod runs the same worker container as any other host; nothing Runpod-shaped
reaches the rest of the app, which only ever learns a base URL.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from .. import log
from ..config import settings
from .base import ERROR, READY, STARTING, STOPPING, Provisioner, ProvisionerError, Session

logger = log.get("runpod")

API = "https://api.runpod.io/v2"

# Control-plane calls are short by nature; a create that hangs should surface as
# an error the user can retry, not as a UI that waits forever on a spinner.
TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 1024 * 1024

# Everything this app creates carries the marker, so `adopt()` can tell our pods
# from ones the user runs themselves. A name alone is not attribution - the
# recorded session id is - but the marker keeps an orphan recognisable by eye in
# the Runpod console.
NAME_PREFIX = "wizards-brush"
MARKER_ENV = "WIZARDS_BRUSH_MANAGED"


class RunpodProvisioner:
    id = "runpod"
    label = "Runpod"

    def __init__(self, store: Any) -> None:
        self._store = store

    # ---- helpers ----------------------------------------------------------
    def _key(self) -> str:
        key = (settings.runpod_api_key or "").strip()
        if not key:
            raise ProvisionerError(
                "RUNPOD_API_KEY is not set. Add it to .env (console.runpod.io/user/settings) "
                "and restart, or set REMOTE_GPU_PROVISIONER=manual."
            )
        return key

    def _redact(self, text: str) -> str:
        """Never let the key reach a log line, a toast, or an error body."""
        key = (settings.runpod_api_key or "").strip()
        return text.replace(key, "***") if key else text

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        key = self._key()
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.request(
                    method, f"{API}{path}",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise ProvisionerError(self._redact(f"Runpod is unreachable: {exc}")) from exc
        if response.status_code == 401:
            raise ProvisionerError("Runpod rejected the API key (401). Check RUNPOD_API_KEY.")
        if response.status_code == 403:
            raise ProvisionerError(
                "The Runpod API key is valid but lacks permission for this action (403). "
                "A read-only or narrowly scoped key cannot create or terminate pods."
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            detail = response.content[:600].decode("utf-8", errors="replace")
            raise ProvisionerError(self._redact(f"Runpod error {response.status_code}: {detail}"))
        if response.status_code == 204 or not response.content:
            return {}
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ProvisionerError("Runpod returned an implausibly large response")
        return response.json()

    def _base_url(self, pod_id: str) -> str:
        return f"https://{pod_id}-{settings.runpod_worker_port}.proxy.runpod.net"

    # ---- protocol ---------------------------------------------------------
    def configured(self) -> bool:
        return bool((settings.runpod_api_key or "").strip()
                    and (settings.runpod_image or "").strip())

    def start(self) -> Session:
        if not (settings.runpod_image or "").strip():
            raise ProvisionerError(
                "RUNPOD_IMAGE is not set. Build and push the worker image "
                "(`make remote-gpu-image`) and name it in .env. See docs/runpod.md."
            )
        existing = self.adopt()
        if existing is not None:
            # Two pods is the expensive mistake: refuse rather than quietly
            # double the bill because a click was repeated.
            raise ProvisionerError(
                f"A Runpod session is already running ({existing.id}). Stop it first."
            )
        port = settings.runpod_worker_port
        body: dict[str, Any] = {
            "name": f"{NAME_PREFIX}-{uuid.uuid4().hex[:8]}",
            "image": settings.runpod_image.strip(),
            "cloud": (settings.runpod_cloud or "SECURE").strip().upper(),
            "gpu": {"id": settings.runpod_gpu_type.strip(), "count": 1},
            "disk": int(settings.runpod_container_disk_gb),
            "ports": [f"{port}/http"],
            "env": {
                # The worker authenticates every request with this; it is the
                # only thing between a public proxy URL and a rented GPU.
                "REMOTE_GPU_SHARED_SECRET": settings.remote_gpu_shared_secret or "",
                "HF_TOKEN": settings.hf_token or "",
                "PORT": str(port),
                MARKER_ENV: "1",
                # Dead-man's switch. The app terminates the pod on a clean
                # shutdown, but a crash, an OOM kill or a lost laptop runs no
                # shutdown code at all - so the worker also watches for silence
                # and terminates itself.
                "REMOTE_GPU_IDLE_TERMINATE_MIN": str(settings.runpod_idle_terminate_min),
                "REMOTE_GPU_MAX_SESSION_HOURS": str(settings.runpod_max_session_hours),
            },
        }
        if (settings.runpod_network_volume_id or "").strip():
            body["mounts"] = {"network": [{
                "volumeId": settings.runpod_network_volume_id.strip(),
                "path": "/workspace",
            }]}
            body["env"]["REMOTE_GPU_ROOT"] = "/workspace/wizards-brush"
        centers = [c.strip() for c in (settings.runpod_data_center_ids or "").split(",") if c.strip()]
        if centers:
            body["dataCenterIds"] = centers

        created = self._request("POST", "/pods", body) or {}
        pod_id = str(created.get("id") or "")
        if not pod_id:
            raise ProvisionerError("Runpod accepted the request but returned no pod id")
        session = Session(
            id=pod_id,
            provisioner=self.id,
            state=STARTING,
            base_url=self._base_url(pod_id),
            detail="pod created; pulling the image",
            started_at=time.time(),
            hourly_usd=(float(created["cost"]) if created.get("cost") is not None else None),
            gpu=str((created.get("gpu") or {}).get("id") or settings.runpod_gpu_type),
        )
        # Record before returning. A crash in the next millisecond must still
        # leave a pod this app can find and stop.
        self._store.save(session)
        logger.info("started runpod pod %s (%s)", pod_id, session.gpu)
        return session

    def _refresh(self, session: Session) -> Session:
        pod = self._request("GET", f"/pods/{session.id}")
        if pod is None:
            self._store.clear()
            return Session(id=session.id, provisioner=self.id, state=ERROR,
                           detail="the pod no longer exists", started_at=session.started_at)
        status = str(pod.get("status") or "").upper()
        runtime = pod.get("runtime") or None
        if status in {"EXITED", "TERMINATED"}:
            state, detail = ERROR, f"the pod is {status.lower()}; it is no longer billing compute"
        elif status != "RUNNING":
            state, detail = STARTING, f"pod {status.lower()}"
        elif not runtime:
            # Runpod reports RUNNING as soon as the container object exists, so
            # this is the long wait: a first pull of the worker image.
            state, detail = STARTING, "pulling the worker image (this is the slow part)"
        else:
            state, detail = READY, "pod is up; waiting for the worker to answer"
        updated = Session(
            id=session.id, provisioner=self.id, state=state,
            base_url=self._base_url(session.id), detail=detail,
            started_at=session.started_at,
            hourly_usd=(float(pod["cost"]) if pod.get("cost") is not None else session.hourly_usd),
            gpu=str((pod.get("gpu") or {}).get("id") or session.gpu),
        )
        self._store.save(updated)
        return updated

    def status(self) -> Session | None:
        session = self._store.load()
        if session is None or session.provisioner != self.id:
            return None
        return self._refresh(session)

    def adopt(self) -> Session | None:
        """What a previous process left running, if anything.

        Only a session this app recorded counts. A pod that merely looks like
        ours by name is not ours to terminate.
        """
        session = self._store.load()
        if session is None or session.provisioner != self.id:
            return None
        try:
            return self._refresh(session)
        except ProvisionerError:
            return session      # unreachable now; still ours, still billing

    def stop(self) -> None:
        session = self._store.load()
        if session is None or session.provisioner != self.id:
            return
        self._store.save(Session(
            id=session.id, provisioner=self.id, state=STOPPING,
            base_url=session.base_url, detail="terminating", started_at=session.started_at,
            hourly_usd=session.hourly_usd, gpu=session.gpu,
        ))
        try:
            # Terminate, never stop: a stopped pod releases the GPU without
            # guaranteeing it back, and keeps billing any attached volume.
            self._request("DELETE", f"/pods/{session.id}")
            logger.info("terminated runpod pod %s", session.id)
        finally:
            self._store.clear()


_ = Provisioner  # documents the interface this class is written against
