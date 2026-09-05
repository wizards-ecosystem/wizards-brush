#!/usr/bin/env python3
"""Export only high-confidence human-reviewed images for a future LoRA dataset.

This never trains or uploads anything. It creates a JSONL manifest whose rows
point at approved local assets and retain their prompt, model provenance and
review notes. Use it as the auditable input to a separate, consented training
run; keep a held-out portion for the fixed-seed regression matrix.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlmodel import select

from backend.app.db import session
from backend.app.models import Asset, AssetKind


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-visual", type=int, default=4, choices=range(1, 6))
    ap.add_argument("--min-prompt", type=int, default=4, choices=range(1, 6))
    ap.add_argument("--output", default="output/training/reviewed-images.jsonl")
    args = ap.parse_args()
    output = (ROOT / args.output).resolve() if not Path(args.output).is_absolute() else Path(args.output)
    try:
        output.relative_to(ROOT)
    except ValueError:
        ap.error("--output must stay inside the project")
    output.parent.mkdir(parents=True, exist_ok=True)
    kept = 0
    with session() as s, output.open("w", encoding="utf-8") as fh:
        rows = s.exec(select(Asset).where(Asset.kind == AssetKind.image.value,
                                          Asset.deleted_at.is_(None)))  # type: ignore[union-attr]
        for asset in rows:
            grade = asset.grade
            visual, prompt = grade.get("visual_quality"), grade.get("prompt_fidelity")
            if not isinstance(visual, int) or not isinstance(prompt, int):
                continue
            if visual < args.min_visual or prompt < args.min_prompt:
                continue
            meta = asset.meta
            row = {
                "image": str(Path(asset.path).resolve().relative_to(ROOT)),
                "prompt": meta.get("prompt", ""),
                "model": meta.get("model", ""),
                "loras": meta.get("loras", []),
                "review": {"visual_quality": visual, "prompt_fidelity": prompt,
                           "notes": grade.get("notes", "")},
            }
            fh.write(json.dumps(row) + "\n")
            kept += 1
    print(f"Exported {kept} approved reviewed images to {output.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
