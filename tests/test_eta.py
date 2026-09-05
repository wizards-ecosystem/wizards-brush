"""Queue estimates: honest about what they know, and never a confident guess."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.app import db, eta
from backend.app.models import Job, JobStatus


def _params(w=1024, h=1024, steps=20, batch=1, **kw):
    return {"width": w, "height": h, "steps": steps, "batch": batch, **kw}


# ---- the cost model -------------------------------------------------------
def test_cost_grows_faster_than_area():
    """Attention is super-linear in sequence length, so doubling the area more
    than doubles the work. A linear model badly underestimates large images."""
    small = eta.cost_of(_params(512, 512))
    double_area = eta.cost_of(_params(724, 724))     # ~2x the pixels
    assert double_area > 2 * small


def test_cost_is_linear_in_steps_and_batch():
    base = eta.cost_of(_params(steps=10, batch=1))
    assert eta.cost_of(_params(steps=20, batch=1)) == pytest.approx(2 * base)
    assert eta.cost_of(_params(steps=10, batch=3)) == pytest.approx(3 * base)


def test_video_scales_with_frame_count():
    still = eta.cost_of(_params())
    clip = eta.cost_of(_params(num_frames=49))
    assert clip == pytest.approx(49 * still)


def test_missing_params_do_not_produce_zero_or_a_crash():
    assert eta.cost_of({}) > 0


# ---- honesty --------------------------------------------------------------
def test_no_history_means_no_number(client, monkeypatch):
    monkeypatch.setattr(eta, "rate_for", lambda kind, samples=20: (None, 0))
    est = eta.for_queued(Job(kind="image_local", params_json="{}"))
    assert est.seconds is None
    assert est.confidence == "unknown"
    assert not est.known


def test_some_history_but_not_enough_says_estimating(client, monkeypatch):
    """A confident wrong number is worse than admitting ignorance."""
    monkeypatch.setattr(eta, "rate_for", lambda kind, samples=20: (None, 2))
    assert eta.for_queued(Job(kind="image_local", params_json="{}")).confidence == "estimating"


def test_enough_history_produces_an_estimate(client, monkeypatch):
    monkeypatch.setattr(eta, "rate_for", lambda kind, samples=20: (10.0, 5))
    est = eta.for_queued(Job(kind="image_local", params_json="{}"))
    assert est.seconds and est.seconds > 0
    assert est.confidence == "estimated"


# ---- the model swap -------------------------------------------------------
def test_a_needed_swap_is_added_to_the_estimate(client, monkeypatch):
    """An estimate that ignores a 90-second reload is a promise that breaks."""
    monkeypatch.setattr(eta, "rate_for", lambda kind, samples=20: (1.0, 5))
    job = Job(kind="image_local", params_json="{}", model_key="quality")
    warm = eta.for_queued(job, resident_model="quality")
    cold = eta.for_queued(job, resident_model="turbo")
    assert cold.seconds - warm.seconds == pytest.approx(eta.swap_seconds())
    assert cold.includes_swap and not warm.includes_swap


def test_a_queue_counts_each_swap_once(client, monkeypatch):
    """Three jobs on the same model pay for one load, not three."""
    monkeypatch.setattr(eta, "rate_for", lambda kind, samples=20: (0.0, 5))
    jobs = [Job(kind="image_local", params_json="{}", model_key="quality", id=i)
            for i in range(3)]
    total = eta.queue_total(jobs, resident_model="turbo")
    assert total.seconds == pytest.approx(eta.swap_seconds())


def test_a_queue_alternating_models_pays_for_each_change(client, monkeypatch):
    monkeypatch.setattr(eta, "rate_for", lambda kind, samples=20: (0.0, 5))
    jobs = [Job(kind="image_local", params_json="{}", model_key=m, id=i)
            for i, m in enumerate(["quality", "turbo", "quality"])]
    total = eta.queue_total(jobs, resident_model="turbo")
    assert total.seconds == pytest.approx(3 * eta.swap_seconds())


# ---- the running job ------------------------------------------------------
def test_a_running_job_is_measured_not_predicted(client):
    """Exact, and automatically right on a slow card, because it is watching the
    actual run rather than extrapolating from other jobs."""
    started = datetime.now(UTC) - timedelta(seconds=30)
    job = Job(kind="image_local", params_json="{}", status=JobStatus.running.value,
              progress=0.5, started_at=started)
    est = eta.for_running(job)
    assert est.confidence == "measured"
    assert est.seconds == pytest.approx(30, abs=3), "half done in 30s means ~30s left"


def test_a_barely_started_job_falls_back_to_history(client, monkeypatch):
    """At 0% there is nothing to extrapolate from."""
    monkeypatch.setattr(eta, "rate_for", lambda kind, samples=20: (5.0, 5))
    job = Job(kind="image_local", params_json="{}", status=JobStatus.running.value,
              progress=0.0, started_at=datetime.now(UTC))
    assert eta.for_running(job).confidence == "estimated"


# ---- presentation ---------------------------------------------------------
@pytest.mark.parametrize(("secs", "shown"), [
    (None, ""), (0, "0s"), (45, "45s"), (90, "1m 30s"), (3661, "1h 01m"),
])
def test_durations_read_at_a_glance(secs, shown):
    assert eta.humanize(secs) == shown


def test_the_route_reports_per_lane_and_per_job(client, no_queue):
    db.create_job("image_local", {"prompt": "p", "width": 512, "height": 512})
    r = client.get("/api/jobs/eta")
    assert r.status_code == 200
    lanes = r.json()["lanes"]
    assert {"local", "remote"} <= set(lanes)
    assert "confidence" in lanes["local"]
