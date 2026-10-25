"""Where work can run: capability reports, a capability revision, and the
routing policy that decides what Auto may do.

A capability report describes one target as this app knows it right now:
identity, hardware (with how confident that is), the operations and kinds it
offers, the model slots it is configured with and whether each is installed,
adapter support, finishing processors, and runtime versions. Advertised
support (configuration, a worker's feature flags) and measured readiness (a
completed device probe, a downloaded snapshot) are separate fields. Unknown is
reported as unknown, never as ready.

The capability revision is a short digest over the parts of both reports that
change what can run. A client keeps it with its controls and refreshes them
when it changes; a plan carries the revision it was resolved against.

The routing policy is the user's: whether remote use is allowed at all,
whether Auto may route there, and whether a request's images and masks may be
sent to a remote worker without that request saying so. Nothing here, or in
any resolve or generate path, starts or rents hardware.
"""
from __future__ import annotations

import time
from typing import Any

from ..config import settings
from . import contracts

POLICY_KEY = "routing_policy"
DEFAULT_POLICY: dict[str, bool] = {
    # Any remote target at all. Off means only this machine runs work.
    "remote_enabled": True,
    # Whether Auto may choose a connected remote worker when this machine
    # cannot run a request. A request can also allow it for itself.
    "auto_may_use_remote": False,
    # Whether a request's images, masks and references may be sent to a remote
    # worker without the request explicitly allowing it.
    "send_inputs_to_remote": False,
}


def policy() -> dict[str, bool]:
    stored = settings.load_overrides().get(POLICY_KEY)
    out = dict(DEFAULT_POLICY)
    if isinstance(stored, dict):
        for key in DEFAULT_POLICY:
            if isinstance(stored.get(key), bool):
                out[key] = stored[key]
    return out


def save_policy(changes: dict[str, Any]) -> dict[str, bool]:
    from ..problems import RequestError

    for key, value in changes.items():
        if key not in DEFAULT_POLICY:
            raise RequestError(400, f"'{key}' is not a routing policy setting",
                               code="unknown_setting", field=key,
                               valid_options=list(DEFAULT_POLICY))
        if not isinstance(value, bool):
            raise RequestError(400, f"{key} must be true or false", code="invalid_type",
                               field=key)
    merged = {**policy(), **changes}
    data = settings.load_overrides()
    data[POLICY_KEY] = merged
    settings.save_overrides(data)
    return merged


def _versions() -> dict[str, str]:
    from importlib import metadata

    out: dict[str, str] = {}
    for package in ("torch", "diffusers", "transformers", "peft"):
        try:
            out[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            out[package] = ""
    return out


def _kinds_for(target: str, specs: dict[str, Any]) -> tuple[list[str], list[str]]:
    operations, kinds = [], []
    for (operation, where), kind in contracts.KIND_FOR.items():
        if where == target and kind in specs:
            operations.append(operation)
            kinds.append(kind)
    return sorted(set(operations)), sorted(set(kinds))


def local_report(specs: dict[str, Any] | None = None) -> dict[str, Any]:
    from .. import hardware
    from ..backends.local import _cache_status, unavailable_reason
    from ..finishing import describe as processors
    from ..resources import catalogue, store
    from ..variant_sets.operations import registry_specs

    live = registry_specs() if specs is None else specs
    hw = hardware.describe()
    operations, kinds = _kinds_for("local", live)
    loras_offered = {kind for kind, spec in live.items()
                     if any(c.get("name") == "loras" for c in spec.get("controls", []))}
    models = []
    for slot in catalogue.configured_slots():
        if slot.lane != "local":
            continue
        state, size_gb = _cache_status(slot.repo)
        models.append({"resource": catalogue.model_resource_id(slot.repo), "repo": slot.repo,
                       "variant": slot.variant, "role": slot.role,
                       "installed": state == "ready", "install_state": state,
                       "size_gb": size_gb})
    adapters = [r for r in store.listing("adapter") if r.state in {"installed", "verified"}]
    vram = hw.get("vram_gb")
    return {
        "target": "local",
        "identity": "local",
        "protocol": "in-process",
        "hardware": {"device": hw.get("device"), "vram_gb": vram, "profile": hw.get("profile"),
                     "host_ram": hw.get("host_ram"), "quant": hw.get("quant"),
                     "offload": hw.get("offload")},
        "confidence": "measured" if vram is not None else "unknown",
        "unavailable_reason": unavailable_reason(),
        "operations": operations,
        "kinds": kinds,
        "models": models,
        "adapters": {"formats": ["safetensors"], "installed": len(adapters),
                     "kinds_that_apply_them": sorted(loras_offered & set(kinds))},
        "processors": [{"name": p["name"], "available": bool(p.get("available", True))}
                       for p in processors()],
        "runtime": _versions(),
        "observed_at": time.time(),
    }


def remote_report(specs: dict[str, Any] | None = None,
                  connected: bool | None = None) -> dict[str, Any]:
    from ..remote_gpu_client import last_health
    from ..resources import catalogue
    from ..variant_sets.operations import registry_specs

    live = registry_specs() if specs is None else specs
    health = last_health()
    up = bool(health.get("connected")) if connected is None else connected
    operations, kinds = _kinds_for("remote", live)
    models = [{"resource": catalogue.model_resource_id(slot.repo), "repo": slot.repo,
               "variant": slot.variant, "role": slot.role,
               # The worker downloads lazily; the app only knows it is configured.
               "installed": None, "install_state": "advertised"}
              for slot in catalogue.configured_slots() if slot.lane == "remote"]
    build = str(health.get("build") or "")
    return {
        "target": "remote",
        "identity": f"remote:{build}" if build else "remote",
        "protocol": "worker-http/1",
        "connected": up,
        "hardware": {"gpu": health.get("gpu"), "vram_gb": health.get("vram_gb"),
                     "disk_free_gb": health.get("disk_free_gb")},
        "confidence": "advertised" if up else "unknown",
        "features": sorted(health.get("features") or []),
        "worker_alive": health.get("worker_alive"),
        "queue_depth": health.get("queue_depth"),
        "operations": operations if up else [],
        "configured_operations": operations,
        "kinds": kinds,
        "models": models,
        "adapters": {"formats": [], "note": "the worker does not apply user adapters yet"},
        "runtime": {"build": build},
        "observed_at": time.time(),
    }


def _stable(report: dict[str, Any]) -> dict[str, Any]:
    """The parts of a report that change what can run (not free memory or clocks)."""
    hardware = dict(report.get("hardware") or {})
    hardware.pop("host_ram", None)
    hardware.pop("disk_free_gb", None)
    return {k: v for k, v in {**report, "hardware": hardware}.items()
            if k not in {"observed_at", "queue_depth", "worker_alive"}}


def capability_revision(local: dict[str, Any], remote: dict[str, Any],
                        current_policy: dict[str, bool] | None = None) -> str:
    from ..resources import store

    adapters = sorted(r.id for r in store.listing("adapter") if r.state in {"installed",
                                                                           "verified"})
    return contracts.digest({"local": _stable(local), "remote": _stable(remote),
                             "adapters": adapters, "policy": current_policy or policy()},
                            domain="wizards-brush/capabilities")[7:23]


def reports(connected: bool | None = None) -> dict[str, Any]:
    from ..variant_sets.operations import registry_specs

    specs = registry_specs()
    local = local_report(specs)
    remote = remote_report(specs, connected)
    current = policy()
    return {"local": local, "remote": remote, "policy": current,
            "capability_revision": capability_revision(local, remote, current)}
