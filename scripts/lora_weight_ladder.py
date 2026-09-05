#!/usr/bin/env python3
"""Diagnose a bad model/LoRA pairing with matched two-seed weight probes.

This is deliberately a *follow-up* to ``sampling_matrix.py``, not a way to
skip a failed row.  It holds the model's published quality recipe, prompt,
aspect, and two seeds constant, then changes only the adapter weight.  That
makes a blob attributable to the merge strength or checkpoint pairing instead
of a resolution or prompt change.

Example after a failed model/adapter row::

    .venv/bin/python scripts/lora_weight_ladder.py \
      --variant quality --lora adapter-name --weights 0.10,0.25,0.45 --wait
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.generators import variants
from backend.app.loras import list_loras
from scripts.sampling_matrix import ENDPOINT, payload, wait_and_validate


def _weights(raw: str) -> list[float]:
    try:
        values = [float(value.strip()) for value in raw.split(",") if value.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("weights must be comma-separated numbers") from exc
    if not values or any(not 0 <= value <= 2 for value in values):
        raise argparse.ArgumentTypeError("supply one or more weights from 0 to 2")
    return values


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--variant", required=True, help="local model variant, e.g. quality")
    ap.add_argument("--lora", required=True, help="installed adapter name, not a filename")
    ap.add_argument("--weights", type=_weights, default=[0.10, 0.25, 0.45])
    ap.add_argument("--prompt-file", default="benchmarks/prompts/lora-style-probe.txt")
    ap.add_argument("--images", type=int, default=2)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--resume-job-ids", default="",
                    help="comma-separated existing job ids matching --weights; validate only")
    ap.add_argument("--wait", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    available = {item.name for item in variants.available("local")}
    if args.variant not in available:
        ap.error(f"unknown local variant {args.variant!r}")
    adapter = next((item for item in list_loras() if item.get("name") == args.lora), None)
    if adapter is None:
        ap.error(f"installed adapter {args.lora!r} not found")
    prompt_file = Path(args.prompt_file)
    if not prompt_file.is_absolute():
        prompt_file = ROOT / prompt_file
    if not prompt_file.is_file() or not (prompt := prompt_file.read_text(encoding="utf-8").strip()):
        ap.error(f"no usable prompt file at {prompt_file}")

    print(f"prompt: {len(prompt)} chars from {prompt_file.name} (not echoed)")
    print(f"--- {args.variant}+{args.lora}: {len(args.weights)} weights x {args.images} images ---")
    for weight in args.weights:
        print(f"    weight {weight:g}")
    if args.dry_run:
        return 0

    existing_ids: list[int] = []
    if args.resume_job_ids:
        try:
            existing_ids = [int(value.strip()) for value in args.resume_job_ids.split(",") if value.strip()]
        except ValueError:
            ap.error("--resume-job-ids must be comma-separated integer job ids")
        if len(existing_ids) != len(args.weights):
            ap.error("--resume-job-ids must contain exactly one id for each weight")

    def record(job_id: int, weight: float, body: dict) -> dict:
        return {
            "job_id": job_id, "label": f"local/{args.variant}+{args.lora}@{weight:g}",
            "images": args.images,
            "settings": {
                key: body[key]
                for key in ("model_variant", "steps", "guidance", "sampler", "aspect", "loras")
            },
            "expected": {
                "model": variants.resolve(args.variant, lane="local"),
                "loras": [adapter["path"]],
            },
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }

    submitted: list[dict] = []
    failures = 0
    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        for position, weight in enumerate(args.weights):
            # Don't mutate the live catalogue mapping: the next weight must
            # start from the same source facts and differ only in merge weight.
            probe_adapter = {**adapter, "recommended_weight": weight}
            body = payload(args.variant, probe_adapter, prompt, args.images, args.seed)
            label = f"local/{args.variant}+{args.lora}@{weight:g}"
            if existing_ids:
                submitted.append(record(existing_ids[position], weight, body))
                print(f"  resuming job {existing_ids[position]:>4}  {label}")
                continue
            try:
                response = client.post(ENDPOINT, data={"payload": json.dumps(body)})
                response.raise_for_status()
                job_id = int(response.json().get("job_id"))
                print(f"  queued job {job_id:>4}  {label}")
                submitted.append(record(job_id, weight, body))
            except Exception as exc:  # noqa: BLE001 — finish every independent probe
                print(f"  FAILED {label}: {type(exc).__name__} {exc}", file=sys.stderr)
                failures += 1
        if args.wait and submitted:
            report = (
                ROOT / "output" / "benchmarks"
                / f"lora-weight-ladder-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
            )
            failures += wait_and_validate(client, submitted, report)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
