#!/usr/bin/env python3
"""Run the same two-seed quality audit against the configured Remote GPU.

It tests every configured A100 image-model slot and, when the live Remote GPU worker
reports it available, the Qwen Image Lightning speed LoRA as a separate
performance profile. Local ``models/loras`` files are intentionally excluded:
they never reach the Remote GPU and pretending otherwise would create a
misleading model/adapter matrix.

Run after the backend and Remote GPU service are healthy:

    source scripts/project-env.sh
    .venv/bin/python scripts/a100_sampling_matrix.py --wait
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.app.config import settings
from backend.app.generators import variants
from scripts.benchmark_prompts import load_single, load_suite
from scripts.sampling_matrix import wait_and_validate

ENDPOINT = "/api/generate/image/remote"

# Qwen-Image-2512's official Diffusers example uses 50 steps, true CFG 4.0,
# 1056x1584 for 2:3, and this short Chinese quality negative. Flux2Klein's
# pipeline defaults are likewise 50 / CFG 4 but it has no negative-prompt API.
PROFILES: dict[str, dict[str, Any]] = {
    "quality": {
        "steps": 50,
        "guidance": 4.0,
        # Qwen's official final-quality Diffusers example uses this exact 2:3
        # bucket. ``quality=Custom`` is required to keep its supplied steps,
        # so make the dimensions explicit instead of accidentally inheriting
        # the application's generic Standard-size fallback.
        "width": 1056,
        "height": 1584,
        "negative_prompt": (
            "低分辨率，低画质，肢体畸形，手指畸形，画面过饱和，蜡像感，人脸无细节，"
            "过度光滑，画面具有AI感。构图混乱。文字模糊，扭曲。"
        ),
        "why": "Qwen-Image-2512 official final-quality recipe",
    },
    "hidream": {
        "steps": 50, "guidance": 5.0, "width": 1024, "height": 1024,
        "negative_prompt": "",
        "why": "HiDream-O1-Image official full-model recipe",
    },
    "alt": {
        # The model card's documented Flux2KleinPipeline recipe is 1024 square,
        # 50 steps and guidance 4. It does not document a portrait bucket.
        "steps": 50, "guidance": 4.0, "width": 1024, "height": 1024,
        "negative_prompt": "",
        "why": "Flux2KleinPipeline model-card recipe; no string negative-prompt input",
    },
}


def _payload(variant: str, prompt: str, images: int, seed: int, *, speed: bool,
             width: int | None = None, height: int | None = None) -> dict[str, Any]:
    profile = PROFILES[variant]
    width = width or profile["width"]
    height = height or profile["height"]
    if speed:
        # The worker changes a Lightning run to 4–8 steps / CFG 1.
        return {
            "prompt": prompt, "negative_prompt": "", "auto_negative": False,
            "model_variant": variant, "batch": images, "seed": seed,
            "seed_mode": "increment", "quality": "Custom", "steps": 4,
            "guidance": 1.0, "aspect": "Custom",
            "width": width, "height": height,
            "finish": "none", "speed_mode": True,
        }
    return {
        "prompt": prompt, "negative_prompt": profile["negative_prompt"], "auto_negative": False,
        "model_variant": variant, "batch": images, "seed": seed,
        "seed_mode": "increment", "quality": "Custom", "steps": profile["steps"],
        "guidance": profile["guidance"], "aspect": "Custom",
        "width": width, "height": height,
        "finish": "none", "speed_mode": False,
    }


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
    ap.add_argument("--images", type=int, default=2)
    ap.add_argument("--seed", type=int, help="override every suite seed")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--wait", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--variants", nargs="+", choices=sorted(PROFILES),
                    help="Recheck only these configured A100 slots.")
    args = ap.parse_args()

    selected_prompts = {value.strip() for value in args.prompt_ids.split(",") if value.strip()}
    try:
        if args.prompt_file:
            prompt_file = Path(args.prompt_file)
            if not prompt_file.is_absolute():
                prompt_file = ROOT / prompt_file
            suite_name, prompts = load_single(prompt_file, seed=args.seed or 12345,
                                               width=1024, height=1024)
        else:
            prompt_file = Path(args.suite)
            if not prompt_file.is_absolute():
                prompt_file = ROOT / prompt_file
            suite_name, prompts = load_suite(
                prompt_file,
                smoke_only=args.smoke or (not args.all_prompts and not selected_prompts),
                selected=selected_prompts or None,
                seed_override=args.seed,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid prompt input: {exc}", file=sys.stderr)
        return 2
    if args.images != 2:
        print("warning: this audit is designed for two images per combination", file=sys.stderr)

    configured = [v.name for v in variants.available("colab") if v.name in PROFILES]
    if args.variants:
        requested = set(args.variants)
        configured = [name for name in configured if name in requested]
        missing = requested - set(configured)
        if missing:
            print(f"requested but not configured: {', '.join(sorted(missing))}", file=sys.stderr)
            return 2
    if not configured:
        print("no supported A100 image model slots are configured", file=sys.stderr)
        return 2

    def print_plan(plan: list[tuple[str, bool]]) -> None:
        total_jobs = len(plan) * len(prompts)
        print(f"suite: {suite_name} ({len(prompts)} prompt(s), text not echoed)")
        print(f"--- A100: {len(plan)} combinations x {len(prompts)} prompts x {args.images}"
              f" = {total_jobs * args.images} images ---")
        for variant, speed in plan:
            first = prompts[0]
            profile = _payload(
                variant, first.prompt, args.images, first.seed, speed=speed,
                width=first.width, height=first.height,
            )
            name = "Qwen Lightning" if speed else "base model"
            print(
                f"    {variant:9s} + {name:16s}  {profile['width']}×{profile['height']}"
                f"  {profile['steps']:>2} steps / CFG {profile['guidance']:g}"
            )

    if args.dry_run:
        # Planning is deliberately offline. The live run rechecks advertised
        # features before queueing so a stale worker can never impersonate a
        # candidate, but users should be able to inspect cost before starting it.
        plan = [(variant, False) for variant in configured]
        if settings.image_lightning_lora and "quality" in configured:
            plan.append(("quality", True))
        print_plan(plan)
        return 0

    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        system = client.get("/api/system").json()
        remote = system.get("remote_gpu") or {}
        if not remote.get("connected") or not remote.get("worker_alive"):
            print("Remote GPU service is not healthy; do not queue an audit", file=sys.stderr)
            return 2
        features = set(remote.get("features") or [])
        # A stale worker would otherwise accept the new variant name and
        # silently fall back to Qwen. Test only capabilities the live A100
        # explicitly advertises; the report must never claim a LoRA ran there
        # when it was not loaded.
        if "hidream" in configured and "image_hidream" not in features:
            configured.remove("hidream")
            print(
                "note: configured HiDream A100 slot is not live; "
                "run the regenerated Remote GPU runner first"
            )
        plan = [(variant, False) for variant in configured]
        if "speed_image" in features and "quality" in configured:
            plan.append(("quality", True))

        print_plan(plan)

        submitted: list[dict[str, Any]] = []
        failures = 0
        for bench in prompts:
            for variant, speed in plan:
                body = _payload(
                    variant, bench.prompt, args.images, bench.seed, speed=speed,
                    width=bench.width, height=bench.height,
                )
                suffix = "qwen-image-lightning" if speed else "base"
                candidate = f"a100/{variant}+{suffix}"
                label = f"{candidate}/{bench.id}"
                try:
                    response = client.post(ENDPOINT, data={"payload": json.dumps(body)})
                    response.raise_for_status()
                    job_id = int(response.json().get("job_id"))
                    print(f"  queued job {job_id:>4}  {label}")
                    submitted.append({
                        "job_id": job_id, "label": label, "candidate": candidate,
                        "suite": suite_name, "prompt_id": bench.id,
                        "prompt_category": bench.category, "images": args.images,
                        "settings": {
                            k: body[k]
                            for k in ("model_variant", "steps", "guidance", "width", "height",
                                      "aspect", "speed_mode")
                        },
                        "expected": {
                            "model": variants.resolve(variant, lane="colab"),
                            "loras": [],
                        },
                        "prompt_sha256": bench.sha256,
                    })
                except Exception as exc:  # noqa: BLE001 — keep other slots testable
                    print(f"  FAILED {label}: {type(exc).__name__} {exc}", file=sys.stderr)
                    failures += 1
        if args.wait and submitted:
            report = (
                ROOT / "output" / "benchmarks"
                / f"a100-model-audit-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
            )
            failures += wait_and_validate(client, submitted, report)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
