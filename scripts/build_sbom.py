"""Generate deterministic CycloneDX SBOMs from the locked dependency graphs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "release"
TEMP_ROOT = ROOT / ".runtime" / "tmp"
REPOSITORY = "https://github.com/wizards-ecosystem/wizards-brush"


def _run(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True)


def _load_json(raw: str, *, source: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{source} did not produce valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{source} did not produce a JSON object")
    return value


def _version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    frontend = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    if frontend["version"] != version:
        raise RuntimeError("backend and frontend versions do not match")
    return version


def _normalize(document: dict[str, Any], *, kind: str, commit: str, epoch: int) -> bytes:
    if (document.get("bomFormat") != "CycloneDX"
            or document.get("specVersion") != "1.5"
            or not isinstance(document.get("components"), list)):
        raise RuntimeError(f"{kind} generator returned an unexpected CycloneDX document")

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise RuntimeError(f"{kind} SBOM has no metadata object")
    metadata["timestamp"] = datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")
    properties = metadata.setdefault("properties", [])
    if not isinstance(properties, list):
        raise RuntimeError(f"{kind} SBOM metadata properties are malformed")
    properties.append({"name": "wizards-brush:source:commit", "value": commit})

    document.pop("serialNumber", None)
    identity = json.dumps(document, sort_keys=True, separators=(",", ":"))
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"{REPOSITORY}/sbom/{kind}/{identity}")
    document["serialNumber"] = f"urn:uuid:{serial}"
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def generate(*, write: bool) -> list[Path]:
    version = _version()
    commit = _run("git", "rev-parse", "HEAD").strip()
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", _run(
        "git", "show", "-s", "--format=%ct", "HEAD"
    ).strip()))
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="wizards-brush-sbom-", dir=TEMP_ROOT) as raw:
        temp = Path(raw)
        python_path = temp / "python.cdx.json"
        subprocess.run(
            [
                "uv", "export", "--preview-features", "sbom-export", "--frozen", "--no-dev",
                "--format", "cyclonedx1.5", "--output-file", str(python_path),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        python_document = _load_json(
            python_path.read_text(encoding="utf-8"), source="uv export"
        )
        frontend_document = _load_json(
            _run(
                "npm", "sbom", "--sbom-format=cyclonedx", "--sbom-type=application",
                cwd=ROOT / "frontend",
            ),
            source="npm sbom",
        )

    documents = {
        f"wizards-brush-{version}-python.cdx.json": _normalize(
            python_document, kind="python", commit=commit, epoch=epoch
        ),
        f"wizards-brush-{version}-frontend.cdx.json": _normalize(
            frontend_document, kind="frontend", commit=commit, epoch=epoch
        ),
    }
    paths = [OUTPUT / name for name in documents]
    if write:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        for path, content in zip(paths, documents.values(), strict=True):
            path.write_bytes(content)
            path.with_suffix(path.suffix + ".sha256").write_text(
                f"{_sha256(content)}  {path.name}\n", encoding="utf-8"
            )
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="generate and validate both SBOMs without writing them"
    )
    args = parser.parse_args()
    paths = generate(write=not args.check)
    action = "validated" if args.check else "wrote"
    print(f"{action} CycloneDX SBOMs: {', '.join(path.name for path in paths)}")


if __name__ == "__main__":
    main()
