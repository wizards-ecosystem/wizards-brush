"""Rent a GPU pod from Runpod and give the app its URL.

Plain REST over httpx, which the app already depends on. A vendor SDK in a
local-first application would be the wrong signal for a feature that is off by
default, and the whole surface used here is four calls.

The pod runs the same worker container as any other host; nothing Runpod-shaped
reaches the rest of the app, which only ever learns a base URL.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Any

import httpx

from .. import log, worker_config
from ..config import settings
from .base import (
    ERROR,
    READY,
    STARTING,
    STOP_FAILED,
    STOP_STATES,
    STOP_UNCONFIRMED,
    STOPPING,
    Provisioner,
    ProvisionerError,
    Session,
)

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
# Errors raised before a request could leave this machine. Anything else - a
# read timeout, a dropped connection mid-response - may have reached Runpod, so
# the action it carried might have happened.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Gateway answers: the request may have reached the API behind them.
_GATEWAY = frozenset({502, 503, 504})
# Runpod reports a terminated pod either as gone (404) or with this status.
_GONE = frozenset({"TERMINATED"})

# Every read-modify-write of the recorded session. A status poll that read the
# session before a stop cleared it must not write it back afterwards.
_LOCK = threading.RLock()

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
        except _NOT_SENT as exc:
            raise ProvisionerError(self._redact(f"Runpod is unreachable: {exc}"),
                                   code="provider_unreachable") from exc
        except httpx.HTTPError as exc:
            # Sent, but the answer never arrived: the action may have happened.
            raise ProvisionerError(
                self._redact(f"Runpod did not answer in time ({type(exc).__name__}: {exc})"),
                uncertain=True, code="provider_timeout",
            ) from exc
        if response.status_code == 401:
            raise ProvisionerError("Runpod rejected the API key (401). Check RUNPOD_API_KEY.",
                                   status=401, code="provider_auth")
        if response.status_code == 403:
            raise ProvisionerError(
                "The Runpod API key is valid but lacks permission for this action (403). "
                "A read-only or narrowly scoped key cannot create or terminate pods.",
                status=403, code="provider_forbidden",
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            detail = response.content[:600].decode("utf-8", errors="replace")
            raise ProvisionerError(
                self._redact(f"Runpod error {response.status_code}: {detail}"),
                uncertain=response.status_code in _GATEWAY, status=response.status_code,
            )
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
        if existing is not None and existing.unresolved_stop:
            # Ownership of the old pod is unresolved: it may still exist and
            # bill. A second rental on top of it is the expensive mistake.
            raise ProvisionerError(
                f"Stopping Runpod pod {existing.id} was not confirmed, so it may still be "
                "running. Retry Stop (it checks with Runpod first), or forget the session "
                "once you have confirmed in the Runpod console that the pod is gone.",
                code="stop_unresolved",
            )
        if existing is not None:
            # Two pods is the expensive mistake: refuse rather than quietly
            # double the bill because a click was repeated.
            raise ProvisionerError(
                f"A Runpod session is already running ({existing.id}). Stop it first.",
                code="session_exists",
            )
        if published:
            self._check_published(image)
        port = settings.runpod_worker_port
        gpu: dict[str, Any] = {"id": settings.runpod_gpu_type.strip(), "count": 1}
        min_cuda = (settings.runpod_min_cuda or "").strip()
        if min_cuda:
            # Runpod wants major.minor; a bare major is a 422 after the request
            # is already shaped, so refuse here, before anything is created.
            if not re.fullmatch(r"\d+\.\d+", min_cuda):
                raise ProvisionerError(
                    f"RUNPOD_MIN_CUDA must look like 12.8, not {min_cuda!r}. Nothing was created."
                )
            gpu["minCudaVersion"] = min_cuda
        body: dict[str, Any] = {
            "name": f"{NAME_PREFIX}-{uuid.uuid4().hex[:8]}",
            "image": image,
            "cloud": (settings.runpod_cloud or "SECURE").strip().upper(),
            "gpu": gpu,
            "disk": int(settings.runpod_container_disk_gb),
            "ports": [f"{port}/http"],
            "env": {
                # This install's model slots and knobs. The published image bakes
                # the shipped defaults; the worker reads environment first, so
                # one image serves every configuration and a `.env` that names
                # an alternate model actually produces that model's picker.
                **worker_config.deploy_env(),
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
        now = time.time()
        if pod is None or str(pod.get("status") or "").upper() in _GONE:
            with _LOCK:
                current = self._store.load()
                if current is not None and current.id == session.id:
                    self._store.clear()
            if session.state in STOP_STATES:
                logger.info("runpod pod %s is confirmed gone", session.id)
            return Session(id=session.id, provisioner=self.id, state=ERROR,
                           detail="the pod no longer exists", started_at=session.started_at,
                           observed_at=now)
        status = str(pod.get("status") or "").upper()
        runtime = pod.get("runtime") or None
        if status == "EXITED":
            state, detail = ERROR, ("the pod has exited; it no longer bills compute, but it "
                                    "still exists until it is terminated")
        elif status != "RUNNING":
            state, detail = STARTING, f"pod {status.lower()}"
        elif not runtime:
            # Runpod reports RUNNING as soon as the container object exists, so
            # this is the long wait: a first pull of the worker image.
            state, detail = STARTING, "pulling the worker image (this is the slow part)"
        else:
            state, detail = READY, "pod is up; waiting for the worker to answer"
        updated = session.replace(
            state=state, base_url=self._base_url(session.id), detail=detail,
            hourly_usd=(float(pod["cost"]) if pod.get("cost") is not None else session.hourly_usd),
            gpu=str((pod.get("gpu") or {}).get("id") or session.gpu),
            observed_at=now, observe_error="",
        )
        with _LOCK:
            current = self._store.load()
            if current is None or current.id != session.id:
                # Stopped or forgotten while we asked: report what we saw, but
                # never write a cleared session back into existence.
                return updated
            if current.state in STOP_STATES:
                # The pod still exists, so a stop is still owed. Keep that
                # state; the provider's answer only refreshes what we show.
                phase = "terminating" if current.state == STOPPING else "stop not confirmed"
                updated = updated.replace(
                    state=current.state, stop_error=current.stop_error,
                    stop_attempts=current.stop_attempts,
                    detail=f"{phase}; Runpod still reports the pod as {status.lower() or 'present'}",
                )
            self._store.save(updated)
        return updated

    def status(self) -> Session | None:
        """The recorded session, refreshed from Runpod when it answers.

        When Runpod cannot be asked, the recorded session is returned with
        `observe_error` set and `observed_at` left at the last real answer.
        It is still ours, and still billing, until something confirms otherwise.
        """
        session = self._store.load()
        if session is None or session.provisioner != self.id:
            return None
        try:
            return self._refresh(session)
        except ProvisionerError as exc:
            return session.replace(observe_error=str(exc))

    def adopt(self) -> Session | None:
        """What a previous process left running, if anything.

        Only a session this app recorded counts. A pod that merely looks like
        ours by name is not ours to terminate.
        """
        return self.status()

    def stop(self) -> None:
        session = self._store.load()
        if session is None or session.provisioner != self.id:
            return
        if session.state in STOP_STATES:
            # A previous DELETE may have landed even though its answer did not.
            # Ask before repeating it; a 404 here is the confirmation we lacked.
            try:
                pod = self._request("GET", f"/pods/{session.id}")
            except ProvisionerError as exc:
                self._record_stop(session, exc)
                raise self._stop_error(session, exc) from exc
            if pod is None or str(pod.get("status") or "").upper() in _GONE:
                self._clear(session, "confirmed gone when reconciled")
                return
        attempt = session.replace(
            state=STOPPING, detail="terminating", stop_error="",
            stop_attempts=session.stop_attempts + 1,
        )
        with _LOCK:
            self._store.save(attempt)
        try:
            # Terminate, never stop: a stopped pod releases the GPU without
            # guaranteeing it back, and keeps billing any attached volume.
            self._request("DELETE", f"/pods/{session.id}")
        except ProvisionerError as exc:
            self._record_stop(attempt, exc)
            raise self._stop_error(attempt, exc) from exc
        # A 2xx is Runpod accepting the termination; a 404 (None) is a pod that
        # is already gone. Either way nothing of ours is left to bill.
        self._clear(session, "terminated")

    def forget(self, session_id: str) -> bool:
        with _LOCK:
            current = self._store.load()
            if current is None or current.provisioner != self.id or current.id != session_id:
                return False
            self._store.clear()
        logger.warning("forgot runpod pod %s at the user's request; the app can no longer "
                       "stop it", session_id)
        return True

    # ---- stop bookkeeping ----------------------------------------------------
    def _clear(self, session: Session, how: str) -> None:
        with _LOCK:
            current = self._store.load()
            if current is not None and current.id == session.id:
                self._store.clear()
        logger.info("runpod pod %s %s", session.id, how)

    def _record_stop(self, session: Session, exc: ProvisionerError) -> None:
        state = STOP_UNCONFIRMED if exc.uncertain else STOP_FAILED
        detail = ("termination was not confirmed; the pod may still be running"
                  if exc.uncertain else "Runpod refused the termination; the pod is still running")
        with _LOCK:
            current = self._store.load()
            if current is None or current.id != session.id:
                return
            self._store.save(session.replace(state=state, detail=detail, stop_error=str(exc)))
        logger.error("stopping runpod pod %s did not complete (%s): %s", session.id, state, exc)

    def _stop_error(self, session: Session, exc: ProvisionerError) -> ProvisionerError:
        code = "stop_unconfirmed" if exc.uncertain else "stop_failed"
        lead = ("Runpod did not confirm that it terminated pod"
                if exc.uncertain else "Runpod did not terminate pod")
        return ProvisionerError(
            f"{lead} {session.id}, so it may still be billing. The session is kept: retry "
            f"Stop, which checks with Runpod first. ({exc})",
            uncertain=exc.uncertain, status=exc.status, code=code,
        )


_ = Provisioner  # documents the interface this class is written against
