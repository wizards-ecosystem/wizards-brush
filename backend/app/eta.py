"""How long a job will take.

Two different problems, so two different mechanisms.

**The running job: measure it.** We get a callback every denoising step, so after
a few steps the rate is known. Remaining time is arithmetic. Exact, needs no
history, and automatically right on a slow card or an unusual resolution because
it is measuring the actual run rather than predicting it.

**Queued jobs: calibrate from history.** Cost scales as

    batch × pixels^1.5 × steps

The exponent matters. Attention cost grows faster than area, so scoring by pixel
count alone badly underestimates large images. Fit seconds-per-cost-unit from
completed jobs of the same kind, then multiply.

**The model swap is folded in.** An estimate that ignores a 60-120 second reload
is a lie, and the scheduler already knows the planned order, so the swaps are
predictable rather than a surprise.

**Nothing is shown until it is honest.** Fewer than three comparable jobs in
history means the answer is "estimating", not a number. A confident wrong number
is worse than an admission of ignorance.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Literal

from . import db
from .models import Job, JobStatus

MIN_SAMPLES = 3
# Cost of loading a model, used until we have measured one. Deliberately near the
# pessimistic end of the observed 60-120s range: an ETA that comes in early is a
# pleasant surprise, one that overruns is a broken promise.
DEFAULT_SWAP_SECONDS = 95.0
UNIT = 2 * (1024 * 1024) * math.sqrt(1024 * 1024) * 24    # a 2x1024² batch at 24 steps


# What an estimate is worth. "measured" comes from the job's own observed rate;
# "estimated" from comparable history; "estimating" means some history exists but
# not enough to be honest about; "unknown" means none at all.
Confidence = Literal["measured", "estimated", "estimating", "unknown"]


@dataclass(frozen=True)
class Estimate:
    seconds: float | None          # None means "not enough information"
    confidence: Confidence
    includes_swap: bool = False

    @property
    def known(self) -> bool:
        return self.seconds is not None


def cost_of(params: dict) -> float:
    """Relative work in a job, in the same units across kinds.

    `pixels^1.5` rather than `pixels`: attention is super-linear in sequence
    length, so doubling the area more than doubles the time. Steps and batch are
    linear and multiply straight through.
    """
    w = int(params.get("width") or 1024)
    h = int(params.get("height") or 1024)
    steps = max(1, int(params.get("steps") or 20))
    batch = max(1, int(params.get("batch") or 1))
    frames = int(params.get("num_frames") or 0)
    pixels = max(1, w * h)
    cost = batch * pixels * math.sqrt(pixels) * steps
    if frames:
        cost *= frames          # video is linear in frame count on top of everything
    return cost / UNIT


def rate_for(kind: str, samples: int = 20) -> tuple[float | None, int]:
    """(seconds per cost unit, sample count) from recent completed jobs of `kind`.

    Uses the median rather than the mean: one job that happened to include a
    model load, or one the machine was thrashing through, would drag a mean far
    enough to make every subsequent estimate wrong.
    """
    rates: list[float] = []
    for job in db.list_jobs(limit=200, status=JobStatus.done.value):
        if job.kind != kind or not job.started_at or not job.finished_at:
            continue
        elapsed = (job.finished_at - job.started_at).total_seconds()
        cost = cost_of(job.params)
        if elapsed <= 0 or cost <= 0:
            continue
        rates.append(elapsed / cost)
        if len(rates) >= samples:
            break
    if len(rates) < MIN_SAMPLES:
        return None, len(rates)
    rates.sort()
    return rates[len(rates) // 2], len(rates)


def swap_seconds() -> float:
    """Measured cost of a model load, or a conservative default.

    Not yet derived from history: a swap is not a job, so it has no row to time.
    The scheduler emits load stages, and wiring those to a measurement is the
    obvious next step — until then this is a documented constant rather than a
    number pretending to be evidence.
    """
    return DEFAULT_SWAP_SECONDS


def for_queued(job: Job, *, resident_model: str | None = None) -> Estimate:
    """How long `job` will take once it starts, including a swap if it needs one."""
    rate, samples = rate_for(job.kind)
    if rate is None:
        return Estimate(None, "unknown" if samples == 0 else "estimating")
    seconds = cost_of(job.params) * rate
    needs_swap = bool(job.model_key) and resident_model is not None \
        and job.model_key != resident_model
    if needs_swap:
        seconds += swap_seconds()
    return Estimate(seconds, "estimated", includes_swap=needs_swap)


def for_running(job: Job, *, now: float | None = None) -> Estimate:
    """Remaining time for a running job, from the rate it is actually achieving.

    Falls back to the historical estimate before enough progress has accrued to
    measure — at 0% there is nothing to extrapolate from.
    """
    if job.started_at is None or not job.progress:
        return for_queued(job)
    elapsed = (now or time.time()) - job.started_at.timestamp()
    if elapsed <= 0 or job.progress <= 0.02:
        return for_queued(job)
    total = elapsed / job.progress
    return Estimate(max(0.0, total - elapsed), "measured")


def queue_total(jobs: list[Job], *, resident_model: str | None = None) -> Estimate:
    """Total remaining time for a whole lane, in the order it will run.

    Walks the queue tracking which model would be resident at each point, so the
    swaps counted are the ones that will actually happen rather than one per job
    that differs from what is loaded right now.
    """
    total = 0.0
    known_any = False
    resident = resident_model
    for job in jobs:
        est = (for_running(job) if job.status == JobStatus.running.value
               else for_queued(job, resident_model=resident))
        if est.seconds is not None:
            total += est.seconds
            known_any = True
        if job.model_key:
            resident = job.model_key
    return Estimate(total if known_any else None,
                    "estimated" if known_any else "unknown")


def humanize(seconds: float | None) -> str:
    """A duration a person can read at a glance. Empty string for unknown."""
    if seconds is None:
        return ""
    s = round(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"
