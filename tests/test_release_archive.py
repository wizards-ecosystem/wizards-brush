"""An archive integrity check must remain data-only unless explicitly trusted."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.build_release import REQUIRED_BUNDLE_FILES, verify_archive


def _archive(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = "wizards-brush-test-linux-x86_64"
    archive = tmp_path / "bundle.tar.xz"
    with tarfile.open(archive, "w:xz") as bundle:
        directory = tarfile.TarInfo(root)
        directory.type = tarfile.DIRTYPE
        bundle.addfile(directory)
        for name, content in sorted(files.items()):
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(content)
            info.mode = 0o755 if name in {"install.sh", "start.sh"} else 0o644
            bundle.addfile(info, io.BytesIO(content))
    return archive


def _valid_files(marker: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = dict.fromkeys(
        REQUIRED_BUNDLE_FILES - {"MANIFEST.sha256"}, b"placeholder\n"
    )
    files["RELEASE-MANIFEST.json"] = json.dumps({
        "schema": 1,
        "name": "The Wizard's Brush",
        "version": "0.0.0-test",
        "target": "linux-x86_64",
        "dirty_build": False,
    }).encode()
    files["install.sh"] = f"#!/bin/sh\ntouch '{marker}'\n".encode()
    lines = [
        f"{hashlib.sha256(content).hexdigest()}  {name}"
        for name, content in sorted(files.items())
    ]
    files["MANIFEST.sha256"] = ("\n".join(lines) + "\n").encode()
    return files


def test_default_verification_never_executes_archive_code(tmp_path):
    marker = tmp_path / "archive-code-ran"
    archive = _archive(tmp_path, _valid_files(marker))
    verify_archive(archive)
    assert not marker.exists()


def test_manifest_must_cover_every_file_exactly_once(tmp_path):
    marker = tmp_path / "unused"
    files = _valid_files(marker)
    files["unlisted.txt"] = b"not in inner manifest"
    archive = _archive(tmp_path, files)
    with pytest.raises(RuntimeError, match="coverage mismatch"):
        verify_archive(archive)
