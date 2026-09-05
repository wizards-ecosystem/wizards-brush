"""Ratings turned into a verdict on models and adapters.

The interesting cases are all about attribution. A rating lands on an image, but
the image is a model AND whatever was merged into it, and charging both equally
is how a good model paired with a weak adapter ends up looking bad.
"""
from __future__ import annotations

import pytest

from backend.app import db, scoring
from backend.app.config import settings


def _img(name: str) -> object:
    p = settings.images_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


@pytest.fixture(autouse=True)
def _isolate(client):
    """A clean library per test.

    `client` is module-scoped, so without this every test would score against
    the rows the previous ones left behind — and `global_mean` is computed over
    the whole library, so the prior would shift under each test in turn.
    """
    from sqlalchemy import delete as sa_delete

    from backend.app.db import session
    from backend.app.models import Asset

    with session() as s:
        s.exec(sa_delete(Asset))
        s.commit()
    yield


@pytest.fixture()
def make(tmp_path):
    """Record an image made by `model` with `loras`, rated `rating` (0 = unrated)."""
    n = [0]

    def _make(model: str, loras: list[str], rating: int) -> int:
        n[0] += 1
        a = db.add_asset("image", _img(f"score_{n[0]}.png"), generator="local_image:txt2img",
                         meta={"model": model, "loras": [{"path": p, "weight": 0.8} for p in loras]})
        if rating:
            db.update_asset(a.id, rating=rating)
        return int(a.id)

    return _make


def test_unrated_images_do_not_count_as_zero(make):
    """`rating` defaults to 0 meaning "not looked at yet". Averaging that in would
    rank a model by how much of its output you got round to reviewing."""
    make("m/one", [], 5)
    make("m/one", [], 0)
    make("m/one", [], 0)
    row = next(r for r in scoring.leaderboards()["models"] if r["key"] == "m/one")
    assert row["rated"] == 1 and row["total"] == 3
    assert row["mean"] == 5.0


def test_a_single_rating_barely_moves_off_the_average(make):
    """What shrinkage actually promises, stated honestly.

    It compresses a thin result toward the library average — a lone 5-star lands
    near average, not at the top — so a score builds up as ratings accumulate.
    It does NOT reverse the order: the posterior mean of one 5 stays above ten
    4s for every prior, because both converge on the global mean from opposite
    sides. Ranking by evidence-count instead would need a lower confidence
    bound, which is a different (and much twitchier) design.
    """
    make("m/lucky", [], 5)
    for _ in range(10):
        make("m/proven", [], 4)
    board = {r["key"]: r for r in scoring.leaderboards()["models"]}
    raw_gap = board["m/lucky"]["mean"] - board["m/proven"]["mean"]
    shrunk_gap = board["m/lucky"]["score"] - board["m/proven"]["score"]
    assert raw_gap == pytest.approx(1.0)
    assert 0 < shrunk_gap < raw_gap / 4, "one rating must not carry a full star of advantage"


def test_a_models_own_score_is_measured_without_adapters(make):
    """base_score is the model judged as itself. A weak adapter merged into it is
    the adapter's fault, and must not be what the model is ranked on."""
    for _ in range(4):
        make("m/solid", [], 5)          # the model alone: excellent
    for _ in range(4):
        make("m/solid", ["bad.safetensors"], 1)   # ruined by an adapter
    row = next(r for r in scoring.leaderboards()["models"] if r["key"] == "m/solid")
    assert row["base_rated"] == 4
    assert row["base_score"] > row["score"], "the blended score is dragged down by the adapter"
    assert row["mean"] == 3.0, "blended: (5+5+5+5+1+1+1+1)/8"


def test_an_adapter_is_judged_by_what_it_changed(make):
    """Lift, not absolute mean. An adapter used only on a strong model would
    otherwise look good merely by association."""
    for _ in range(4):
        make("m/strong", [], 4)                       # baseline 4
    for _ in range(4):
        make("m/strong", ["helps.safetensors"], 5)    # +1
    for _ in range(4):
        make("m/strong", ["hurts.safetensors"], 2)    # -2
    rows = {r["key"]: r for r in scoring.leaderboards()["loras"]}
    assert rows["helps.safetensors"]["lift"] == pytest.approx(1.0)
    assert rows["hurts.safetensors"]["lift"] == pytest.approx(-2.0)


def test_lift_needs_a_control_and_says_nothing_without_one(make):
    """An adapter whose model has no rated adapter-free run has nothing to be
    compared against. Reporting a difference from an absent baseline would be
    inventing a number."""
    make("m/nobase", ["lonely.safetensors"], 5)
    row = next(r for r in scoring.leaderboards()["loras"] if r["key"] == "lonely.safetensors")
    assert row["lift"] is None and row["models_compared"] == 0
    assert row["rated"] == 1, "it is still counted, just not compared"


def test_lift_weighs_each_model_by_how_much_it_was_rated(make):
    """Averaging the per-model differences unweighted would let one rating on a
    rarely-used model outvote ten on the model you actually work with."""
    for _ in range(10):
        make("m/main", [], 3)
    for _ in range(10):
        make("m/main", ["a.safetensors"], 4)      # +1 over ten ratings
    make("m/rare", [], 5)
    make("m/rare", ["a.safetensors"], 1)          # -4 over one rating
    row = next(r for r in scoring.leaderboards()["loras"] if r["key"] == "a.safetensors")
    assert row["models_compared"] == 2
    # (+1*10 + -4*1) / 11
    assert row["lift"] == pytest.approx((1 * 10 + -4 * 1) / 11, abs=1e-3)


def test_a_trashed_image_stops_voting(make):
    """Deleting a bad result must actually remove its vote — otherwise rejecting
    an image is a no-op on the score it caused."""
    make("m/mixed", [], 5)
    worst = make("m/mixed", [], 1)
    before = next(r for r in scoring.leaderboards()["models"] if r["key"] == "m/mixed")
    assert before["rated"] == 2

    db.delete_assets([worst])

    after = next(r for r in scoring.leaderboards()["models"] if r["key"] == "m/mixed")
    assert after["rated"] == 1 and after["mean"] == 5.0


def test_a_structured_review_contributes_its_precise_composite(make):
    """The leaderboard should use the review's 4.4 evidence, not only its
    rounded four-star gallery badge."""
    aid = make("m/rubric", [], 0)
    grade, stars = scoring.build_human_grade({
        "prompt_fidelity": 5, "visual_quality": 4,
    })
    db.set_asset_grade(aid, grade, stars)
    row = next(r for r in scoring.leaderboards()["models"] if r["key"] == "m/rubric")
    assert row["rated"] == 1
    assert row["mean"] == pytest.approx(4.4, abs=1e-3)
