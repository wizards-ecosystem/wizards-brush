"""What a provisioner is: something that can make remote GPU hardware exist.

Distinct from `backends/`, which talks to compute that already exists. A backend
answers *"can this place take work right now"*; a provisioner answers *"can I
make a place"*. Keeping them apart is what lets the default stay `manual`: you
run the worker yourself and the app never touches your money.

**Every implementation spends money.** That shapes the interface more than
anything else here:

* `start()` is only ever called from an explicit user action. Nothing in the app
  may provision on a timer, on launch, or to satisfy a queued job.
* a started session is written to runtime settings *before* it is returned, so a
  crash between "created" and "recorded" cannot orphan a billing resource the
  app no longer knows about.
* `adopt()` exists for the same reason: on startup the app asks each provisioner
  what it already owns, because the previous process may have died holding one.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ProvisionerError(RuntimeError):
    """A provisioner could not do what was asked.

    The message reaches the user, so it says what to do about it and never
    contains the API key.

    `uncertain` is true when the request may have reached the provider without
    its answer reaching us (a read timeout, a dropped connection, a gateway
    error): the action might have happened. `code` is stable for programs.
    """

    def __init__(self, message: str, *, uncertain: bool = False, status: int | None = None,
                 code: str = "provider_error") -> None:
        super().__init__(message)
        self.uncertain = uncertain
        self.status = status
        self.code = code


# Progress is a coarse state machine rather than a percentage: the phases have
# genuinely different durations (an image pull is minutes, a model load is
# minutes, booting is seconds) and a fake percentage across them would lie.
STARTING = "starting"
READY = "ready"
STOPPING = "stopping"
ERROR = "error"
# A stop whose outcome is unknown (a timeout, a dropped connection) and one the
# provider refused (401/403/5xx). Both keep the session: the hardware may still
# exist and bill, and the recorded id is the only handle that can stop it. Only
# a confirmed termination or the user explicitly forgetting it clears them.
STOP_UNCONFIRMED = "stop_unconfirmed"
STOP_FAILED = "stop_failed"
STOP_STATES = frozenset({STOPPING, STOP_UNCONFIRMED, STOP_FAILED})


@dataclass(frozen=True)
class Session:
    """One running rental, as the app understands it."""

    id: str                              # the provider's handle, e.g. a pod id
    provisioner: str                     # which implementation owns it
    state: str = STARTING
    base_url: str = ""                   # authenticated HTTPS, once it exists
    detail: str = ""                     # human progress line for the UI
    started_at: float = 0.0              # unix seconds, for elapsed and cost
    hourly_usd: float | None = None
    gpu: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    # Why the last stop did not complete, and how many were attempted. Kept
    # with the session so a restart can show and retry it.
    stop_error: str = ""
    stop_attempts: int = 0
    # The last time the provider answered about this session, and why the
    # latest attempt to ask did not get an answer. A UI shows the age of what
    # it knows instead of presenting a stale snapshot as current.
    observed_at: float = 0.0
    observe_error: str = ""

    @property
    def unresolved_stop(self) -> bool:
        """A stop was asked for and not confirmed: ownership is still ours."""
        return self.state in (STOP_UNCONFIRMED, STOP_FAILED)

    def replace(self, **changes: Any) -> Session:
        from dataclasses import replace

        return replace(self, **changes)

    def elapsed_s(self, now: float | None = None) -> float:
        if not self.started_at:
            return 0.0
        return max(0.0, (now if now is not None else time.time()) - self.started_at)

    def cost_estimate_usd(self, now: float | None = None) -> float | None:
        """Rough spend so far. None when the rate is unknown - never a guess.

        Deliberately an estimate and labelled as one: the provider bills per
        second from its own clock, and storage is not in this number.
        """
        if self.hourly_usd is None:
            return None
        return round(self.hourly_usd * self.elapsed_s(now) / 3600.0, 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provisioner": self.provisioner,
            "state": self.state,
            "base_url": self.base_url,
            "detail": self.detail,
            "started_at": self.started_at,
            "hourly_usd": self.hourly_usd,
            "gpu": self.gpu,
            "extra": dict(self.extra),
            "stop_error": self.stop_error,
            "stop_attempts": self.stop_attempts,
            "observed_at": self.observed_at,
            "observe_error": self.observe_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        return cls(
            id=str(data.get("id", "")),
            provisioner=str(data.get("provisioner", "")),
            state=str(data.get("state", STARTING)),
            base_url=str(data.get("base_url", "")),
            detail=str(data.get("detail", "")),
            started_at=float(data.get("started_at") or 0.0),
            hourly_usd=(float(data["hourly_usd"])
                        if data.get("hourly_usd") is not None else None),
            gpu=str(data.get("gpu", "")),
            extra=dict(data.get("extra") or {}),
            stop_error=str(data.get("stop_error") or ""),
            stop_attempts=int(data.get("stop_attempts") or 0),
            observed_at=float(data.get("observed_at") or 0.0),
            observe_error=str(data.get("observe_error") or ""),
        )


@runtime_checkable
class Provisioner(Protocol):
    """Small on purpose: a new provider should be an afternoon, not a project."""

    id: str
    label: str

    def configured(self) -> bool:
        """Whether there is enough configuration to try. No network calls."""

    def start(self) -> Session:
        """Create hardware and return as soon as it has an id. Never blocks
        until ready - the caller polls `status()` so the UI can show progress."""

    def status(self) -> Session | None:
        """The session this provisioner currently owns, refreshed. None when
        nothing is running."""

    def stop(self) -> None:
        """Release the hardware. Must be safe to call when nothing is running,
        because it is called on shutdown without checking first.

        Clears the recorded session only once the provider confirms the
        hardware is gone. Otherwise it records STOP_UNCONFIRMED or STOP_FAILED
        and raises, so the handle survives for a retry or a restart."""

    def adopt(self) -> Session | None:
        """Re-attach to a session a previous process left behind."""

    def forget(self, session_id: str) -> bool:
        """Drop the recorded session without contacting the provider.

        Only for an explicit user decision, with the id they confirmed: after
        this the app can no longer stop that hardware. False when the id does
        not match what is recorded."""
