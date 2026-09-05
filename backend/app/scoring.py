"""What the ratings say about each model and adapter.

A rating is attached to an image, but the question people actually have is about
the things that made it: is this adapter worth keeping, is that model better for
faces. Every image already records the model id it came from and the adapters
that were merged into it, so the answer is an aggregation rather than new
bookkeeping — nothing extra has to be captured at generation time.

Three decisions carry this module:

**Unrated is not zero.** `rating` defaults to 0 and means "not looked at yet".
Averaging that in would rank a model by how much of its output you have got
around to reviewing, so only rated images count, and the number of them is
reported alongside every score.

**A flat average confounds the model with the adapter.** An image made by
turbo + a weak adapter is evidence against the adapter, not against turbo, but a
plain per-key mean charges both equally — so a good model paired with bad
adapters sinks, and a mediocre model looks strong because its adapters were good.
Two separate numbers fix that:

  * a model also carries `base_score`, over its adapter-free images only. That is
    the model judged as itself, with nothing merged into it.
  * an adapter also carries `lift`: its mean minus the SAME model's base mean,
    averaged over the models it was used with. That is the adapter judged by what
    it changed, which is the only thing an adapter is responsible for.

`lift` is None until an adapter and its model's base have both been rated. It
needs a control, and saying nothing is better than reporting a difference
against an absent baseline.

**One image credits every adapter on it.** A stack of two is evidence about both,
and cannot separate them; `rated` keeps that visible.

**A single 5-star image does not win.** The whole point is a score that settles
in slowly, so the raw mean is shrunk toward the global mean by a fixed prior:

    score = (PRIOR * global_mean + sum_of_ratings) / (PRIOR + rated)

which is the standard Bayesian estimate. A new adapter starts indistinguishable
from average and earns its way up or down over roughly PRIOR ratings, instead of
topping the board on its first lucky image. Raw `mean` is reported too, because
the shrunk score is the ranking and the mean is the honest observation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlmodel import select

from .db import session
from .models import Asset

# How many ratings it takes for an entry's own evidence to outweigh the prior.
# Five is deliberately small: this is one person rating their own library, not a
# storefront, and a score that needs fifty ratings to move would never move.
PRIOR = 5.0

STAR_MIN, STAR_MAX = 1, 5

# A star says whether an image is worth keeping. Keep the review as small as
# possible: people can reliably answer "does it look good?" and "did it do what
# I asked?". Adapter impact is derived by comparing those two scores with the
# same model's adapter-free control in ``leaderboards()``; asking a reviewer to
# separately guess at a LoRA's invisible contribution proved confusing.
GRADE_WEIGHTS: dict[str, float] = {
    "visual_quality": 0.60,
    "prompt_fidelity": 0.40,
}


def build_human_grade(values: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Normalise a submitted rubric and return ``(stored_grade, rounded_stars)``."""
    clean: dict[str, int | None] = {}
    total = weight = 0.0
    for key, row_weight in GRADE_WEIGHTS.items():
        raw = values.get(key)
        if raw is None:
            clean[key] = None
            continue
        value = max(STAR_MIN, min(int(raw), STAR_MAX))
        clean[key] = value
        total += value * row_weight
        weight += row_weight
    # Prompt match and visual quality are required by the API. Keep a defensive
    # fallback so an internal caller cannot create a divide-by-zero review.
    overall = total / weight if weight else 0.0
    grade: dict[str, Any] = {
        "version": 2,
        **clean,
        "overall": round(overall, 3),
        "notes": str(values.get("notes") or "").strip()[:800],
        "reviewed_at": datetime.now(UTC).isoformat(),
    }
    return grade, max(STAR_MIN, min(int(overall + 0.5), STAR_MAX))


@dataclass
class Entry:
    """One model or adapter's standing."""

    key: str
    rated: int = 0
    total: int = 0                      # images made with it, rated or not
    _sum: float = 0.0
    stars: dict[int, int] = field(default_factory=dict)

    @property
    def mean(self) -> float:
        return self._sum / self.rated if self.rated else 0.0

    def score(self, global_mean: float) -> float:
        """Mean shrunk toward `global_mean`; see the module docstring."""
        return (PRIOR * global_mean + self._sum) / (PRIOR + self.rated)

    def as_dict(self, global_mean: float) -> dict[str, Any]:
        return {
            "key": self.key,
            "rated": self.rated,
            "total": self.total,
            "mean": round(self.mean, 3),
            "score": round(self.score(global_mean), 3),
            "stars": {str(s): self.stars.get(s, 0) for s in range(STAR_MIN, STAR_MAX + 1)},
        }


# Models key by id, adapter/model pairs key by both — the same tally either way.
def _accumulate[K](entries: dict[K, Entry], key: K, rating: float) -> None:
    e = entries.setdefault(key, Entry(str(key)))
    e.total += 1
    if rating:                                   # 0 == not rated, never a score
        e.rated += 1
        e._sum += rating
        # Grade-derived scores can land between stars. The histogram remains a
        # useful coarse distribution, so place them in the nearest star bin.
        star = max(STAR_MIN, min(int(rating + 0.5), STAR_MAX))
        e.stars[star] = e.stars.get(star, 0) + 1


def _rating_of(asset: Asset) -> float:
    """Prefer a completed rubric; old quick-star reviews remain valid data."""
    raw = asset.grade.get("overall")
    try:
        score = float(str(raw))
    except (TypeError, ValueError):
        score = 0.0
    if STAR_MIN <= score <= STAR_MAX:
        return score
    return float(asset.rating or 0)


def leaderboards() -> dict[str, Any]:
    """Per-model and per-adapter standings over every live asset.

    Trashed rows are excluded for the same reason every other accessor excludes
    them: a deleted image is one the user rejected, and letting it keep voting
    would make deleting a bad result *lower* nothing and count forever.
    """
    models: dict[str, Entry] = {}
    base: dict[str, Entry] = {}                  # adapter-free images, per model
    loras: dict[str, Entry] = {}
    paired: dict[tuple[str, str], Entry] = {}    # (adapter, model) -> its own tally
    rated_sum = 0.0
    rated_n = 0

    with session() as s:
        rows = s.exec(select(Asset).where(Asset.deleted_at == None))  # noqa: E711 — SQL NULL
        for a in rows:
            meta = a.meta
            rating = _rating_of(a)
            if rating:
                rated_sum += rating
                rated_n += 1
            model = str(meta.get("model") or "").strip()
            names = [str(i.get("path") or "").strip()
                     for i in (meta.get("loras") or []) if isinstance(i, dict)]
            names = [n for n in names if n]
            if model:
                _accumulate(models, model, rating)
                if not names:
                    # The control: this model with nothing merged into it.
                    _accumulate(base, model, rating)
            for name in names:
                _accumulate(loras, name, rating)
                if model:
                    _accumulate(paired, (name, model), rating)

    # The prior has to be anchored somewhere. The library's own mean is the right
    # anchor: it makes "average" mean average *for this user's taste and prompts*,
    # not a constant guess about what a rating means.
    global_mean = rated_sum / rated_n if rated_n else 0.0
    def rank(entries: dict[str, Entry]) -> list[dict[str, Any]]:
        # Score first, then evidence: two entries that shrink to the same score
        # are separated by which one has actually earned it.
        return sorted((e.as_dict(global_mean) for e in entries.values()),
                      key=lambda x: (-x["score"], -x["rated"], x["key"]))

    def lift_of(name: str) -> tuple[float | None, int]:
        """Mean rating with this adapter minus the same model's base mean.

        Averaged across models, weighted by how many rated images back each
        comparison, so a model you have rated ten times counts for more than one
        you rated once. None when no model offers a rated control.
        """
        num = den = 0.0
        models_compared = 0
        for (adapter, model), entry in paired.items():
            control = base.get(model)
            if adapter != name or not entry.rated or control is None or not control.rated:
                continue
            num += (entry.mean - control.mean) * entry.rated
            den += entry.rated
            models_compared += 1
        return (round(num / den, 3) if den else None), models_compared

    model_rows = rank(models)
    for row in model_rows:
        control = base.get(row["key"])
        row["base_rated"] = control.rated if control else 0
        row["base_score"] = round(control.score(global_mean), 3) if control and control.rated else None

    lora_rows = rank(loras)
    for row in lora_rows:
        row["lift"], row["models_compared"] = lift_of(row["key"])

    return {
        "models": model_rows,
        "loras": lora_rows,
        "global_mean": round(global_mean, 3),
        "rated": rated_n,
        "prior": PRIOR,
    }
