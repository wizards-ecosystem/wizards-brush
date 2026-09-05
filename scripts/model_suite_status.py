#!/usr/bin/env python3
"""Audit the configured/cached state of the curated model evaluation suite."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.config import settings
from backend.app.generators import variants
from backend.app.generators.vram import model_weight_bytes

MANIFEST = ROOT / "benchmarks" / "model-candidates.json"


def audit() -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    runnable: list[dict[str, Any]] = []
    for item in manifest["runnable"]:
        lane = "local" if item["lane"] == "local" else "colab"
        configured = variants.resolve(item["variant"], lane=lane) == item["repo"]
        cached_bytes = model_weight_bytes(item["repo"])
        installed = configured and (cached_bytes is not None if lane == "local" else True)
        runnable.append({
            **item,
            "configured": configured,
            "installed": installed,
            "cached_gb": round(cached_bytes / 1_000_000_000, 2) if cached_bytes else None,
            "availability": "cached" if cached_bytes else ("remote-lazy" if lane == "colab" else "missing"),
        })

    sidecars: dict[str, Path] = {}
    for path in settings.loras_dir.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("repo_id"):
            sidecars[str(value["repo_id"])] = path
    remote_repos = {settings.image_lightning_lora, settings.edit_lightning_lora}
    adapters: list[dict[str, Any]] = []
    for item in manifest["adapters"]:
        if item["lane"] == "local":
            sidecar = sidecars.get(item["repo"])
            target = sidecar.with_suffix(".safetensors") if sidecar else None
            installed = bool(target and target.is_file())
            detail = target.name if target and installed else "missing"
        else:
            installed = item["repo"] in remote_repos
            detail = "remote-lazy" if installed else "not configured"
        adapters.append({**item, "installed": installed, "availability": detail})

    return {
        "reviewed_at": manifest["reviewed_at"],
        "policy": manifest["policy"],
        "runnable": runnable,
        "adapters": adapters,
        "excluded": manifest["excluded"],
        "ready": all(item["installed"] for item in runnable + adapters),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = audit()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Model evaluation suite (reviewed {report['reviewed_at']})")
        for item in report["runnable"]:
            mark = "OK" if item["installed"] else "MISSING"
            size = f" ({item['cached_gb']} GB cached)" if item["cached_gb"] else ""
            print(f"  {mark:7s} {item['lane']:6s} {item['variant']:8s} {item['repo']}{size}")
        for item in report["adapters"]:
            mark = "OK" if item["installed"] else "MISSING"
            print(f"  {mark:7s} lora   {item['repo']} [{item['availability']}]")
        print(f"ready: {'yes' if report['ready'] else 'no'}")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
