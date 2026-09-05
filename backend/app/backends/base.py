"""What a backend is, and what we are allowed to ask one.

A *backend* is a place a job can run. Today there are two: the local GPU and an
operator-controlled Remote GPU service reached through an authenticated tunnel.
Tomorrow there may be a second remote worker, a machine on the LAN, or a rented
GPU running the same worker.

**Scope: backends that run our code.** Both ends speak our protocol, so we own
the parameter names, the model ids and the feature list. Wrapping somebody
else's service — fal, Replicate, an image API — is a different and much larger
problem, because they own all of those. `catalog()` is the seam where such an
adapter would plug in; nothing else in this interface assumes we control both
sides.

**Two protocols, not one.** `Backend` describes what a place can do. `RemoteBackend`
adds the submit-and-poll lifecycle that only makes sense over a network.

The local backend deliberately does not implement the second. Running a handler
in-process is a function call, and wrapping it in tokens and polling to satisfy
a shared interface would be ceremony with exactly one implementation and no
second consumer. The remote lifecycle is a real abstraction because two remotes
genuinely share it; a local one would not be.

*(This narrows what the plan sketched — it had submit/poll on the base protocol.
Building it that way meant inventing a token for a synchronous call. The split
keeps every method on an interface that at least two implementations want.)*
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class BackendKind(str, Enum):
    local = "local"
    remote = "remote"


@dataclass(frozen=True)
class Health:
    """Whether a backend can take work right now, and what it is holding.

    `connected=False` with an empty `features` means *unknown*, not
    *unsupported*. Callers must fall back to configured capability rather than
    hiding controls the user is about to need — which is the mistake that once
    made the UI offer speed mode against a session whose Lightning LoRA had
    never loaded.
    """
    connected: bool
    reason: str = ""                       # why not, when not connected
    device: str = ""                       # "NVIDIA RTX 4080, 16 GB", "A100 40 GB"
    build: str = ""                        # fingerprint of the code it is running
    build_stale: bool = False              # that build is older than ours
    queue_depth: int = 0
    disk_free_gb: float | None = None
    vram_gb: float | None = None
    features: frozenset[str] = frozenset()
    models_loaded: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        """Whether we have actually heard from this backend."""
        return self.connected or bool(self.features)


@dataclass(frozen=True)
class ModelInfo:
    """One model, as a backend reports it."""
    id: str                                # "Tongyi-MAI/Z-Image-Turbo"
    label: str = ""
    kind: str = "image"                    # image | video
    ready: bool = False                    # weights present and loadable now
    status: str = "absent"                 # ready | partial | absent | unknown
    size_gb: float | None = None
    family: str = "unknown"                # from modelprobe, for LoRA matching


@runtime_checkable
class Backend(Protocol):
    """Somewhere a job can run. Describes itself; does not necessarily run jobs
    over a wire."""

    id: str
    label: str
    kind: BackendKind

    def health(self) -> Health: ...

    def catalog(self) -> list[ModelInfo]:
        """Models this backend can run. The seam for a third-party adapter."""
        ...

    def features(self) -> frozenset[str]:
        """Capability names the UI gates controls on. Empty means unknown."""
        ...

    def has_resource(self, digest: str) -> bool:
        """Whether this backend already holds a file with that content hash.

        The content hash is the identity, not the filename — the same LoRA saved
        under two names is one resource, and never uploaded twice.
        """
        ...


@runtime_checkable
class RemoteBackend(Backend, Protocol):
    """A backend reached over a network, with a job lifecycle to match.

    Implemented by the Remote GPU worker today. A LAN machine or a rented GPU
    running the same service fit without changing anything here.
    """

    def submit(self, path: str, payload: dict[str, Any]) -> str:
        """Enqueue work. Returns a token to poll. Must be idempotent per
        `client_job_id` so a retry after a lost response cannot double-enqueue."""
        ...

    def poll(self, token: str) -> dict[str, Any]:
        """Current state of a submitted job: status, progress, preview, result."""
        ...

    def cancel(self, token: str) -> None:
        """Best effort. A backend that has already gone away is not an error."""
        ...

    def ack(self, token: str) -> None:
        """Confirm the result is safely ours so the backend may release it.

        Separate from `poll` on purpose: the result must survive being read, or
        a dropped response — the exact failure the retry loop exists for — turns
        every later poll into an error.
        """
        ...

    def put_resource(self, digest: str, data: bytes, name: str) -> None:
        """Upload a file this backend needs and does not have."""
        ...
