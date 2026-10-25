"""Import a model or LoRA file from this machine into the managed library.

The local counterpart of `POST /api/resources/uploads` + `/import`: the file
is copied (never moved or changed) through the same quarantine, format and
structure checks, deduplicated by content, and promoted atomically. Only
`.safetensors` is accepted; pickle-based formats are refused.

Examples:
    .venv/bin/python scripts/import_resource.py ~/Downloads/ink_wash.safetensors
    .venv/bin/python scripts/import_resource.py --kind model ~/Downloads/base.safetensors
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app import db
from backend.app.problems import RequestError
from backend.app.resources import intake


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path, help="A .safetensors file on this machine.")
    parser.add_argument("--kind", choices=intake.KINDS, default="adapter")
    parser.add_argument("--label", default="", help="A display name.")
    args = parser.parse_args()
    db.init_db()
    try:
        result = intake.import_path(args.path.expanduser(), kind=args.kind, label=args.label)
    except RequestError as error:
        print(f"refused ({error.code}): {error.detail}", file=sys.stderr)
        return 1
    resource = result["resource"]
    print(json.dumps({"status": result["status"], "id": resource["id"],
                      "kind": resource["kind"], "family": resource["family"],
                      "installs": resource["installs"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
