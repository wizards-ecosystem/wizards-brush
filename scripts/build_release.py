"""Build and verify the minimal standalone Linux release archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "release"
FILE_LIST = ROOT / "packaging" / "release-files.txt"
BUNDLE_README = ROOT / "packaging" / "BUNDLE_README.md"
REPOSITORY = "https://github.com/wizards-ecosystem/wizards-brush"
TARGET = "linux-x86_64"
REQUIRED_BUNDLE_FILES = {
    ".env.example",
    "LICENSE",
    "MANIFEST.sha256",
    "NOTICE",
    "README.md",
    "RELEASE-MANIFEST.json",
    "THIRD_PARTY_NOTICES.md",
    "frontend/dist/index.html",
    "install.sh",
    "start.sh",
}
FORBIDDEN_PARTS = {
    ".git",
    ".runtime",
    ".venv",
    "__pycache__",
    "node_modules",
    "output",
    "tests",
}
FORBIDDEN_SUFFIXES = {".ckpt", ".db", ".onnx", ".pth", ".pyc", ".safetensors"}
MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024


def _run(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    frontend = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    expected_citation = f"version: {version}"
    if frontend["version"] != version or expected_citation not in citation.splitlines():
        raise RuntimeError("version mismatch among pyproject.toml, frontend/package.json, and CITATION.cff")
    return version


def _manifest_entries() -> list[Path]:
    entries: set[Path] = set()
    for raw in FILE_LIST.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        tracked = _run("git", "ls-files", "--", value).splitlines()
        if not tracked:
            raise RuntimeError(f"release input is missing or untracked: {value}")
        entries.update(ROOT / item for item in tracked)
    for path in entries:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"release input must be a regular file: {path.relative_to(ROOT)}")
    return sorted(entries)


def _validate_layout(*, require_frontend: bool) -> tuple[str, list[Path]]:
    version = _version()
    entries = _manifest_entries()
    relative = {str(path.relative_to(ROOT)) for path in entries}
    for required in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "install.sh", "start.sh"):
        if required not in relative:
            raise RuntimeError(f"required release file is absent from the manifest: {required}")
    if any(part in FORBIDDEN_PARTS for value in relative for part in PurePosixPath(value).parts):
        raise RuntimeError("release manifest includes a forbidden development/runtime path")
    if any(Path(value).suffix.lower() in FORBIDDEN_SUFFIXES for value in relative):
        raise RuntimeError("release manifest includes a model, database, bytecode, or tool-weight file")
    frontend = ROOT / "frontend" / "dist" / "index.html"
    if require_frontend and not frontend.is_file():
        raise RuntimeError("frontend/dist is missing; run the production frontend build first")

    tag_result = subprocess.run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    exact_tag = tag_result.stdout.strip() if tag_result.returncode == 0 else ""
    if exact_tag and exact_tag != f"v{version}":
        raise RuntimeError(f"tag {exact_tag} does not match project version {version}")
    return version, entries


def _copy_inputs(stage: Path, entries: list[Path]) -> None:
    for source in entries:
        relative = source.relative_to(ROOT)
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    shutil.copyfile(BUNDLE_README, stage / "README.md")
    frontend_root = ROOT / "frontend" / "dist"
    for source in sorted(path for path in frontend_root.rglob("*") if path.is_file()):
        if source.is_symlink():
            raise RuntimeError(f"prebuilt frontend contains a symlink: {source}")
        destination = stage / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _write_inner_manifest(stage: Path, *, version: str, dirty: bool, epoch: int) -> None:
    release_manifest = {
        "schema": 1,
        "name": "The Wizard's Brush",
        "version": version,
        "target": TARGET,
        "source_repository": REPOSITORY,
        "source_commit": _run("git", "rev-parse", "HEAD"),
        "source_date_epoch": epoch,
        "dirty_build": dirty,
        "install": "./install.sh --with-models",
        "start": "./start.sh",
        "contents_checksum": "MANIFEST.sha256",
    }
    (stage / "RELEASE-MANIFEST.json").write_text(
        json.dumps(release_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    lines = []
    for path in sorted(item for item in stage.rglob("*") if item.is_file()):
        relative = path.relative_to(stage).as_posix()
        if relative != "MANIFEST.sha256":
            lines.append(f"{_sha256(path)}  {relative}")
    (stage / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tar_filter(info: tarfile.TarInfo, *, epoch: int) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = epoch
    if info.isfile():
        executable = PurePosixPath(info.name).name in {"install.sh", "start.sh"}
        info.mode = 0o755 if executable else 0o644
    else:
        info.mode = 0o755
    return info


def verify_archive(archive: Path, *, runtime_smoke: bool = False) -> None:
    with tempfile.TemporaryDirectory(prefix="wizards-brush-verify-") as raw_temp:
        destination = Path(raw_temp)
        with tarfile.open(archive, "r:xz") as bundle:
            members = bundle.getmembers()
            if (len(members) > MAX_ARCHIVE_MEMBERS
                    or sum(member.size for member in members) > MAX_ARCHIVE_BYTES):
                raise RuntimeError("release archive exceeds verification limits")
            names: set[str] = set()
            for member in members:
                parts = PurePosixPath(member.name).parts
                if (member.name in names or member.name.startswith("/") or ".." in parts
                        or not (member.isfile() or member.isdir())):
                    raise RuntimeError(f"unsafe archive member: {member.name}")
                names.add(member.name)
            bundle.extractall(destination, filter="data")

        roots = list(destination.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise RuntimeError("release archive must contain exactly one top-level directory")
        root = roots[0]
        present = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
        missing = REQUIRED_BUNDLE_FILES - present
        if missing:
            raise RuntimeError(f"release archive is missing: {', '.join(sorted(missing))}")
        if any(part in FORBIDDEN_PARTS for value in present for part in PurePosixPath(value).parts):
            raise RuntimeError("release archive contains a forbidden development/runtime path")
        if any(Path(value).suffix.lower() in FORBIDDEN_SUFFIXES for value in present):
            raise RuntimeError("release archive contains generated data or model weights")

        manifest_entries: dict[str, str] = {}
        for line in (root / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
            try:
                expected, relative = line.split("  ", 1)
            except ValueError as exc:
                raise RuntimeError("malformed bundle checksum manifest") from exc
            parts = PurePosixPath(relative).parts
            if (len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected)
                    or not relative or relative.startswith("/") or ".." in parts
                    or relative == "MANIFEST.sha256" or relative in manifest_entries):
                raise RuntimeError(f"unsafe bundle checksum entry: {relative!r}")
            manifest_entries[relative] = expected
        expected_entries = present - {"MANIFEST.sha256"}
        if set(manifest_entries) != expected_entries:
            missing_checksums = expected_entries - set(manifest_entries)
            extra_checksums = set(manifest_entries) - expected_entries
            raise RuntimeError(
                "bundle checksum coverage mismatch "
                f"(missing={sorted(missing_checksums)}, extra={sorted(extra_checksums)})"
            )
        for relative, expected in manifest_entries.items():
            target = root / relative
            if not target.is_file() or _sha256(target) != expected:
                raise RuntimeError(f"bundle checksum mismatch: {relative}")
        try:
            release_data = json.loads(
                (root / "RELEASE-MANIFEST.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError("release metadata is not valid JSON") from exc
        if (not isinstance(release_data, dict) or release_data.get("schema") != 1
                or release_data.get("name") != "The Wizard's Brush"
                or release_data.get("target") != TARGET
                or not isinstance(release_data.get("version"), str)
                or not isinstance(release_data.get("dirty_build"), bool)):
            raise RuntimeError("release metadata does not match the bundle contract")
        if runtime_smoke:
            # This intentionally executes code from the archive. Callers must
            # authenticate the outer archive/checksum or attestation first.
            subprocess.run(["bash", "install.sh", "--check"], cwd=root, check=True)
            smoke_env = os.environ.copy()
            smoke_env.update(
                {
                    "PYTHONPATH": str(root),
                    "WARM_UP": "false",
                    "LOG_LEVEL": "WARNING",
                    "OUTPUT_DIR": str(root / "output"),
                    "HF_HOME": str(root / "models" / "huggingface"),
                    "LORA_DIR": str(root / "models" / "loras"),
                    "WEIGHTS_DIR": str(root / "models" / "weights"),
                    "RUNTIME_DIR": str(root / ".runtime"),
                    "WB_RELEASE_EXPECTED_VERSION": str(release_data["version"]),
                }
            )
            smoke = """
import os
from fastapi.testclient import TestClient
from backend.app.main import app
with TestClient(app, base_url='http://localhost') as client:
    index = client.get('/')
    status = client.get('/api/system')
    assert index.status_code == 200
    assert \"The Wizard's Brush\" in index.text
    assert status.status_code == 200
    assert status.json()['version'] == os.environ['WB_RELEASE_EXPECTED_VERSION']
print('extracted release runtime OK')
"""
            subprocess.run([sys.executable, "-c", smoke], cwd=root, env=smoke_env, check=True)


def build(*, allow_dirty: bool) -> Path:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise RuntimeError("the standalone bundle currently targets Linux x86-64")
    version, entries = _validate_layout(require_frontend=True)
    dirty = bool(_run("git", "status", "--porcelain", "--untracked-files=all"))
    if dirty and not allow_dirty:
        raise RuntimeError(
            "refusing a release from a dirty tree; commit or use --allow-dirty for local testing"
        )
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", _run("git", "show", "-s", "--format=%ct", "HEAD")))
    name = f"wizards-brush-{version}-{TARGET}"
    OUTPUT.mkdir(parents=True, exist_ok=True)
    archive = OUTPUT / f"{name}.tar.xz"

    with tempfile.TemporaryDirectory(prefix="wizards-brush-build-", dir=ROOT / ".runtime" / "tmp") as raw:
        stage = Path(raw) / name
        stage.mkdir()
        _copy_inputs(stage, entries)
        _write_inner_manifest(stage, version=version, dirty=dirty, epoch=epoch)
        temporary_archive = Path(raw) / archive.name
        with tarfile.open(temporary_archive, "w:xz", preset=9) as bundle:
            directory_info = tarfile.TarInfo(name)
            directory_info.type = tarfile.DIRTYPE
            bundle.addfile(_tar_filter(directory_info, epoch=epoch))
            for source in sorted(path for path in stage.rglob("*") if path.is_file()):
                arcname = f"{name}/{source.relative_to(stage).as_posix()}"
                info = _tar_filter(bundle.gettarinfo(str(source), arcname), epoch=epoch)
                with source.open("rb") as content:
                    bundle.addfile(info, content)
        os.replace(temporary_archive, archive)

    checksum = f"{_sha256(archive)}  {archive.name}\n"
    archive.with_suffix(archive.suffix + ".sha256").write_text(checksum, encoding="utf-8")
    verify_archive(archive, runtime_smoke=True)
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate release metadata and inputs only")
    parser.add_argument("--verify", type=Path, help="verify an existing release archive")
    parser.add_argument(
        "--runtime-smoke", action="store_true",
        help="execute the extracted installer/API (only after authenticating the archive)",
    )
    parser.add_argument("--allow-dirty", action="store_true", help="allow a marked local test artifact")
    args = parser.parse_args()
    if args.check:
        version, entries = _validate_layout(require_frontend=False)
        print(f"release layout OK for {version} ({len(entries)} tracked runtime files)")
        return
    if args.verify:
        verify_archive(args.verify.resolve(), runtime_smoke=args.runtime_smoke)
        print(f"release archive OK: {args.verify}")
        return
    archive = build(allow_dirty=args.allow_dirty)
    print(f"release archive OK: {archive.relative_to(ROOT)}")
    print(f"checksum: {archive.relative_to(ROOT)}.sha256")


if __name__ == "__main__":
    main()
