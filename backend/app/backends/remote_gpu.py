"""The user-operated Remote GPU server as a backend.

Wraps `remote_gpu_client`, which keeps every piece of hardening it has earned:

* an idempotency key, so a retry after a lost response cannot enqueue twice;
* `_PollHiccup` and a miss budget, so a tunnel that drops mid-poll is retried
  rather than reported as a failed job;
* secret redaction on every surfaced error;
* ack-and-TTL rather than free-on-read, because `/result` used to `pop()` and a
  dropped response then turned every later poll into a 500.

None of that moves. This class is the shape the rest of the app talks to, not a
reimplementation.
"""
from __future__ import annotations

from typing import Any

from .. import remote_gpu_client
from ..config import settings
from ..modelprobe import family_of_model, short_name
from .base import BackendKind, Health, ModelInfo, RemoteBackend


class RemoteGPUBackend(RemoteBackend):
    id = "remote_gpu"
    label = "Remote GPU"
    kind = BackendKind.remote

    def health(self) -> Health:
        """Read the last observed health. Never performs I/O.

        `remote_gpu_client.remote_gpu_health()` is async and refreshes the cache; this is
        the synchronous view of it, because the registry is built during a
        request and cannot await. A stale answer is correct behaviour here: the
        alternative is a blocking network call on every page render.
        """
        raw = remote_gpu_client.last_health()
        connected = bool(raw.get("connected"))
        return Health(
            connected=connected,
            reason=str(raw.get("reason") or ""),
            device=str(raw.get("gpu") or ("A100" if connected else "")),
            build=str(raw.get("build") or ""),
            build_stale=bool(raw.get("build_stale")),
            queue_depth=int(raw.get("queue_depth") or 0),
            disk_free_gb=_maybe_float(raw.get("disk_free_gb")),
            features=frozenset(raw.get("features") or ()),
            models_loaded=tuple(raw.get("models_loaded") or ()),
            extra={k: v for k, v in raw.items()
                   if k in ("worker_alive", "speed_loaded", "url")},
        )

    def catalog(self) -> list[ModelInfo]:
        """Models the live session reports, falling back to what is configured.

        The fallback matters: before the first successful health call we know
        nothing, and showing an empty model list would read as "this backend has
        nothing" rather than "we have not asked yet".
        """
        raw = remote_gpu_client.last_health()
        reported = raw.get("models")
        loaded = set(raw.get("models_loaded") or ())
        if isinstance(reported, list) and reported:
            return [_model_info(m, loaded, family_of_model) for m in reported]
        configured = [
            (settings.a100_image_model, "image"),
            (settings.a100_image_model_hidream, "image"),
            (settings.a100_image_model_alt, "image"),
            (settings.qwen_edit_model, "image-edit"),
            (settings.video_model, "video"),
            (settings.hunyuan_video_model, "video"),
            (settings.ltx_video_model, "video"),
        ]
        return [
            ModelInfo(id=mid, label=short_name(mid), kind=kind,
                      ready=mid in loaded, status="ready" if mid in loaded else "unknown",
                      family=family_of_model(mid))
            for mid, kind in configured if mid
        ]

    def features(self) -> frozenset[str]:
        return frozenset(remote_gpu_client.last_features())

    def has_resource(self, digest: str) -> bool:
        """Whether the session already holds this file.

        Unknown resources report False, which routes to an upload. That is the
        safe direction: an unnecessary upload costs a few seconds, while assuming
        a file is present that is not fails the job at generation time.
        """
        return digest in set(remote_gpu_client.last_health().get("resources") or ())

    # ---- RemoteBackend ----------------------------------------------------
    #
    # Stated once, as a constant rather than by reading `submit.__doc__`: a
    # docstring is stripped under `python -OO`, which would turn every message
    # below into `NotImplementedError(None)` in exactly the build where a clear
    # error matters most.
    _WHY_UNSPLIT = (
        "Remote GPU work goes through remote_gpu_client.run_remote, which owns the "
        "submit/poll/retry loop as one unit. Split it only when a second remote "
        "backend exists to share the pieces with."
    )

    def submit(self, path: str, payload: dict[str, Any]) -> str:
        raise NotImplementedError(self._WHY_UNSPLIT)

    def poll(self, token: str) -> dict[str, Any]:
        raise NotImplementedError(self._WHY_UNSPLIT)

    def cancel(self, token: str) -> None:
        raise NotImplementedError(self._WHY_UNSPLIT)

    def ack(self, token: str) -> None:
        raise NotImplementedError(self._WHY_UNSPLIT)

    def put_resource(self, digest: str, data: bytes, name: str) -> None:
        raise NotImplementedError(
            "Uploading a resource to the Remote GPU session needs a receiving endpoint "
            "in remote_gpu.py, which does not exist yet. Until it does, a job whose "
            "inputs the session does not already hold is refused before it is "
            "queued rather than failing on the A100."
        )


def _model_info(raw: Any, loaded: set[str], family_of) -> ModelInfo:
    if isinstance(raw, str):
        ready = raw in loaded
        return ModelInfo(id=raw, label=short_name(raw), ready=ready,
                         status="ready" if ready else "unknown",
                         family=family_of(raw))
    mid = str(raw.get("id") or "")
    return ModelInfo(
        id=mid, label=str(raw.get("label") or short_name(mid)),
        kind=str(raw.get("kind") or "image"),
        ready=bool(raw.get("ready", mid in loaded)),
        status=str(raw.get("status") or
                   ("ready" if bool(raw.get("ready", mid in loaded)) else "unknown")),
        size_gb=_maybe_float(raw.get("size_gb")), family=family_of(mid),
    )


def _maybe_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
