"""Grid axes are enqueued cheapest-inner, so an expensive sweep pays for its
cost once per group instead of once per cell."""
from __future__ import annotations

from itertools import pairwise

from backend.app import db
from backend.app.generators.registry import change_weight


def _cells(client, x, y=None, payload=None):
    body = {"kind": "image_local", "payload": payload or {"prompt": "c", "seed": 1}, "x": x}
    if y:
        body["y"] = y
    r = client.post("/api/generate/grid", json=body)
    assert r.status_code == 200, r.text
    return [db.get_job(j).params for j in r.json()["job_ids"]]


def test_model_change_is_the_most_expensive_thing_we_know_about():
    assert change_weight("model_variant") > change_weight("loras")
    assert change_weight("loras") > change_weight("steps")
    assert change_weight("steps") > change_weight("seed")
    assert change_weight("something_unheard_of") == 0


def test_model_axis_is_grouped_even_when_the_user_puts_it_on_x(client, no_queue):
    """The bug this fixes: model_variant on X used to reload on every cell."""
    cells = _cells(
        client,
        x={"param": "model_variant", "values": ["turbo", "quality"]},
        y={"param": "seed", "values": [1, 2, 3]},
    )
    variants = [c["model_variant"] for c in cells]
    # Grouped: every turbo cell, then every quality cell (or the reverse).
    transitions = sum(1 for a, b in pairwise(variants) if a != b)
    assert transitions == 1, f"expected one model change, got {transitions}: {variants}"
    assert len(cells) == 6


def test_model_axis_on_y_is_grouped_too(client, no_queue):
    cells = _cells(
        client,
        x={"param": "seed", "values": [1, 2, 3]},
        y={"param": "model_variant", "values": ["turbo", "quality"]},
    )
    variants = [c["model_variant"] for c in cells]
    transitions = sum(1 for a, b in pairwise(variants) if a != b)
    assert transitions == 1


def test_worst_case_reload_count_is_bounded_by_the_axis_length(client, no_queue):
    """A 4x4 grid with the model on an axis should load 2 models, not 16 times."""
    cells = _cells(
        client,
        x={"param": "model_variant", "values": ["turbo", "quality"]},
        y={"param": "guidance", "values": [1.0, 2.0, 3.0, 4.0]},
    )
    variants = [c["model_variant"] for c in cells]
    loads = 1 + sum(1 for a, b in pairwise(variants) if a != b)
    assert loads == 2, f"one load per distinct model, got {loads}"


def test_cheap_axes_keep_their_original_nesting(client, no_queue):
    """Equal cost must not reshuffle a grid that was already fine."""
    cells = _cells(
        client,
        x={"param": "steps", "values": [4, 8]},
        y={"param": "strength", "values": [0.4, 0.8]},
    )
    assert len(cells) == 4


def test_single_axis_grid_is_unaffected(client, no_queue):
    """No Y axis means nothing to reorder — the regression this caught once."""
    cells = _cells(client, x={"param": "guidance", "values": [1.0, 2.0, 3.0]})
    assert [c["guidance"] for c in cells] == [1.0, 2.0, 3.0]


def test_display_coordinates_survive_the_reordering(client, no_queue):
    """Enqueue order changes; the grid the user sees must not."""
    cells = _cells(
        client,
        x={"param": "model_variant", "values": ["turbo", "quality"]},
        y={"param": "seed", "values": [7, 8]},
    )
    coords = {(c["grid"]["x"], c["grid"]["y"]) for c in cells}
    assert coords == {("turbo", "7"), ("turbo", "8"), ("quality", "7"), ("quality", "8")}
    for c in cells:
        assert c["grid"]["x_param"] == "model_variant"
        assert c["grid"]["y_param"] == "seed"


def test_every_cell_still_gets_generated(client, no_queue):
    cells = _cells(
        client,
        x={"param": "model_variant", "values": ["turbo", "quality"]},
        y={"param": "guidance", "values": [1.0, 2.0, 3.0]},
    )
    pairs = {(c["model_variant"], c["guidance"]) for c in cells}
    assert len(pairs) == 6, "no cell lost or duplicated by the reordering"


def test_random_seed_axis_is_materialized_before_it_is_labelled(client, no_queue):
    cells = _cells(client, x={"param": "seed", "values": [-1, -1, -1]})
    labels = [c["grid"]["x"] for c in cells]
    assert len(set(labels)) == 3
    assert "-1" not in labels
    assert [str(c["seed"]) for c in cells] == labels


def test_grid_rejects_values_that_would_make_lying_columns(client, no_queue):
    absent = client.post("/api/generate/grid", json={
        "kind": "image_local", "payload": {"prompt": "a cat"},
        "x": {"param": "prompt_sr", "sr_search": "dog", "values": ["fox"]},
    })
    assert absent.status_code == 400 and "does not contain" in absent.text

    unknown = client.post("/api/generate/grid", json={
        "kind": "image_local", "payload": {"prompt": "a cat"},
        "x": {"param": "model_variant", "values": ["not-a-model"]},
    })
    assert unknown.status_code == 400 and "unknown local model" in unknown.text


def test_grid_rejects_the_same_parameter_on_both_axes(client, no_queue):
    r = client.post("/api/generate/grid", json={
        "kind": "image_local", "payload": {"prompt": "a cat"},
        "x": {"param": "steps", "values": [4, 8]},
        "y": {"param": "steps", "values": [12, 16]},
    })
    assert r.status_code == 400


def test_retried_grid_request_recovers_one_complete_group(client, no_queue):
    body = {
        "kind": "image_local",
        "payload": {
            "prompt": "one grid",
            "seed": 1,
            "request_id": "grid_retry_1234567890",
        },
        "x": {"param": "guidance", "values": [1.0, 2.0, 3.0]},
    }
    first = client.post("/api/generate/grid", json=body)
    second = client.post("/api/generate/grid", json=body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(first.json()["job_ids"]) == 3
    assert len(db.list_jobs_by_group(first.json()["group_id"])) == 3
