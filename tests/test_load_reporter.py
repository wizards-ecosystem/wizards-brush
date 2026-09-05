"""LoadReporter: named stages, a heartbeat that proves liveness, and the
guarantee that reporting can never break a load."""
from __future__ import annotations

import time

from backend.app.generators.base import LoadReporter


def _collector():
    seen: list[tuple[str, str, dict]] = []
    return seen, lambda stage, detail, extra: seen.append((stage, detail, extra))


def test_stage_emits_immediately():
    seen, emit = _collector()
    with LoadReporter(emit, interval=60):   # heartbeat will not fire
        LoadReporter(emit, interval=60).stage("resolving", "some/model")
    assert seen == [("resolving", "some/model", {"beat": False})]


def test_stages_are_reported_in_order():
    seen, emit = _collector()
    rep = LoadReporter(emit, interval=60)
    for name in ("swapping", "downloading", "quantizing", "placing", "ready"):
        rep.stage(name)
    assert [s for s, _, _ in seen] == ["swapping", "downloading", "quantizing",
                                       "placing", "ready"]


def test_heartbeat_repeats_the_current_stage():
    """The message does not change; the arrival does. That is what lets the UI
    tell 'slow' apart from 'hung' without inventing a percentage."""
    seen, emit = _collector()
    with LoadReporter(emit, interval=0.05) as rep:
        rep.stage("downloading", "8.4 GB")
        time.sleep(0.22)
    beats = [e for s, d, e in seen if s == "downloading" and e["beat"]]
    assert len(beats) >= 2, f"expected repeated heartbeats, got {seen}"
    assert all(d == "8.4 GB" for s, d, _ in seen), "heartbeat must not invent detail"


def test_heartbeat_follows_a_stage_change():
    seen, emit = _collector()
    with LoadReporter(emit, interval=0.05) as rep:
        rep.stage("downloading")
        time.sleep(0.12)
        rep.stage("placing")
        time.sleep(0.12)
    tail = [s for s, _, e in seen if e["beat"]]
    assert tail[-1] == "placing", "heartbeat must track the latest stage"


def test_heartbeat_stops_on_exit():
    seen, emit = _collector()
    with LoadReporter(emit, interval=0.05) as rep:
        rep.stage("downloading")
        time.sleep(0.12)
    count = len(seen)
    time.sleep(0.15)
    assert len(seen) == count, "no events after the load finished"


def test_no_events_before_a_stage_is_set():
    seen, emit = _collector()
    with LoadReporter(emit, interval=0.05):
        time.sleep(0.12)
    assert seen == [], "an idle reporter must stay silent"


def test_a_raising_sink_never_breaks_the_load():
    """Telemetry is not allowed to fail a model load."""
    def boom(stage, detail, extra):
        raise RuntimeError("websocket died")

    with LoadReporter(boom, interval=0.05) as rep:
        rep.stage("downloading")   # must not raise
        time.sleep(0.12)           # nor must the heartbeat thread


def test_null_reporter_is_a_no_op():
    """The default when nothing is listening — used by warm-up and by tests."""
    with LoadReporter(None, interval=0.01) as rep:
        rep.stage("downloading")


def test_queue_reporter_emits_model_load_events():
    from backend.app import queue

    got: list[dict] = []
    original = queue.hub.emit

    def capture(event: dict) -> None:
        got.append(event)

    queue.hub.emit = capture  # type: ignore[method-assign]
    try:
        rep = queue.load_reporter(42, "image_local")
        rep.stage("swapping", "Turbo → Quality")
    finally:
        queue.hub.emit = original  # type: ignore[method-assign]
    assert got == [{"type": "model_load", "id": 42, "kind": "image_local",
                    "stage": "swapping", "detail": "Turbo → Quality", "beat": False}]
    assert got[0]["type"] != "job", "a load is not job progress and must not move the bar"


def test_the_heartbeat_stops_once_the_load_is_ready():
    """`generate()` holds the reporter open for the WHOLE generation, not just
    the load. A heartbeat that kept beating after "ready" would emit a
    model_load event every couple of seconds for the entire job — and those
    events carry no `status`, so ProgressHub treats them as must-deliver state
    transitions and they would evict droppable preview frames under backlog."""
    import time

    got: list[tuple] = []
    with LoadReporter(lambda s, d, x: got.append((s, x.get("beat"))),
                      interval=0.02) as rep:
        rep.stage("loading", "bf16 weights")
        time.sleep(0.1)
        assert any(beat for _s, beat in got), "it should beat while loading"
        rep.stage("ready", "done")
        got.clear()
        time.sleep(0.1)
    assert got == [], f"nothing may be emitted after ready, got {got}"


def test_ready_is_still_reported_once():
    got: list[str] = []
    rep = LoadReporter(lambda s, d, x: got.append(s), interval=60)
    rep.stage("loading", "weights")
    rep.stage("ready", "done")
    assert got == ["loading", "ready"]


def test_the_hub_treats_a_model_load_event_as_droppable():
    """It is telemetry on a timer, not a state transition a client must not miss."""
    from backend.app import queue

    hub = queue.ProgressHub()
    assert hub._droppable({"type": "model_load", "stage": "loading"}) is True
    assert hub._droppable({"type": "job", "status": "running"}) is True
    assert hub._droppable({"__binary__": b"x"}) is True
    assert hub._droppable({"type": "job", "status": "done"}) is False
