"""One-time, lossless migration into the self-contained directory contract."""
from __future__ import annotations

import filecmp
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
MODELS = ROOT / "models"


def _require_inside(path: Path) -> None:
    try:
        path.resolve().relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeError(f"refusing path outside checkout: {path} -> {path.resolve()}") from exc


def _merge(source: Path, destination: Path) -> None:
    """Move a tree without overwriting a different existing file."""
    _require_inside(source)
    _require_inside(destination)
    if not source.exists() and not source.is_symlink():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() and not destination.is_symlink():
        source.rename(destination)
        print(f"moved {source.relative_to(ROOT)} -> {destination.relative_to(ROOT)}")
        return
    if source.is_symlink() or source.is_file():
        if destination.is_file() and filecmp.cmp(source, destination, shallow=False):
            source.unlink()
            return
        raise RuntimeError(f"migration conflict: {source} and {destination}")
    if not destination.is_dir():
        raise RuntimeError(f"migration conflict: {destination} is not a directory")
    for child in source.iterdir():
        _merge(child, destination / child.name)
    source.rmdir()


def _rewrite_env() -> None:
    path = ROOT / ".env"
    if not path.exists() and not path.is_symlink():
        return
    _require_inside(path)
    wanted = {
        "HF_HOME": "./models/huggingface",
        "LORA_DIR": "./models/loras",
        "WEIGHTS_DIR": "./models/weights",
        "RUNTIME_DIR": "./.runtime",
        "OUTPUT_DIR": "./output",
    }
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    seen: set[str] = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in wanted:
            lines[index] = f"{key}={wanted[key]}"
            seen.add(key)
    if missing := [key for key in wanted if key not in seen]:
        lines.extend(["", "# Project-local storage paths (managed by The Wizard's Brush)"])
        lines.extend(f"{key}={wanted[key]}" for key in missing)
    rendered = "\n".join(lines) + "\n"
    if rendered != original:
        path.write_text(rendered, encoding="utf-8")
        print("updated .env storage paths")


def _python_version(path: Path) -> tuple[int, int, int]:
    match = re.match(r"cpython-(\d+)\.(\d+)\.(\d+)-", path.parents[1].name)
    if not match:
        return (0, 0, 0)
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _local_python() -> Path | None:
    candidates = list((RUNTIME / "python").glob("cpython-3.12*-*/bin/python3.12"))
    if not candidates:
        return None
    selected = max(candidates, key=_python_version)
    _require_inside(selected)
    return selected.resolve()


def _relink_venv() -> None:
    venv = ROOT / ".venv"
    python = _local_python()
    if not venv.exists() or python is None:
        return
    _require_inside(venv)
    link = venv / "bin" / "python"
    relative = os.path.relpath(python, link.parent)
    changed = not link.is_symlink() or os.readlink(link) != relative
    if changed:
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(relative)
    cfg = venv / "pyvenv.cfg"
    if cfg.exists():
        original = cfg.read_text(encoding="utf-8")
        lines = original.splitlines()
        home = f"home = {python.parent}"
        lines = [home if line.startswith("home = ") else line for line in lines]
        rendered = "\n".join(lines) + "\n"
        if rendered != original:
            cfg.write_text(rendered, encoding="utf-8")
            changed = True
    output = subprocess.check_output(
        [str(link), "-c", "import sys; print(sys.base_prefix)"], text=True
    ).strip()
    resolved = Path(output)
    resolved.relative_to(ROOT)
    if changed:
        print("relinked .venv to the project-owned Python")


def main() -> None:
    _require_inside(MODELS)
    MODELS.mkdir(parents=True, exist_ok=True)
    _merge(ROOT / "hf_cache", MODELS / "huggingface")
    _merge(ROOT / "weights", MODELS / "weights")
    _merge(ROOT / "loras", MODELS / "loras")
    _rewrite_env()
    _relink_venv()
    print("self-contained storage migration complete")


if __name__ == "__main__":
    main()
