"""File wildcards, combinatorial fan-out, wildcard CRUD API, and the grid API."""
from __future__ import annotations

import json

import pytest

from backend.app import db
from backend.app import prompt_engine as pe


@pytest.fixture()
def wc_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "WILDCARDS_DIR", tmp_path)
    pe._WC_CACHE.clear()
    return tmp_path


def test_file_wildcard_seed_keyed(wc_dir):
    (wc_dir / "animal.txt").write_text("cat\ndog\nfox\n")
    a = pe.expand_prompt("a __animal__ in a hat", seed=7)
    b = pe.expand_prompt("a __animal__ in a hat", seed=7)
    assert a == b  # deterministic per seed
    assert "__animal__" not in a
    picks = {pe.expand_prompt("a __animal__", seed=s) for s in range(30)}
    assert len(picks) > 1  # different seeds vary


def test_fixed_image_seed_still_varies_prompt_wildcards_by_batch_index(wc_dir):
    (wc_dir / "mood.txt").write_text("calm\ntense\neerie\nbright\n")
    picks = {pe.expand_prompt("__mood__", index=i, seed=7) for i in range(12)}
    assert len(picks) > 1
    assert [pe.expand_prompt("__mood__", index=i, seed=7) for i in range(12)] == [
        pe.expand_prompt("__mood__", index=i, seed=7) for i in range(12)
    ]


def test_unknown_wildcard_left_literal(wc_dir):
    assert pe.expand_prompt("a __nope__ here", seed=1) == "a __nope__ here"


def test_wildcard_occurrences_independent(wc_dir):
    (wc_dir / "c.txt").write_text("red\nblue\ngreen\nyellow\npink\n")
    out = {pe.expand_prompt("__c__ and __c__", seed=s) for s in range(40)}
    assert any(o.split(" and ")[0] != o.split(" and ")[1] for o in out)


def test_nested_file_wildcards_expand_reproducibly(wc_dir):
    (wc_dir / "scene.txt").write_text("__weather__ forest\n__weather__ beach\n")
    (wc_dir / "weather.txt").write_text("misty\nsunny\nstormy\n")
    first = pe.expand_prompt("a __scene__", seed=71)
    assert "__" not in first
    assert first == pe.expand_prompt("a __scene__", seed=71)


def test_recursive_wildcard_cycle_is_bounded(wc_dir):
    (wc_dir / "a.txt").write_text("__b__\n")
    (wc_dir / "b.txt").write_text("__a__\n")
    out = pe.expand_prompt("prefix __a__ suffix", seed=4)
    assert len(out) <= pe.MAX_PROMPT_CHARS
    assert out in {"prefix __a__ suffix", "prefix __b__ suffix"}


def test_expansion_is_recapped_after_large_substitution(wc_dir):
    (wc_dir / "huge.txt").write_text("x" * (pe.MAX_PROMPT_CHARS + 500))
    assert len(pe.expand_prompt("__huge__ __huge__ __huge__", seed=1)) == pe.MAX_PROMPT_CHARS


def test_count_and_expand_all():
    assert pe.count_variants("plain") == 1
    assert pe.count_variants("{a|b} {x|y|z}") == 6
    all6 = pe.expand_all("{a|b} {x|y|z}")
    assert len(all6) == 6 and len(set(all6)) == 6
    assert pe.expand_all("{a|b}", cap=1) == ["a"]


def test_wildcard_crud_api(client, wc_dir):
    r = client.post("/api/wildcards/My Style!", json={"items": ["one", " two ", ""]})
    assert r.status_code == 200 and r.json()["name"] == "My_Style_"
    lst = client.get("/api/wildcards").json()
    assert any(w["name"] == "My_Style_" and w["count"] == 2 for w in lst)
    got = client.get("/api/wildcards/My_Style_").json()
    assert got["items"] == ["one", "two"]
    assert client.delete("/api/wildcards/My_Style_").json()["ok"]
    assert client.get("/api/wildcards/My_Style_").status_code == 404


def test_wildcard_crud_rejects_oversized_collections(client, wc_dir):
    items = ["x"] * (pe.MAX_WILDCARD_ENTRIES + 1)
    r = client.post("/api/wildcards/too_many", json={"items": items})
    assert r.status_code == 413
    assert not (wc_dir / "too_many.txt").exists()


def test_combinatorial_fanout(client, no_queue):
    r = client.post("/api/generate/image/local", data={"payload": json.dumps({
        "prompt": "a {red|blue} {cat|dog}", "combinatorial": True, "batch": 4,
    })})
    assert r.status_code == 200
    body = r.json()
    assert len(body["job_ids"]) == 4 and body["group_id"]
    prompts = {db.get_job(j).params["prompt"] for j in body["job_ids"]}
    assert prompts == {"a red cat", "a blue cat", "a red dog", "a blue dog"}
    for j in body["job_ids"]:
        assert db.get_job(j).params["batch"] == 1


def test_combinatorial_off_is_single_job(client, no_queue):
    r = client.post("/api/generate/image/local", data={"payload": json.dumps({
        "prompt": "a {red|blue} cat", "combinatorial": False,
    })})
    assert "job_ids" not in r.json()


def test_grid_generate_and_fetch(client, no_queue):
    r = client.post("/api/generate/grid", json={
        "kind": "image_local",
        "payload": {"prompt": "a castle at day", "seed": 1},
        "x": {"param": "guidance", "values": [1.0, 2.0]},
        "y": {"param": "prompt_sr", "values": ["day", "night"], "sr_search": "day"},
    })
    assert r.status_code == 200
    gid = r.json()["group_id"]
    assert len(r.json()["job_ids"]) == 4

    g = client.get(f"/api/grids/{gid}").json()
    assert g["x_param"] == "guidance" and g["y_param"] == "prompt_sr"
    assert g["x_values"] == ["1.0", "2.0"] and g["y_values"] == ["day", "night"]
    assert len(g["cells"]) == 4
    night = [c for c in g["cells"] if c["y"] == "night"]
    assert all(db.get_job(c["job_id"]).params["prompt"] == "a castle at night" for c in night)


def test_grid_caps_and_validation(client, no_queue):
    too_big = {"param": "seed", "values": list(range(37))}
    r = client.post("/api/generate/grid", json={
        "kind": "image_local", "payload": {"prompt": "x"}, "x": too_big})
    assert r.status_code == 400
    r = client.post("/api/generate/grid", json={
        "kind": "t2v", "payload": {"prompt": "x"}, "x": {"param": "seed", "values": [1]}})
    assert r.status_code == 400  # video kinds unsupported in v1
    assert client.get("/api/grids/nope").status_code == 404


def test_numeric_grid_ranges_expand_before_jobs_are_persisted(client, no_queue):
    stepped = client.post("/api/generate/grid", json={
        "kind": "image_local", "payload": {"prompt": "x", "seed": 1},
        "x": {"param": "guidance", "values": ["1-2 (+0.5)"]},
    })
    assert stepped.status_code == 200
    assert [db.get_job(job_id).params["guidance"]
            for job_id in stepped.json()["job_ids"]] == [1.0, 1.5, 2.0]

    counted = client.post("/api/generate/grid", json={
        "kind": "image_local", "payload": {"prompt": "x", "seed": 1},
        "x": {"param": "steps", "values": ["5-9 [3]"]},
    })
    assert counted.status_code == 200
    assert [db.get_job(job_id).params["steps"]
            for job_id in counted.json()["job_ids"]] == [5, 7, 9]


def test_numeric_grid_range_refuses_invalid_or_excessive_expansion(client, no_queue):
    for value in ("1-100 (+1)", "9-1 (+1)", "1-9 [100]"):
        response = client.post("/api/generate/grid", json={
            "kind": "image_local", "payload": {"prompt": "x"},
            "x": {"param": "steps", "values": [value]},
        })
        assert response.status_code == 400
        assert "range" in response.json()["detail"]


def test_grid_quality_axis_changes_derived_steps(client, no_queue):
    """Sweeping quality/aspect must re-derive steps/dims per cell — sweeping the
    already-derived params would produce N identical images."""
    r = client.post("/api/generate/grid", json={
        "kind": "image_local",
        "payload": {"prompt": "castle", "seed": 3},
        "x": {"param": "quality", "values": ["Draft", "High"]},
    })
    assert r.status_code == 200
    steps = [db.get_job(j).params["steps"] for j in r.json()["job_ids"]]
    assert steps[0] != steps[1]


def test_grid_aspect_axis_changes_dims(client, no_queue):
    r = client.post("/api/generate/grid", json={
        "kind": "image_local",
        "payload": {"prompt": "castle", "seed": 3},
        "x": {"param": "aspect", "values": ["1:1", "16:9"]},
    })
    dims = [(db.get_job(j).params["width"], db.get_job(j).params["height"])
            for j in r.json()["job_ids"]]
    assert dims[0] != dims[1]
    assert dims[0][0] == dims[0][1]  # 1:1 cell is square


def test_grid_pins_random_seed_across_cells(client, no_queue):
    # Without a fixed seed the cells must still share one, or the sweep is
    # confounded by per-cell random seeds.
    r = client.post("/api/generate/grid", json={
        "kind": "image_local",
        "payload": {"prompt": "castle"},
        "x": {"param": "quality", "values": ["Draft", "High"]},
    })
    seeds = {db.get_job(j).params["seed"] for j in r.json()["job_ids"]}
    assert len(seeds) == 1


def test_grid_rejects_non_numeric_axis_values(client, no_queue):
    r = client.post("/api/generate/grid", json={
        "kind": "image_local", "payload": {"prompt": "x"},
        "x": {"param": "guidance", "values": ["abc"]},
    })
    assert r.status_code == 400
