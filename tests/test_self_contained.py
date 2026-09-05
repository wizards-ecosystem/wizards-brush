"""The local runtime contract: no mutable application path escapes the repo."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from backend.app.config import ROOT, Settings, settings


def _inside(path: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


def test_application_storage_is_inside_checkout():
    for path in (
        settings.output_path,
        settings.runtime_path,
        settings.temp_path,
        settings.hf_home_path,
        settings.hf_hub_path,
        settings.weights_path,
        settings.loras_dir,
    ):
        assert _inside(path), path


def test_process_caches_and_temp_are_inside_checkout():
    names = (
        "HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
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
        "TMPDIR",
        "TMP",
        "TEMP",
    )
    for name in names:
        assert _inside(os.environ[name]), f"{name}={os.environ[name]}"
    assert _inside(tempfile.gettempdir())


def test_torch_allocator_uses_expandable_segments_without_overwriting_an_override(monkeypatch):
    monkeypatch.delenv("PYTORCH_ALLOC_CONF", raising=False)
    settings.apply_process_environment()
    assert os.environ["PYTORCH_ALLOC_CONF"] == "expandable_segments:True"

    monkeypatch.setenv("PYTORCH_ALLOC_CONF", "garbage_collection_threshold:0.8")
    settings.apply_process_environment()
    assert os.environ["PYTORCH_ALLOC_CONF"] == "garbage_collection_threshold:0.8"


def test_external_application_path_is_rejected():
    candidate = Settings(OUTPUT_DIR=str(ROOT.parent / "outside-wizards-brush"))
    with pytest.raises(ValueError, match="must stay inside"):
        _ = candidate.output_path


def test_nested_storage_symlink_cannot_escape(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "images").symlink_to("/tmp", target_is_directory=True)
    candidate = Settings(OUTPUT_DIR=str(output))
    with pytest.raises(ValueError, match="must stay inside"):
        _ = candidate.images_dir


def test_sourcing_project_environment_repairs_path_overrides():
    script = (
        "source scripts/project-env.sh; "
        "export HF_HOME=/tmp/wizards-brush-escape; "
        "source scripts/project-env.sh; "
        "printf '%s' \"$HF_HOME\""
    )
    result = subprocess.check_output(["bash", "-c", script], cwd=ROOT, text=True)
    assert Path(result).resolve() == settings.hf_home_path


def test_legacy_model_directories_are_retired():
    assert not (ROOT / "hf_cache").exists()
    assert not (ROOT / "weights").exists()
    assert not (ROOT / "loras").exists()
