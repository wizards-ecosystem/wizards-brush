"""Terminate this rental when nobody is listening any more.

The app stops the pod when it shuts down cleanly. A SIGKILL, an OOM kill, a
crashed browser host or a laptop that sleeps and never wakes run no shutdown
code at all — and the GPU keeps billing at an hourly rate with nobody watching.
So the worker also watches for silence and ends itself.

What counts as "listening" is already in the protocol and costs nothing to
observe: the client polls `/result/{token}` every few seconds for the whole of a
render, and re-checks `/health` whenever its last answer is older than 15s. Any
authenticated request is a heartbeat. A long Wan video is therefore covered by
the polling that a long Wan video already does.

Two independent limits, because they fail differently:

* **idle** — nothing has called for N minutes. The normal case.
* **max session** — a hard ceiling regardless of traffic, so a wedged client
  looping on /health cannot hold a GPU open indefinitely.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

from . import runtime

CHECK_INTERVAL_S = 30.0

_LAST_SEEN = time.monotonic()
_LOCK = threading.Lock()


def touch() -> None:
    """Record an authenticated request. Cheap enough for every call."""
    global _LAST_SEEN
    with _LOCK:
        _LAST_SEEN = time.monotonic()


def idle_seconds() -> float:
    with _LOCK:
        return time.monotonic() - _LAST_SEEN


def _terminate_self() -> bool:
    """Ask Runpod to delete this pod. Returns whether the call was made."""
    import httpx

    pod_id = runtime.runpod_pod_id()
    key = runtime.runpod_api_key()
    if not pod_id or not key:
        print("[watchdog] no pod id or API key available; cannot self-terminate. "
              "This pod will keep billing until it is stopped elsewhere.", flush=True)
        return False
    try:
        httpx.delete(f"https://api.runpod.io/v2/pods/{pod_id}",
                     headers={"Authorization": f"Bearer {key}"}, timeout=30.0)
    except Exception as exc:  # noqa: BLE001 — last act of the process; report and stop
        print(f"[watchdog] self-terminate failed: {type(exc).__name__}", flush=True)
        return False
    return True


def decide(*, elapsed_s: float, quiet_s: float, busy: bool,
           idle_limit_s: float, max_session_s: float) -> str:
    """Why this pod should end now, or "" to keep running.

    Pure, so the policy can be tested without waiting out a sleep loop — which
    is the only reason anyone would otherwise trust it by reading it.
    """
    # The ceiling wins over everything, including work in flight: its whole
    # purpose is to bound a client that never stops asking.
    if max_session_s and elapsed_s >= max_session_s:
        return "max_session"
    if not idle_limit_s:
        return ""
    # Never pull the floor out from under a running render. A job in flight is
    # activity even when its client has gone quiet.
    if busy:
        return ""
    return "idle" if quiet_s >= idle_limit_s else ""


def _run(idle_limit_s: float, max_session_s: float, busy: Callable[[], bool]) -> None:
    started = time.monotonic()
    while True:
        time.sleep(CHECK_INTERVAL_S)
        working = busy()
        if working:
            touch()
        reason = decide(
            elapsed_s=time.monotonic() - started, quiet_s=idle_seconds(),
            busy=working, idle_limit_s=idle_limit_s, max_session_s=max_session_s,
        )
        if not reason:
            continue
        if reason == "max_session":
            print(f"[watchdog] max session of {max_session_s / 3600:.1f}h reached; "
                  "terminating this pod.", flush=True)
        else:
            print(f"[watchdog] no authenticated request for {idle_seconds() / 60:.0f} min; "
                  "terminating this pod to stop billing.", flush=True)
        _terminate_self()
        return


def start(busy: Callable[[], bool]) -> None:
    """Arm the switch from the deployment's configuration.

    Off unless configured, because a worker on hardware the operator owns has no
    business deleting anything.
    """
    idle_limit_s = _minutes("REMOTE_GPU_IDLE_TERMINATE_MIN") * 60
    max_session_s = _minutes("REMOTE_GPU_MAX_SESSION_HOURS") * 3600
    if not idle_limit_s and not max_session_s:
        return
    if not runtime.runpod_pod_id():
        print("[watchdog] idle termination is configured but this is not a Runpod "
              "pod; the switch stays off.", flush=True)
        return
    print(f"[watchdog] armed: idle {idle_limit_s / 60:.0f} min, "
          f"max session {max_session_s / 3600:.1f} h", flush=True)
    threading.Thread(target=_run, args=(idle_limit_s, max_session_s, busy),
                     daemon=True, name="watchdog").start()


def _minutes(name: str) -> float:
    import os

    raw = os.environ.get(name, "").strip()
    try:
        return max(0.0, float(raw)) if raw else 0.0
    except ValueError:
        return 0.0
