#!/usr/bin/env python3
"""Aggregate asset-inspector human grades for a completed benchmark report."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.db import session
from backend.app.models import Asset


def summarize(report: dict[str, Any], assets: dict[int, Asset]) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "images": 0, "graded": 0, "overall": [], "visual_quality": [],
        "prompt_fidelity": [], "generation_seconds": [], "prompt_ids": set(),
    })
    for record in report.get("records") or []:
        if not isinstance(record, dict):
            continue
        candidate = str(record.get("candidate") or record.get("label") or "unknown")
        group = groups[candidate]
        group["prompt_ids"].add(str(record.get("prompt_id") or "custom"))
        seconds = record.get("generation_seconds")
        if isinstance(seconds, int | float):
            group["generation_seconds"].append(float(seconds))
        for verdict in record.get("validation") or []:
            if not isinstance(verdict, dict) or not isinstance(verdict.get("asset_id"), int):
                continue
            group["images"] += 1
            asset = assets.get(verdict["asset_id"])
            grade = asset.grade if asset is not None else {}
            overall = grade.get("overall")
            visual = grade.get("visual_quality")
            fidelity = grade.get("prompt_fidelity")
            if not all(isinstance(value, int | float) for value in (overall, visual, fidelity)):
                continue
            group["graded"] += 1
            group["overall"].append(float(cast(int | float, overall)))
            group["visual_quality"].append(float(cast(int | float, visual)))
            group["prompt_fidelity"].append(float(cast(int | float, fidelity)))

    rows: list[dict[str, Any]] = []
    for candidate, group in groups.items():
        def mean(values: list[float]) -> float | None:
            return round(statistics.fmean(values), 3) if values else None

        rows.append({
            "candidate": candidate,
            "images": group["images"],
            "graded": group["graded"],
            "completion": round(group["graded"] / group["images"], 3) if group["images"] else 0,
            "prompts": len(group["prompt_ids"]),
            "overall": mean(group["overall"]),
            "visual_quality": mean(group["visual_quality"]),
            "prompt_fidelity": mean(group["prompt_fidelity"]),
            "median_generation_seconds": (
                round(statistics.median(group["generation_seconds"]), 3)
                if group["generation_seconds"] else None
            ),
        })
    rows.sort(key=lambda row: (
        row["overall"] is not None,
        row["overall"] if row["overall"] is not None else -1,
        row["prompt_fidelity"] if row["prompt_fidelity"] is not None else -1,
    ), reverse=True)
    return {
        "suite": report.get("suite", "custom"),
        "source_created_at": report.get("created_at"),
        "complete": bool(rows) and all(row["completion"] == 1 for row in rows),
        "ranking": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("report", help="output/benchmarks/*-model-audit-*.json")
    ap.add_argument("--output", help="default: REPORT.scores.json")
    ap.add_argument("--require-complete", action="store_true")
    args = ap.parse_args()
    path = Path(args.report)
    if not path.is_absolute():
        path = ROOT / path
    report = json.loads(path.read_text(encoding="utf-8"))
    ids = {
        verdict["asset_id"]
        for record in report.get("records") or []
        if isinstance(record, dict)
        for verdict in record.get("validation") or []
        if isinstance(verdict, dict) and isinstance(verdict.get("asset_id"), int)
    }
    with session() as db:
        assets = {asset_id: asset for asset_id in ids if (asset := db.get(Asset, asset_id))}
    result = summarize(report, assets)
    output = Path(args.output) if args.output else path.with_suffix(".scores.json")
    if not output.is_absolute():
        output = ROOT / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(f"suite: {result['suite']}")
    print("candidate                                      graded  overall  visual  fidelity  median s")
    for row in result["ranking"]:
        def show(value: object) -> str:
            return f"{value:.2f}" if isinstance(value, float) else "-"
        print(f"{row['candidate'][:46]:46s} {row['graded']:>3}/{row['images']:<3} "
              f"{show(row['overall']):>7s} {show(row['visual_quality']):>7s} "
              f"{show(row['prompt_fidelity']):>9s} {show(row['median_generation_seconds']):>9s}")
    print(f"scores: {output.relative_to(ROOT) if output.is_relative_to(ROOT) else output}")
    return 1 if args.require_complete and not result["complete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
