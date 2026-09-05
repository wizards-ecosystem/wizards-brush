#!/usr/bin/env python3
"""Run a reproducible, quality-first audit of every local model/LoRA pairing.

One job per compatible (model, adapter, prompt) row produces the same number of
images from the same dimensions and seed sequence. The only intentional differences are
the model or adapter and the quality settings its own model card recommends.

Why this produces evidence rather than a giant pile of pictures:

* Published quality recipes are used instead of one generic step/CFG pair.
* Seeds are fixed and shared. Two images per row give each combination an
  initial robustness check without confusing random noise for a model change.
* The adapter catalogue's family *and sidecar checkpoint pin* decide eligible
  combinations. A LoRA officially trained for FLUX.1-dev is not a Chroma test
  merely because the two architectures are related.
* Finishing is off. Face restoration and upscaling would conceal exactly the
  model-level differences this audit needs to expose.

Usage:
    source scripts/project-env.sh
    .venv/bin/python scripts/sampling_matrix.py --smoke --dry-run
    .venv/bin/python scripts/sampling_matrix.py --all-prompts --wait

The backend must be running (``make dev`` or ``make start``). ``--wait``
validates finished file decodability and recorded model/LoRA provenance, reports
timing, and saves a JSON artifact under output/benchmarks/. It cannot decide
whether an image is visually good; complete the human review rubric in the
asset inspector before choosing a winner.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.generators import variants
from backend.app.loras import list_loras
from backend.app.modelprobe import compatible, family_of_model
from scripts.benchmark_prompts import load_single, load_suite

ENDPOINT = "/api/generate/image/local"

# Model cards and maintained reference Spaces use these values for final
# quality. Negative prompting is family-specific: Turbo at CFG 0 ignores it,
# caption-trained DiTs do not benefit from an SDXL-era token soup, and Chroma's
# reference space supplies its own short quality list.
QUALITY_PROFILES: dict[str, dict[str, Any]] = {
    "turbo": {
        "steps": 9, "guidance": 0.0, "negative_prompt": "",
        "why": "Z-Image-Turbo official 9-step / CFG 0 recipe",
    },
    "quality": {
        "steps": 40, "guidance": 4.0,
        "negative_prompt": "watermark, text overlay, deformed hands, extra fingers, distorted face",
        "why": "Z-Image base recommended 28–50 steps / CFG 3–5",
    },
    "klein": {
        "steps": 4, "guidance": 1.0, "negative_prompt": "",
        "why": "FLUX.2-klein-4B official 4-step / CFG 1 recipe",
    },
    "sdxl": {
        "steps": 36, "guidance": 3.5,
        "negative_prompt": (
            "low quality, blurry, deformed, bad anatomy, extra limbs, extra fingers, watermark, text"
        ),
        "why": "SDXL checkpoint quality profile: 36 steps / CFG 3.5 at its trained area",
    },
    "chroma": {
        "steps": 40, "guidance": 3.0,
        "negative_prompt": (
            "low quality, ugly, unfinished, out of focus, deformed, disfigured, blurry, smudged, "
            "restricted palette, flat colors"
        ),
        "why": "Chroma1-HD reference Space: 40 steps / CFG 3",
    },
    "flux": {
        "steps": 50, "guidance": 3.5, "negative_prompt": "",
        "why": "FLUX.1-dev official quality recipe: 50 steps / CFG 3.5",
    },
}

# The shared prompt is a full-person portrait. High quality uses ~0.92 MP on
# the local DiT profile while preserving SDXL's ~1.05 MP trained bucket.
ASPECT = "2:3"


def combinations() -> list[tuple[str, dict | None]]:
    """Every valid local base or single-LoRA row, in model-affine order."""
    adapters = list_loras()
    out: list[tuple[str, dict | None]] = []
    for variant in variants.available("local"):
        family = family_of_model(variants.repo_of(variant))
        out.append((variant.name, None))
        for adapter in adapters:
            if not compatible(adapter.get("family", ""), family):
                continue
            pins = [p.lower() for p in (adapter.get("variants") or [])]
            if pins and variant.name.lower() not in pins:
                continue
            out.append((variant.name, adapter))
    return out


def _weight(adapter: dict | None) -> float:
    if adapter is None:
        return 0.0
    try:
        return float(str(adapter.get("recommended_weight")))
    except (TypeError, ValueError):
        return 0.8


def payload(variant: str, adapter: dict | None, prompt: str, images: int, seed: int,
            *, width: int | None = None, height: int | None = None) -> dict:
    profile = QUALITY_PROFILES.get(variant)
    if profile is None:
        raise ValueError(f"no quality benchmark profile for local variant {variant!r}")
    body = {
        "prompt": prompt,
        "negative_prompt": profile["negative_prompt"],
        "auto_negative": False,
        "model_variant": variant,
        "loras": ([{"path": adapter["path"], "weight": _weight(adapter)}] if adapter else []),
        "batch": images,
        "seed": seed,
        "seed_mode": "increment",
        "quality": "Custom",
        "steps": profile["steps"],
        "guidance": profile["guidance"],
        "sampler": "default",
        "aspect": "Custom" if width and height else ASPECT,
        "finish": "none",
    }
    if width and height:
        body.update(width=width, height=height)
    return body


def _image_check(client: httpx.Client, asset: dict, expected: dict) -> dict:
    """Validate objective output/provenance. A human grades visual quality."""
    result: dict[str, Any] = {"asset_id": asset.get("id"), "ok": False, "problems": []}
    meta = asset.get("meta") or {}
    if meta.get("model") != expected["model"]:
        result["problems"].append("recorded model does not match the planned model")
    got_loras = [x.get("path") for x in (meta.get("loras") or []) if isinstance(x, dict)]
    if got_loras != expected["loras"]:
        result["problems"].append("recorded LoRA set does not match the planned combination")
    warnings = meta.get("warnings") or []
    if warnings:
        result["problems"].append(f"generation warnings: {warnings}")
    try:
        response = client.get(asset["url"])
        response.raise_for_status()
        with Image.open(BytesIO(response.content)) as image:
            image.verify()
        with Image.open(BytesIO(response.content)) as image:
            result["dimensions"] = list(image.size)
        if not result["dimensions"][0] or not result["dimensions"][1]:
            result["problems"].append("decoded image has an empty dimension")
    except Exception as exc:  # noqa: BLE001 — report a bad result, do not hide it
        result["problems"].append(f"image could not be decoded: {type(exc).__name__}: {exc}")
    result["ok"] = not result["problems"]
    return result


def _get_json(client: httpx.Client, path: str, *, attempts: int = 10) -> dict:
    """Read a local API row through short-lived connection resets.

    GPU pressure can briefly reset an otherwise healthy localhost socket on this
    WSL host. A benchmark must not confuse a lost *poll* with a failed image;
    the durable job row remains the source of truth. Ten retries at two seconds
    is short compared with an image run but long enough to bridge the reset.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = client.get(path)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise RuntimeError(f"unexpected JSON object at {path}")
            return value
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(2)
    raise RuntimeError(f"could not read {path} after {attempts} attempts: {last}") from last


def _seconds_between(start: object, end: object) -> float | None:
    """ISO timestamps from the API to a duration, without trusting wall clock."""
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end)
    except ValueError:
        return None
    return round((b - a).total_seconds(), 3)


def wait_and_validate(client: httpx.Client, queued: list[dict], report_path: Path) -> int:
    """Wait for jobs and persist timing, image, and provenance evidence."""
    records: list[dict[str, Any]] = []
    failures = 0
    for row in queued:
        job_id = row["job_id"]
        started = time.monotonic()
        while True:
            job = _get_json(client, f"/api/jobs/{job_id}")
            if job.get("status") in {"done", "error", "canceled"}:
                break
            time.sleep(2)
        elapsed = round(time.monotonic() - started, 3)
        record: dict[str, Any] = {
            "label": row["label"],
            "candidate": row.get("candidate", row["label"]),
            "prompt_id": row.get("prompt_id", "custom"),
            "prompt_category": row.get("prompt_category", "custom"),
            "prompt_sha256": row.get("prompt_sha256", ""),
            "job_id": job_id,
            "status": job.get("status"),
            "elapsed_wait_seconds": elapsed,
            "queue_wait_seconds": _seconds_between(job.get("created_at"), job.get("started_at")),
            "generation_seconds": _seconds_between(job.get("started_at"), job.get("finished_at")),
            "settings": row["settings"],
            "validation": [],
        }
        ids = list((job.get("result") or {}).get("asset_ids") or [])
        if job.get("status") != "done" or len(ids) != row["images"]:
            record["error"] = job.get("error") or (
                f"expected {row['images']} images, job returned {len(ids)}")
            failures += 1
        else:
            for asset_id in ids:
                asset = _get_json(client, f"/api/assets/{asset_id}")
                verdict = _image_check(client, asset, row["expected"])
                record["validation"].append(verdict)
                if not verdict["ok"]:
                    failures += 1
        records.append(record)
        state = "OK" if not record.get("error") and all(v["ok"] for v in record["validation"]) else "FAIL"
        perf = record["generation_seconds"]
        perf_text = f"{perf:.1f}s generate" if isinstance(perf, float) else f"{elapsed:.1f}s observed"
        print(f"  {state:4s} job {job_id:>4}  {row['label']}  ({perf_text})")

    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "suite": queued[0].get("suite", "custom") if queued else "",
        "prompt_count": len({row.get("prompt_id") for row in queued}),
        "images_per_combination": queued[0]["images"] if queued else 0,
        "records": records,
        "objective_failures": failures,
        "human_review": "Open each asset and complete the Review rubric before choosing a winner.",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"report: {report_path.relative_to(ROOT)}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    prompt_group = ap.add_mutually_exclusive_group()
    prompt_group.add_argument(
        "--suite", default="benchmarks/prompts/general-art-v1.json",
        help="JSON prompt suite (default: general-art-v1)",
    )
    prompt_group.add_argument("--prompt-file", help="single custom prompt; disables the suite")
    coverage = ap.add_mutually_exclusive_group()
    coverage.add_argument("--smoke", action="store_true", help="run only suite rows marked smoke")
    coverage.add_argument("--all-prompts", action="store_true", help="run the entire suite")
    ap.add_argument("--prompt-ids", default="", help="comma-separated suite prompt ids")
    ap.add_argument("--images", type=int, default=2, help="images per combination")
    ap.add_argument("--seed", type=int, help="override every suite seed")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--variants", default="", help="comma-separated local variants to audit")
    ap.add_argument("--wait", action="store_true", help="wait, validate files/provenance, and write a report")
    ap.add_argument("--dry-run", action="store_true", help="print the matrix, queue nothing")
    args = ap.parse_args()

    if args.images != 2:
        print("warning: this audit is designed for two images per combination", file=sys.stderr)
    selected_prompts = {value.strip() for value in args.prompt_ids.split(",") if value.strip()}
    try:
        if args.prompt_file:
            path = Path(args.prompt_file)
            if not path.is_absolute():
                path = ROOT / path
            suite_name, prompts = load_single(path, seed=args.seed or 12345)
        else:
            path = Path(args.suite)
            if not path.is_absolute():
                path = ROOT / path
            # A no-flag run is intentionally the two-prompt smoke suite. The
            # full matrix is explicit because it can take hours on a 16 GB card.
            suite_name, prompts = load_suite(
                path,
                smoke_only=args.smoke or (not args.all_prompts and not selected_prompts),
                selected=selected_prompts or None,
                seed_override=args.seed,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid prompt input: {exc}", file=sys.stderr)
        return 2

    selected = {name.strip() for name in args.variants.split(",") if name.strip()}
    plan = [row for row in combinations() if not selected or row[0] in selected]
    unknown = selected - {variant.name for variant in variants.available("local")}
    if unknown:
        print(f"unknown local variant(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    if not plan:
        print("no configured local combinations match the selection", file=sys.stderr)
        return 2
    total_jobs = len(plan) * len(prompts)
    print(f"suite: {suite_name} ({len(prompts)} prompt(s), text not echoed)")
    print(f"--- local: {len(plan)} combinations x {len(prompts)} prompts x {args.images}"
          f" = {total_jobs * args.images} images ---")
    for variant, adapter in plan:
        profile = QUALITY_PROFILES[variant]
        label = adapter["name"] if adapter else "base model"
        print(f"    {variant:9s} + {label:24s}  {profile['steps']:>2} steps / CFG {profile['guidance']:g}")
    print(f"\n{total_jobs} jobs, {total_jobs * args.images} images total")
    if args.dry_run:
        return 0

    queued = failed = 0
    submitted: list[dict] = []
    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        for bench in prompts:
            for variant, adapter in plan:
                body = payload(
                    variant, adapter, bench.prompt, args.images, bench.seed,
                    width=bench.width, height=bench.height,
                )
                candidate = f"local/{variant}+{adapter['name'] if adapter else 'base'}"
                label = f"{candidate}/{bench.id}"
                try:
                    # The routes take a JSON string in their form field so image
                    # generators can share a signature with upload endpoints.
                    response = client.post(ENDPOINT, data={"payload": json.dumps(body)})
                    response.raise_for_status()
                    job = response.json()
                    job_id = int(job.get("job_id") or job.get("id"))
                    print(f"  queued job {job_id:>4}  {label}")
                    queued += 1
                    submitted.append({
                        "job_id": job_id,
                        "label": label,
                        "candidate": candidate,
                        "suite": suite_name,
                        "prompt_id": bench.id,
                        "prompt_category": bench.category,
                        "images": args.images,
                        "settings": {
                            k: body[k]
                            for k in ("model_variant", "steps", "guidance", "sampler", "aspect",
                                      "width", "height", "loras")
                        },
                        "expected": {
                            "model": variants.resolve(variant, lane="local"),
                            "loras": [adapter["path"]] if adapter else [],
                        },
                        "prompt_sha256": bench.sha256,
                    })
                except Exception as exc:  # noqa: BLE001 — keep independent rows testable
                    print(f"  FAILED {label}: {type(exc).__name__} {exc}", file=sys.stderr)
                    failed += 1
        if args.wait and submitted:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            report = ROOT / "output" / "benchmarks" / f"local-model-audit-{stamp}.json"
            failed += wait_and_validate(client, submitted, report)

    print(f"\nqueued {queued} job(s), {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
