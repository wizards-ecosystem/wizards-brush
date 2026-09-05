"""Telling you when something finished, without being told twice.

The feature people actually want from a generation queue is "let me know when the
batch is done so I can stop watching it". The naive version — fire whenever the
queue is empty — is useless, because the queue is momentarily empty between every
pair of jobs and you get a notification per image.

So the rule is: the queue must stay idle for `NOTIFY_IDLE_SECONDS` before it
counts as finished. That debounce is the whole design, not a detail.

Three hooks, all optional and all off unless a URL is configured:

    queue_start   the first job after a period of idleness began
    queue_idle    the queue drained and stayed drained
    job_done      one job finished (noisy; for scripting rather than for people)

The body is JSON. Anything that accepts a POST works — ntfy, a Discord webhook, a
Home Assistant automation, a shell script behind a one-line server.

Adapted in design from SwarmUI's WebhookManager (MIT).
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import log
from .config import settings

logger = log.get("notify")

# The queue must be continuously idle for this long before it counts as finished.
DEFAULT_IDLE_SECONDS = 5.0


class QueueWatcher:
    """Tracks busy/idle transitions and fires the hooks.

    Deliberately not driven by "is the pending dict empty" alone: that is true
    for a moment between every pair of jobs. A timer started on the transition to
    idle, and cancelled if work arrives before it fires, is what turns "empty" into
    "finished".
    """

    def __init__(self, idle_seconds: float = DEFAULT_IDLE_SECONDS) -> None:
        self._idle_seconds = idle_seconds
        self._busy = False
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._started_at: float | None = None
        self._completed = 0

    def job_started(self) -> None:
        """Called when a lane picks up work."""
        fire_start = False
        with self._lock:
            self._cancel_timer()
            if not self._busy:
                self._busy = True
                self._started_at = time.monotonic()
                self._completed = 0
                fire_start = True
        if fire_start:
            self._send("queue_start", {})

    def job_finished(self, *, job_id: int, status: str, kind: str) -> None:
        """Called when a lane finishes work, whatever the outcome."""
        with self._lock:
            self._completed += 1
            self._cancel_timer()
            # Arm the idle timer. If another job starts first, job_started
            # cancels it and nothing is sent.
            self._timer = threading.Timer(self._idle_seconds, self._on_idle)
            self._timer.daemon = True
            self._timer.start()
        self._send("job_done", {"job_id": job_id, "status": status, "kind": kind})

    def _on_idle(self) -> None:
        with self._lock:
            if not self._busy:
                return
            self._busy = False
            elapsed = time.monotonic() - (self._started_at or time.monotonic())
            completed = self._completed
            self._timer = None
        self._send("queue_idle", {"completed": completed,
                                  "seconds": round(elapsed, 1)})

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def shutdown(self) -> None:
        with self._lock:
            self._cancel_timer()
            self._busy = False

    # ---- delivery --------------------------------------------------------
    def _send(self, event: str, payload: dict[str, Any]) -> None:
        url = _url_for(event)
        if not url:
            return
        body = {"event": event, "app": "wizards-brush", **payload}
        threading.Thread(target=_post, args=(url, body), daemon=True,
                         name=f"notify-{event}").start()


def _url_for(event: str) -> str:
    return {
        "queue_start": settings.notify_queue_start_url,
        "queue_idle": settings.notify_queue_idle_url,
        "job_done": settings.notify_job_done_url,
    }.get(event, "")


def _post(url: str, body: dict[str, Any]) -> None:
    """Fire and forget. A notification that fails must never affect a job."""
    try:
        import httpx

        with httpx.Client(timeout=10) as c:
            c.post(url, json=body)
    except Exception as e:  # noqa: BLE001 — a hook is a courtesy, never a dependency
        logger.warning("notification %s failed: %s", body.get("event"), e)


watcher = QueueWatcher()


def configure(idle_seconds: float | None = None) -> None:
    """Rebuild the watcher with the configured debounce."""
    global watcher
    watcher.shutdown()
    watcher = QueueWatcher(idle_seconds if idle_seconds is not None
                           else settings.notify_idle_seconds)
