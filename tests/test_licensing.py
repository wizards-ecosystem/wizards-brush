"""The project is Apache-2.0. These tests are what keep that true.

A licence is not a file you add once. It is a set of claims — about what the
project is, what it reuses, and what it may not copy — and every one of them can
quietly stop being true as the code moves. Each test here pins one of those
claims to something mechanical.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_licence_file_exists_and_is_apache_2():
    """Without this file the project is all rights reserved, whatever the README
    says. That is the difference between 'FOSS project' and 'source you can look
    at'."""
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text
    assert "END OF TERMS AND CONDITIONS" in text, "the text is truncated"


def test_the_licence_text_is_unmodified():
    """It is a legal instrument, not project prose. Editing it — even to add our
    own name in the appendix — makes it something other than Apache-2.0."""
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright [yyyy] [name of copyright owner]" in text, \
        "the appendix boilerplate must stay as placeholders; ours belongs in NOTICE"
    assert "Wizards Brush" not in text


def test_notice_credits_the_apache_code_we_reuse():
    """Apache-2.0 s4(d): attribution notices travel with derivative works. This is
    the file that carries them."""
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    for required in ("SD.Next", "InvokeAI", "SwarmUI", "Apache License, Version 2.0"):
        assert required in notice, f"NOTICE does not credit {required}"


def test_notice_covers_the_optional_hidream_runtime_fetch():
    """The remote runner fetches this optional upstream at runtime, not from a
    vendored checkout. The attribution must remain visible with that boundary."""
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    for required in ("HiDream-O1-Image", "HiDream.ai", "MIT License",
                     "remote_gpu.py", "not bundled"):
        assert required in notice, f"NOTICE does not describe {required}"


def test_notice_records_the_gpl_boundary():
    """The two GPL projects must be named as read-and-reimplemented. If that
    boundary is only in a code comment it cannot be audited from outside."""
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "ComfyUI" in notice and "krita-ai-diffusion" in notice
    assert "GPL-3.0" in notice
    assert "NOT copied" in notice


def test_notice_records_the_agpl_and_later_study_boundaries():
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    for required in ("stable-diffusion-webui", "locally-uncensored", "Fooocus",
                     "Uncensored-Local-Studio", "open-generative-ai", "AGPL-3.0"):
        assert required in notice, f"NOTICE does not record the {required} study boundary"


def test_package_metadata_declares_the_licence():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["license"] == "Apache-2.0"
    assert set(data["project"]["license-files"]) == {"LICENSE", "NOTICE"}

    pkg = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert pkg["license"] == "Apache-2.0", "the frontend is part of the same project"


def test_the_two_halves_agree_on_the_version():
    """Declared in pyproject.toml and package.json. They are separate files that
    must not drift, and nothing else notices when they do."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pkg = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert data["project"]["version"] == pkg["version"]


def test_the_running_version_matches_what_is_declared():
    from backend.app.version import UNKNOWN, get_version

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    got = get_version()
    assert got != UNKNOWN, "the version could not be resolved at runtime"
    assert got == data["project"]["version"]


def test_the_upstream_checkouts_can_never_be_committed():
    """`explorations/` holds 400+ MB of upstream clones, two of them GPL-3.0. It
    was protected only by .git/info/exclude, which is local to one machine and
    does not survive a clone. A stray `git add -A` elsewhere would put GPL
    sources into an Apache-2.0 repository."""
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "explorations/" in ignore


def test_shipped_code_never_points_at_files_that_do_not_ship():
    """The build plans in `explorations/` are private. A user-facing error that
    cites one sends someone to a file they cannot possibly have."""
    for pattern in ("backend/**/*.py", "frontend/src/**/*.ts", "frontend/src/**/*.tsx"):
        for path in ROOT.glob(pattern):
            assert "explorations/" not in path.read_text(encoding="utf-8"), \
                f"{path.relative_to(ROOT)} references the private explorations/ tree"


def test_no_module_reaches_into_another_modules_privates():
    """A leading underscore is a contract about what may be depended on. Four
    places had crossed it — a mutable health dict, a pipeline-class resolver, and
    a model-id helper that was private on the settings object and called from
    three other modules. Each now has a public accessor that can be changed
    behind, which is the whole point of the underscore.

    Only *module* attributes are checked. Reaching into a third-party object —
    diffusers exposes `pipe._unpack_latents` as de-facto API — is a different
    decision, made deliberately and guarded with `hasattr` where it occurs.
    """
    import ast

    offenders: list[str] = []
    for path in (ROOT / "backend").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level:
                # `from . import db, queue` — exactly the relative-module form
                # that makes `db._migrate` reachable.
                modules.update(a.asname or a.name for a in node.names)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and node.attr.startswith("_")
                    and not node.attr.startswith("__")
                    and isinstance(node.value, ast.Name)
                    and node.value.id in modules):
                offenders.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} "
                    f"{node.value.id}.{node.attr}")
    assert not offenders, "cross-module private access:\n  " + "\n  ".join(offenders)


def test_claude_delegates_to_agents_and_agents_documents_the_licence_boundary():
    """Every agent reads one canonical guide, whichever compatibility filename
    its host looks for. The canonical guide must retain the copyleft boundary."""
    assert (ROOT / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Apache-2.0" in text
    assert "Read and reimplement" in text
    assert "explorations/" in text
