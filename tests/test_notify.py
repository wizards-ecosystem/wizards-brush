"""Queue notifications, and the debounce that makes 'finished' mean something.

Without it, "the queue is empty" is true for a moment between every pair of jobs
and you get one notification per image.
"""
from __future__ import annotations

import time

import pytest

from backend.app.notify import QueueWatcher


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have been POSTed, and make every hook 'configured'."""
    out: list[tuple[str, dict]] = []
    monkeypatch.setattr("backend.app.notify._url_for", lambda event: "http://hook.test")
    monkeypatch.setattr("backend.app.notify._post",
                        lambda url, body: out.append((body["event"], body)))
    # _send spawns a thread per delivery; run it inline so tests stay deterministic.
    monkeypatch.setattr(
        QueueWatcher, "_send",
        lambda self, event, payload: out.append((event, {"event": event, **payload})))
    return out


def _finished(w, job_id=1, status="done", kind="image_local"):
    w.job_finished(job_id=job_id, status=status, kind=kind)


def test_the_first_job_announces_that_work_started(sent):
    w = QueueWatcher(idle_seconds=10)
    try:
        w.job_started()
        assert [e for e, _ in sent] == ["queue_start"]
    finally:
        w.shutdown()


def test_a_second_job_does_not_re_announce_the_start(sent):
    w = QueueWatcher(idle_seconds=10)
    try:
        w.job_started()
        _finished(w)
        w.job_started()
        assert [e for e, _ in sent].count("queue_start") == 1
    finally:
        w.shutdown()


def test_a_gap_between_two_jobs_does_not_look_like_finishing(sent):
    """The whole point. Without the debounce this fires once per image."""
    w = QueueWatcher(idle_seconds=0.3)
    try:
        w.job_started()
        _finished(w, job_id=1)
        time.sleep(0.1)          # shorter than the debounce
        w.job_started()          # more work arrived
        _finished(w, job_id=2)
        time.sleep(0.1)
        assert "queue_idle" not in [e for e, _ in sent]
    finally:
        w.shutdown()


def test_staying_idle_past_the_debounce_reports_finished(sent):
    w = QueueWatcher(idle_seconds=0.2)
    try:
        w.job_started()
        _finished(w)
        time.sleep(0.45)
        events = [e for e, _ in sent]
        assert events.count("queue_idle") == 1
    finally:
        w.shutdown()


def test_the_idle_report_says_how_much_was_done(sent):
    w = QueueWatcher(idle_seconds=0.2)
    try:
        w.job_started()
        for i in range(3):
            _finished(w, job_id=i)
        time.sleep(0.45)
        payload = next(p for e, p in sent if e == "queue_idle")
        assert payload["completed"] == 3
        assert payload["seconds"] >= 0
    finally:
        w.shutdown()


def test_it_only_reports_finished_once_per_busy_period(sent):
    w = QueueWatcher(idle_seconds=0.15)
    try:
        w.job_started()
        _finished(w)
        time.sleep(0.35)
        time.sleep(0.2)
        assert [e for e, _ in sent].count("queue_idle") == 1
    finally:
        w.shutdown()


def test_a_new_busy_period_can_report_again(sent):
    w = QueueWatcher(idle_seconds=0.15)
    try:
        w.job_started()
        _finished(w)
        time.sleep(0.35)
        w.job_started()
        _finished(w)
        time.sleep(0.35)
        assert [e for e, _ in sent].count("queue_idle") == 2
        assert [e for e, _ in sent].count("queue_start") == 2
    finally:
        w.shutdown()


def test_every_job_reports_individually_including_failures(sent):
    w = QueueWatcher(idle_seconds=10)
    try:
        w.job_started()
        _finished(w, job_id=1, status="done")
        _finished(w, job_id=2, status="error")
        done = [p for e, p in sent if e == "job_done"]
        assert [p["status"] for p in done] == ["done", "error"]
    finally:
        w.shutdown()


def test_nothing_is_sent_when_no_hook_is_configured(monkeypatch):
    """Off by default: an unconfigured hook must not build or post anything."""
    posted: list = []
    monkeypatch.setattr("backend.app.notify._url_for", lambda event: "")
    monkeypatch.setattr("backend.app.notify._post",
                        lambda url, body: posted.append(body))
    w = QueueWatcher(idle_seconds=0.1)
    try:
        w.job_started()
        _finished(w)
        time.sleep(0.25)
        assert posted == []
    finally:
        w.shutdown()


def test_shutdown_cancels_a_pending_report(sent):
    w = QueueWatcher(idle_seconds=0.2)
    w.job_started()
    _finished(w)
    w.shutdown()
    time.sleep(0.35)
    assert "queue_idle" not in [e for e, _ in sent]


def test_a_failing_hook_never_reaches_the_caller(monkeypatch):
    """A notification is a courtesy. It must not be able to affect a job."""
    monkeypatch.setattr("backend.app.notify._url_for", lambda event: "http://hook.test")

    def boom(url, body):
        raise RuntimeError("hook exploded")

    monkeypatch.setattr("backend.app.notify.httpx", None, raising=False)
    w = QueueWatcher(idle_seconds=10)
    try:
        w.job_started()   # spawns a real delivery thread against an unroutable host
        _finished(w)
    finally:
        w.shutdown()


def test_configure_adopts_the_configured_debounce():
    """NOTIFY_IDLE_SECONDS is documented in .env.example. The watcher is built at
    import time with a module default, so without configure() the setting reads
    like it works and does nothing."""
    from backend.app import notify
    from backend.app.config import settings

    before = notify.watcher
    original = settings.notify_idle_seconds
    settings.notify_idle_seconds = 42.0
    try:
        notify.configure()
        assert notify.watcher._idle_seconds == 42.0
        assert notify.watcher is not before
    finally:
        settings.notify_idle_seconds = original
        notify.configure()


def test_the_app_calls_configure_on_startup():
    """The wiring itself: a setting nothing reads is a setting that does not exist."""
    import inspect

    from backend.app import main

    assert "notify.configure()" in inspect.getsource(main.lifespan)
