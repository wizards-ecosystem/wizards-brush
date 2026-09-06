"""Mechanical checks for the files people rely on before a public release."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from backend.app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
_REQUIRED_PUBLIC_FILES = (
    "README.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "CHANGELOG.md",
    "GOVERNANCE.md",
    "CITATION.cff",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "SUPPORT.md",
    "docs/remote-gpu.md",
    "docs/README.md",
    "docs/getting-started.md",
    "docs/user-guide.md",
    "docs/architecture.md",
    "docs/distribution.md",
    "docs/model-licenses.md",
    "docs/network-security.md",
    "docs/assets/brush-header.png",
    "docs/assets/readme-social.png",
    "scripts/check-host.sh",
    "scripts/build_release.py",
    "scripts/build_sbom.py",
    "packaging/release-files.txt",
    "packaging/BUNDLE_README.md",
    "install.sh",
    "start.sh",
    "docs/release-checklist.md",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/pull_request_template.md",
    ".github/workflows/release.yml",
)
_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]*]\(([^)]+)\)")
_PUBLIC_COPY = (
    "README.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "SUPPORT.md",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    ".env.example",
)
_PUBLIC_DOCS = (
    "README.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "SUPPORT.md",
    "CHANGELOG.md",
    "GOVERNANCE.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/README.md",
    "docs/getting-started.md",
    "docs/user-guide.md",
    "docs/architecture.md",
    "docs/distribution.md",
    "docs/model-licenses.md",
    "docs/network-security.md",
    "docs/exploration-mining-ledger.md",
    "docs/release-checklist.md",
    "docs/remote-gpu.md",
)


def test_public_release_files_exist():
    missing = [path for path in _REQUIRED_PUBLIC_FILES if not (ROOT / path).is_file()]
    assert not missing, f"missing public-release files: {', '.join(missing)}"


def test_tracked_sources_exclude_the_retired_label():
    """Keep the removed label out of code, docs, tests, and release metadata."""
    forbidden = "ns" + "fw"
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True,
    ).stdout.split(b"\0")
    matches: list[str] = []
    for raw_name in listed:
        if not raw_name:
            continue
        path = ROOT / raw_name.decode("utf-8")
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if forbidden in text.casefold():
            matches.append(str(path.relative_to(ROOT)))
    assert not matches, "retired label found in: " + ", ".join(matches)


def test_remote_worker_has_no_dedicated_paired_model_slot_or_safety_bypass():
    from backend.app.generators import variants

    remote = (ROOT / "remote_gpu.py").read_text(encoding="utf-8")
    retired_fields = {
        "a100_image_model_private",
        "a100_image_lora_private",
        "a100_image_lora_private_weight",
    }
    assert retired_fields.isdisjoint(Settings.model_fields)
    assert "private" not in {variant.name for variant in variants.VARIANTS}
    assert all(field not in remote for field in retired_fields)
    assert re.search(r"safety_checker\s*=\s*None", remote) is None


def test_local_links_in_public_markdown_resolve():
    """Broken relative links make a polished README feel unfinished immediately."""
    docs = [ROOT / name for name in _PUBLIC_DOCS]
    broken: list[str] = []
    for doc in docs:
        for raw_target in _MARKDOWN_LINK.findall(doc.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>")
            if target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            target = target.split("#", 1)[0]
            if target and not (doc.parent / target).is_file():
                broken.append(f"{doc.relative_to(ROOT)} -> {raw_target}")
    assert not broken, "broken local Markdown links:\n  " + "\n  ".join(broken)


def test_public_copy_uses_plain_ascii_dashes():
    """Keep rendered release copy consistent with the project's plain style."""
    paths = [ROOT / name for name in (*_PUBLIC_COPY, *_PUBLIC_DOCS)]
    em_dashes = [str(path.relative_to(ROOT)) for path in paths
                 if "—" in path.read_text(encoding="utf-8")]
    assert not em_dashes, "public copy contains em dashes: " + ", ".join(em_dashes)


def test_remote_gpu_configuration_uses_neutral_names_with_legacy_fallback():
    """Existing private installs keep working without advertising a notebook provider."""
    modern = Settings(
        REMOTE_GPU_BASE_URL="https://gpu.example.test",
        REMOTE_GPU_SHARED_SECRET="modern-secret",
    )
    legacy = Settings(
        COLAB_BASE_URL="https://legacy.example.test",
        COLAB_SHARED_SECRET="legacy-secret",
    )
    assert modern.remote_gpu_base_url == "https://gpu.example.test"
    assert modern.remote_gpu_shared_secret == "modern-secret"
    assert legacy.remote_gpu_base_url == "https://legacy.example.test"
    assert legacy.remote_gpu_shared_secret == "legacy-secret"


def test_release_gate_and_ci_cover_the_renamed_remote_runner():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert ".DEFAULT_GOAL := help" in makefile
    assert "release-check: check doctor offline-check optional-check" in makefile
    assert "sbom-check" in makefile and "scripts/build_sbom.py" in makefile
    assert "remote_gpu.py" in workflow
    assert "colab.py" not in workflow


def test_workflows_pin_actions_and_release_credentials_are_job_scoped():
    workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
    floating: list[str] = []
    for workflow in workflows:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            match = re.search(r"\buses:\s*[^@\s]+@([^\s#]+)", line)
            if match and not re.fullmatch(r"[0-9a-f]{40}", match.group(1)):
                floating.append(f"{workflow.name}: {line.strip()}")
    assert not floating, "floating GitHub Actions refs: " + ", ".join(floating)

    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "  build:" in release and "  attest:" in release and "  publish:" in release
    assert "environment: release" in release
    assert release.count("GH_TOKEN:") == 1
    assert "permissions:\n  contents: read" in release
    assert "release/*.cdx.json release/*.cdx.json.sha256" in release
    assert '--repo "$GITHUB_REPOSITORY"' in release
    assert "subject-path: release/*" in release


def test_public_defaults_are_local_only_and_license_conservative():
    assert Settings.model_fields["host"].default == "127.0.0.1"
    assert Settings.model_fields["hunyuan_video_model"].default == ""
    assert Settings.model_fields["local_image_model_hq"].default == ""
    assert Settings.model_fields["local_image_model_klein"].default == ""

    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in (
        "HF_TOKEN=",
        "HOST=127.0.0.1",
        "LOCAL_IMAGE_MODEL_HQ=",
        "LOCAL_IMAGE_MODEL_KLEIN=",
        "HUNYUAN_VIDEO_MODEL=",
    ):
        assert re.search(rf"^{re.escape(line)}$", env, flags=re.MULTILINE)


def test_readme_quick_start_and_frontend_legal_assets_are_shipped():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert '<h1 align="center">The Wizard\'s Brush</h1>' in readme
    assert "make install" in readme and "make models" in readme and "make start" in readme
    assert "docs/assets/brush-header.png" in readme
    assert (ROOT / "docs" / "assets" / "brush-header.png").is_file()
    for badge in (
        "github/actions/workflow/status/wizards-ecosystem/wizards-brush/ci.yml",
        "github/v/release/wizards-ecosystem/wizards-brush",
        "license-Apache--2.0",
        "contributions-welcome",
        "GPU-NVIDIA%20CUDA",
        "design-local--first",
    ):
        assert badge in readme

    bundle_readme = (ROOT / "packaging" / "BUNDLE_README.md").read_text(encoding="utf-8")
    assert bundle_readme.startswith("# The Wizard's Brush\n")
    assert "**Standalone Linux bundle**" in bundle_readme

    public = ROOT / "frontend" / "public"
    assert (public / "favicon.svg").is_file()
    assert (public / "manifest.webmanifest").is_file()
    notices = (public / "third-party-notices.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in notices
    assert "Instrument Sans" in notices and "Newsreader" in notices
    assert "Permission is hereby granted, free of charge" in notices

    package_lock = json.loads(
        (ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")
    )
    bundled_mit_packages = {
        "react": "React",
        "react-dom": "React DOM",
        "scheduler": "Scheduler",
        "react-router": "React Router",
        "react-router-dom": "React Router DOM",
        "zustand": "Zustand",
        "cookie": "cookie",
        "set-cookie-parser": "set-cookie-parser",
        "tailwindcss": "Tailwind CSS",
        "@tailwindcss/postcss": "@tailwindcss/postcss",
        "vite": "Vite",
        "rolldown": "Rolldown",
    }
    for package, notice_name in bundled_mit_packages.items():
        metadata = package_lock["packages"][f"node_modules/{package}"]
        assert metadata["license"] == "MIT"
        assert f"{notice_name} {metadata['version']}" in notices


def test_setup_has_a_download_free_host_preflight():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    setup = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts" / "bootstrap-tools.sh").read_text(encoding="utf-8")
    assert "host-check:" in makefile
    assert "scripts/check-host.sh" in setup
    assert "scripts/check-host.sh" in bootstrap


def test_bootstrap_and_native_installers_use_reviewed_immutable_inputs():
    bootstrap = (ROOT / "scripts" / "bootstrap-tools.sh").read_text(encoding="utf-8")
    versions = (ROOT / "scripts" / "tool-versions.env").read_text(encoding="utf-8")
    nunchaku = (ROOT / "scripts" / "install_nunchaku.sh").read_text(encoding="utf-8")
    remote = (ROOT / "remote_gpu.py").read_text(encoding="utf-8")
    assert "astral.sh/uv" not in bootstrap and "SHASUMS256.txt" not in bootstrap
    assert versions.count("UV_SHA256_") == 4 and versions.count("NODE_SHA256_") == 4
    assert "NUNCHAKU_WHEEL_SHA256" in nunchaku and "releases/latest" not in nunchaku
    assert "releases/latest" not in remote
    assert "cloudflared_sha256" in remote and '"--require-hashes"' in remote


def test_both_launchers_refuse_tokenless_non_loopback_binding():
    for name in ("scripts/start.sh", "scripts/dev.sh"):
        launcher = (ROOT / name).read_text(encoding="utf-8")
        assert "api_token_set=" in launcher
        assert "Refusing non-loopback HOST=" in launcher


def test_windows_lan_helper_is_port_and_subnet_scoped_and_reversible():
    helper = (ROOT / "scripts" / "windows-lan-access.ps1").read_text(encoding="utf-8")
    assert "DefaultInboundAction" not in helper
    for required in (
        "[switch]$Remove", "-Profile Private", "-RemoteAddress LocalSubnet",
        "-Protocol TCP", "-LocalPort $port", "New-NetFirewallHyperVRule",
        "-LocalPorts $port", "-RemoteAddresses LocalSubnet",
        "Remove-NetFirewallHyperVRule",
    ):
        assert required in helper


def test_patched_protobuf_override_has_an_optional_processor_smoke_test():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    smoke = (ROOT / "scripts" / "check_optional_imports.py").read_text(encoding="utf-8")
    assert '"protobuf>=5.29.6,<6"' in project
    assert '"setuptools==84.0.0"' in project
    assert 'override-dependencies = ["protobuf>=5.29.6,<6", "setuptools==84.0.0"]' in project
    assert "optional-check:" in makefile
    assert "OpenposeDetector" in smoke and 'hasattr(mp, "solutions")' in smoke


def test_dependency_check_allows_only_the_two_smoke_tested_overrides():
    checker = (ROOT / "scripts" / "check_dependencies.py").read_text(encoding="utf-8")
    assert '("mediapipe", "protobuf")' in checker
    assert '("torch", "setuptools")' in checker
    assert 'installed["setuptools"] == "84.0.0"' in checker
    assert "_override_is_tested" in checker
    assert "scripts/check_dependencies.py" in (ROOT / "Makefile").read_text(encoding="utf-8")


def test_standalone_release_is_allowlisted_and_excludes_development_state():
    release_files = (ROOT / "packaging" / "release-files.txt").read_text(encoding="utf-8")
    entries = {
        line.strip() for line in release_files.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    builder = (ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8")
    for required in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "backend", "uv.lock"):
        assert required in entries
    for forbidden in ("tests", ".env", "frontend/src", "node_modules", "output"):
        assert forbidden not in entries
    assert "FORBIDDEN_PARTS" in builder and "FORBIDDEN_SUFFIXES" in builder
    assert "dirty_build" in builder and "MANIFEST.sha256" in builder
    assert "if runtime_smoke:" in builder
    assert 'subprocess.run(["bash", "install.sh", "--check"]' in builder
    assert "extracted release runtime OK" in builder
