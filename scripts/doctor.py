"""Fail when any active The Wizard's Brush path resolves outside the checkout."""
from __future__ import annotations

import os
import platform
import shutil
import site
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def inside(value: str | Path) -> bool:
    try:
        Path(value).expanduser().resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


def command(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def expected_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for line in (ROOT / "scripts" / "tool-versions.env").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            versions[name] = value
    return versions


def main() -> None:
    release_bundle = (ROOT / "RELEASE-MANIFEST.json").is_file()
    failures: list[str] = []
    checks: dict[str, str | Path] = {}
    version_checks: dict[str, tuple[str, str]] = {}
    env_names = (
        "HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_TOOL_DIR",
        "UV_TOOL_BIN_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "PIP_CACHE_DIR",
        "PIP_CONFIG_FILE",
        "PYTHONUSERBASE",
        "PYTHONPYCACHEPREFIX",
        "RUFF_CACHE_DIR",
        "NPM_CONFIG_CACHE",
        "NPM_CONFIG_PREFIX",
        "NPM_CONFIG_USERCONFIG",
        "COREPACK_HOME",
        "PLAYWRIGHT_BROWSERS_PATH",
        "ELECTRON_CACHE",
        "NODE_COMPILE_CACHE",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HF_XET_CACHE",
        "HF_ASSETS_CACHE",
        "HF_MODULES_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "SENTENCE_TRANSFORMERS_HOME",
        "TORCH_HOME",
        "TORCH_EXTENSIONS_DIR",
        "TORCHINDUCTOR_CACHE_DIR",
        "TRITON_CACHE_DIR",
        "CUDA_CACHE_PATH",
        "NUMBA_CACHE_DIR",
        "MPLCONFIGDIR",
        "IMAGEIO_USERDIR",
        "KERAS_HOME",
        "ONNX_HOME",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "CCACHE_DIR",
        "SCCACHE_DIR",
        "IPYTHONDIR",
        "JUPYTER_CONFIG_DIR",
        "WANDB_DIR",
        "WANDB_CACHE_DIR",
        "WANDB_CONFIG_DIR",
        "OUTPUT_DIR",
        "LORA_DIR",
        "WEIGHTS_DIR",
        "RUNTIME_DIR",
    )
    for name in env_names:
        value = os.environ.get(name, "")
        checks[name] = value
        if not value or not inside(value):
            failures.append(f"{name} escapes or is unset: {value or '(unset)'}")

    checks["temporary files"] = tempfile.gettempdir()
    checks["active Python"] = Path(sys.executable).resolve()
    checks["venv base Python"] = Path(sys.base_prefix).resolve()
    for index, path in enumerate(site.getsitepackages(), start=1):
        checks[f"site-packages {index}"] = Path(path).resolve()
    checks["user site-packages"] = Path(site.getusersitepackages()).resolve()
    for name in (
        "temporary files",
        "active Python",
        "venv base Python",
        *(name for name in checks if name.startswith("site-packages")),
        "user site-packages",
    ):
        if not inside(checks[name]):
            failures.append(f"{name} escapes: {checks[name]}")

    try:
        pinned = expected_versions()
        checks["uv cache"] = command("uv", "cache", "dir")
        checks["uv binary"] = shutil.which("uv") or ""
        version_checks = {
            "Python": (pinned["PYTHON_VERSION"], platform.python_version()),
            "uv": (pinned["UV_VERSION"], command("uv", "--version").split()[1]),
            ".python-version": (
                pinned["PYTHON_VERSION"], (ROOT / ".python-version").read_text().strip()
            ),
        }
        if not release_bundle:
            checks["npm cache"] = command("npm", "config", "get", "cache")
            checks["node binary"] = shutil.which("node") or ""
            version_checks["Node"] = (
                pinned["NODE_VERSION"], command("node", "--version").removeprefix("v")
            )
            version_checks[".nvmrc"] = (
                pinned["NODE_VERSION"], (ROOT / ".nvmrc").read_text().strip()
            )
    except (OSError, subprocess.CalledProcessError, KeyError, IndexError) as exc:
        failures.append(f"toolchain check failed: {exc}")
    for name in ("uv cache", "npm cache", "uv binary", "node binary"):
        if name in checks and not inside(checks[name]):
            failures.append(f"{name} escapes: {checks[name]}")
    for name, (expected, actual) in version_checks.items():
        if actual != expected:
            failures.append(f"{name} version is {actual}; expected {expected}")

    from backend.app.config import settings

    app_paths = {
        "app output": settings.output_path,
        "app runtime": settings.runtime_path,
        "app temp": settings.temp_path,
        "Hugging Face models": settings.hf_home_path,
        "Hugging Face hub": settings.hf_hub_path,
        "tool weights": settings.weights_path,
        "LoRAs": settings.loras_dir,
    }
    checks.update(app_paths)
    for app_name, app_path in app_paths.items():
        if not inside(app_path):
            failures.append(f"{app_name} escapes: {app_path}")

    for legacy in (ROOT / "hf_cache", ROOT / "weights", ROOT / "loras"):
        if legacy.exists():
            failures.append(f"legacy directory still exists: {legacy.relative_to(ROOT)}")

    if os.name == "posix":
        private_files = (
            ROOT / ".env",
            settings.runtime_overrides_path,
            ROOT / "remote_gpu_filled.py",
        )
        for private_file in private_files:
            if not private_file.exists():
                continue
            mode = stat.S_IMODE(private_file.stat().st_mode)
            checks[f"private mode {private_file.name}"] = private_file
            if mode & 0o077:
                failures.append(
                    f"{private_file.relative_to(ROOT)} permissions are {mode:04o}; expected 0600"
                )

    print("The Wizard's Brush self-containment audit")
    for check_name, check_value in checks.items():
        marker = "ok" if inside(check_value) else "ESCAPES"
        print(f"  {marker:7} {check_name:24} {check_value}")
    for name, (expected, actual) in version_checks.items():
        marker = "ok" if actual == expected else "WRONG"
        print(f"  {marker:7} {name + ' version':24} {actual}")
    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"  - {failure}")
        raise SystemExit(1)
    print("\nAll active writable paths are inside the project.")


if __name__ == "__main__":
    main()
