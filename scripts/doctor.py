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
    # On Windows npm is npm.cmd; resolving through PATH first finds it, and the
    # pinned project tools are first on PATH either way.
    program = shutil.which(args[0]) or args[0]
    return subprocess.check_output((program, *args[1:]), cwd=ROOT, text=True).strip()


def long_path_risk() -> str:
    """A warning when a deep model-cache path could pass Windows' 260-character
    limit and long paths are not enabled. Empty when there is no risk."""
    if sys.platform != "win32":
        return ""
    # models\huggingface\hub\models--org--name\snapshots\<40 hex>\component\file
    deepest = len(str(ROOT)) + 190
    if deepest < 260:
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            enabled = winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        enabled = False
    if enabled:
        return ""
    return (f"the checkout path is {len(str(ROOT))} characters, so model cache files can pass "
            "Windows' 260-character limit. Move the checkout to a shorter path (for example "
            "C:\\wb), or have an administrator enable long paths (LongPathsEnabled).")


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
    warnings: list[str] = []
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

    private_files = (ROOT / ".env", settings.runtime_overrides_path)
    if os.name == "posix":
        for private_file in private_files:
            if not private_file.exists():
                continue
            mode = stat.S_IMODE(private_file.stat().st_mode)
            checks[f"private mode {private_file.name}"] = private_file
            if mode & 0o077:
                failures.append(
                    f"{private_file.relative_to(ROOT)} permissions are {mode:04o}; expected 0600"
                )
    elif os.name == "nt":
        from backend.app.utils.private_files import is_private

        for private_file in private_files:
            if not private_file.exists():
                continue
            checks[f"private access {private_file.name}"] = private_file
            verdict = is_private(private_file)
            if verdict is False:
                failures.append(f"{private_file.relative_to(ROOT)} is readable by other accounts; "
                                "run scripts\\setup.ps1 again to restrict it")
            elif verdict is None:
                warnings.append(f"could not read the access list of {private_file.name}")
    if risk := long_path_risk():
        warnings.append(risk)

    print("The Wizard's Brush self-containment audit")
    for check_name, check_value in checks.items():
        marker = "ok" if inside(check_value) else "ESCAPES"
        print(f"  {marker:7} {check_name:24} {check_value}")
    for name, (expected, actual) in version_checks.items():
        marker = "ok" if actual == expected else "WRONG"
        print(f"  {marker:7} {name + ' version':24} {actual}")
    for warning in warnings:
        print(f"  warning {warning}")
    if failures:
        print("\nFailures:")
        for failure in failures:
            print(f"  - {failure}")
        raise SystemExit(1)
    print("\nAll active writable paths are inside the project.")


if __name__ == "__main__":
    main()
