"""The model bake-off must stay matched, reproducible, and gradeable."""
from __future__ import annotations

import json
from pathlib import Path

from backend.app.models import Asset
from scripts.a100_sampling_matrix import _payload as a100_payload
from scripts.benchmark_prompts import load_suite
from scripts.benchmark_results import summarize
from scripts.sampling_matrix import payload as local_payload

ROOT = Path(__file__).resolve().parents[1]


def test_general_art_suite_has_smoke_and_broad_full_coverage():
    path = ROOT / "benchmarks" / "prompts" / "general-art-v1.json"
    name, smoke = load_suite(path, smoke_only=True)
    _, full = load_suite(path)
    assert name == "general-art-v1"
    assert [prompt.id for prompt in smoke] == ["portrait", "composition"]
    assert len(full) >= 6
    assert len({prompt.category for prompt in full}) == len(full)
    assert all(prompt.width % 16 == prompt.height % 16 == 0 for prompt in full)


def test_matched_dimensions_and_seeds_reach_both_lanes():
    bench = load_suite(
        ROOT / "benchmarks" / "prompts" / "general-art-v1.json",
        selected={"portrait"},
    )[1][0]
    local = local_payload(
        "klein", None, bench.prompt, 2, bench.seed,
        width=bench.width, height=bench.height,
    )
    remote = a100_payload(
        "hidream", bench.prompt, 2, bench.seed, speed=False,
        width=bench.width, height=bench.height,
    )
    assert (local["width"], local["height"], local["seed"]) == (
        remote["width"], remote["height"], remote["seed"])
    assert (local["steps"], local["guidance"]) == (4, 1.0)
    assert (remote["steps"], remote["guidance"]) == (50, 5.0)


def test_candidate_manifest_excludes_models_with_extra_deployment_obligations():
    manifest = json.loads((ROOT / "benchmarks" / "model-candidates.json").read_text())
    runnable = {item["repo"] for item in manifest["runnable"]}
    excluded = {item["repo"]: item["reason"].lower() for item in manifest["excluded"]}
    for repo in (
        "ideogram-ai/ideogram-4-nf4",
        "black-forest-labs/FLUX.2-dev",
        "black-forest-labs/FLUX.2-klein-9B",
        "krea/Krea-2-Turbo",
    ):
        assert repo not in runnable
        assert "deployment controls" in excluded[repo]
        assert "review" in excluded[repo] or "oversight" in excluded[repo]


def test_grade_aggregation_ranks_complete_candidates():
    first = Asset(path="output/a.png", filename="a.png", kind="image", grade_json=json.dumps({
        "overall": 4.6, "visual_quality": 5, "prompt_fidelity": 4,
    }))
    first.id = 11
    second = Asset(path="output/b.png", filename="b.png", kind="image", grade_json=json.dumps({
        "overall": 3.8, "visual_quality": 4, "prompt_fidelity": 3,
    }))
    second.id = 12
    report = {
        "suite": "unit",
        "records": [
            {"candidate": "better", "prompt_id": "p", "generation_seconds": 8.0,
             "validation": [{"asset_id": 11}]},
            {"candidate": "other", "prompt_id": "p", "generation_seconds": 4.0,
             "validation": [{"asset_id": 12}]},
        ],
    }
    result = summarize(report, {11: first, 12: second})
    assert result["complete"] is True
    assert [row["candidate"] for row in result["ranking"]] == ["better", "other"]
