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

# Each release publishes the worker image here, tagged with `worker.build_id()`.
# With RUNPOD_IMAGE unset the app runs the tag that matches its own worker
# sources, so the pod and the checkout cannot disagree about the code.
REGISTRY = "ghcr.io"
PUBLISHED_REPO = "wizards-ecosystem/wizards-brush-remote-gpu"
_MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


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

    def _image(self) -> tuple[str, bool]:
        """The image to run, and whether it is the published default."""
        custom = (settings.runpod_image or "").strip()
        if custom:
            return custom, False
        from ..remote_gpu_client import local_build_id

        build = local_build_id()
        if not build:
            raise ProvisionerError(
                "Cannot tell which worker image to run: the worker/ sources are missing. "
                "Set RUNPOD_IMAGE to an image you built. See docs/runpod.md."
            )
        return f"{REGISTRY}/{PUBLISHED_REPO}:{build}", True

    def _check_published(self, image: str) -> None:
        """Refuse before creating a pod that could never pull its image.

        A pod whose image does not exist sits in "pulling" and bills until
        someone stops it. The usual cause is a worker/ edited locally, which
        changes the build id to one no release ever published.
        """
        tag = image.rsplit(":", 1)[1]
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                token = client.get(f"https://{REGISTRY}/token",
                                   params={"scope": f"repository:{PUBLISHED_REPO}:pull"})
                bearer = (token.json().get("token", "")
                          if token.status_code == 200 else "")
                # No anonymous token is GHCR's answer for a package that does
                # not exist or is not public: either way a pod could not pull it.
                status = token.status_code if not bearer else client.head(
                    f"https://{REGISTRY}/v2/{PUBLISHED_REPO}/manifests/{tag}",
                    headers={"Authorization": f"Bearer {bearer}", "Accept": _MANIFEST_TYPES},
                ).status_code
        except (httpx.HTTPError, ValueError) as exc:
            raise ProvisionerError(
                f"Could not check that the worker image {image} is published ({exc}). "
                "Nothing was created; try again."
            ) from exc
        if status != 200:
            raise ProvisionerError(
                f"No public worker image matches this checkout (build {tag}). That "
                "usually means worker/ was changed locally, or the release's image has "
                "not been made public yet. Nothing was created. Build and "
                "push your own with `make remote-gpu-image` and set RUNPOD_IMAGE. "
                "See docs/runpod.md."
            )

    def _base_url(self, pod_id: str) -> str:
        return f"https://{pod_id}-{settings.runpod_worker_port}.proxy.runpod.net"

    # ---- protocol ---------------------------------------------------------
    def configured(self) -> bool:
        return bool((settings.runpod_api_key or "").strip())

    def start(self) -> Session:
        self._key()
        image, published = self._image()
        existing = self.adopt()
        if existing is not None:
            # Two pods is the expensive mistake: refuse rather than quietly
            # double the bill because a click was repeated.
            raise ProvisionerError(
                f"A Runpod session is already running ({existing.id}). Stop it first."
            )
        if published:
            self._check_published(image)
        port = settings.runpod_worker_port
        body: dict[str, Any] = {
            "name": f"{NAME_PREFIX}-{uuid.uuid4().hex[:8]}",
            "image": image,
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
