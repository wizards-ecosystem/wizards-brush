"""Filesystem layout, feature flags, and which kind of host we are on.

Imported before anything heavy: it sets the cache environment variables that
torch, diffusers and huggingface_hub read at *their* import time, so a late
import here would silently spill caches into the container's root filesystem.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def flag(name: str) -> bool:
    """An environment variable read as a boolean."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# Everything the worker downloads or caches lives under one root so a container
# can point it at storage that outlives the container. Left at the default it
# lands on the ephemeral container disk, which is correct only for a throwaway
# run: on Runpod that means re-downloading tens of GB on every start.
#   pod:        REMOTE_GPU_ROOT=/workspace/wizards-brush
#   serverless: REMOTE_GPU_ROOT=/runpod-volume/wizards-brush
_ROOT_OVERRIDE = os.environ.get("REMOTE_GPU_ROOT", "").strip()
ROOT = (Path(_ROOT_OVERRIDE).expanduser().resolve() if _ROOT_OVERRIDE
        else Path.cwd() / ".wizards-brush-remote-gpu")

CACHE = ROOT / "cache"
CONFIG_DIR = ROOT / "config"
DATA = ROOT / "data"
STATE = ROOT / "state"
MODELS = ROOT / "models"
TMP = ROOT / "tmp"
TOOLS = ROOT / "tools"
HF_CACHE = MODELS / "huggingface" / "hub"
HOME = ROOT / "home"

for _directory in (CACHE, CONFIG_DIR, DATA, STATE, MODELS, TMP, TOOLS, HOME):
    _directory.mkdir(parents=True, exist_ok=True)

# The no-home-directory-spill contract: a rented machine is someone else's, and
# a serverless worker's filesystem is discarded, so nothing may assume $HOME.
os.environ.update({
    "HOME": str(HOME),
    "XDG_CACHE_HOME": str(CACHE / "xdg"),
    "XDG_CONFIG_HOME": str(CONFIG_DIR / "xdg"),
    "XDG_DATA_HOME": str(DATA / "xdg"),
    "XDG_STATE_HOME": str(STATE / "xdg"),
    "TMPDIR": str(TMP),
    "TMP": str(TMP),
    "TEMP": str(TMP),
    "PYTHONPYCACHEPREFIX": str(CACHE / "python-bytecode"),
    "HF_HOME": str(MODELS / "huggingface"),
    "HF_HUB_CACHE": str(HF_CACHE),
    "HF_XET_CACHE": str(MODELS / "huggingface" / "xet"),
    "HF_ASSETS_CACHE": str(MODELS / "huggingface" / "assets"),
    "HUGGINGFACE_HUB_CACHE": str(HF_CACHE),
    "SENTENCE_TRANSFORMERS_HOME": str(MODELS / "sentence-transformers"),
    "TORCH_HOME": str(MODELS / "torch"),
    "TORCH_EXTENSIONS_DIR": str(CACHE / "torch-extensions"),
    "TORCHINDUCTOR_CACHE_DIR": str(CACHE / "torch-inductor"),
    "TRITON_CACHE_DIR": str(CACHE / "triton"),
    "CUDA_CACHE_PATH": str(CACHE / "cuda"),
    "NUMBA_CACHE_DIR": str(CACHE / "numba"),
    "MPLCONFIGDIR": str(CONFIG_DIR / "matplotlib"),
    "IMAGEIO_USERDIR": str(DATA / "imageio"),
    "KERAS_HOME": str(MODELS / "keras"),
    "ONNX_HOME": str(MODELS / "onnx"),
})


def free_disk_gb(path: str | Path = ROOT) -> float:
    import shutil

    return shutil.disk_usage(path).free / 1e9


# ---- host detection --------------------------------------------------------
# Provider knowledge lives here and nowhere else. A new host is a function and
# a row, not another `or` in a boolean expression somewhere downstream.

# Runpod writes the pod's identity into this file and sources it from the
# interactive shell profile, NOT into every process environment. Measured on a
# live pod 2026-09-20: a non-interactive `ssh pod 'env'` has no RUNPOD_POD_ID,
# while this file has it — and ssh/nohup is exactly how an operator starts the
# worker, so reading the environment alone gets the answer wrong every time.
_RUNPOD_ENV_FILE = Path("/etc/rp_environment")


def runpod_pod_id() -> str:
    """This Runpod pod's id, from the environment or Runpod's own env file."""
    pod_id = os.environ.get("RUNPOD_POD_ID", "").strip()
    if pod_id:
        return pod_id
    try:
        text = _RUNPOD_ENV_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""  # not a Runpod host, or the file is unreadable
    found = re.search(r"""^\s*(?:export\s+)?RUNPOD_POD_ID=["']?([^"'\s]+)""",
                      text, re.MULTILINE)
    return found.group(1) if found else ""


def public_url(port: int) -> str:
    """The address this host already publishes for `port`, if it publishes one.

    Empty for a plain VM, where the operator supplies the route themselves.
    """
    pod_id = runpod_pod_id()
    if pod_id:
        return f"https://{pod_id}-{port}.proxy.runpod.net"
    return ""


def is_managed_host() -> bool:
    """True when the platform already terminates TLS in front of this process."""
    return bool(runpod_pod_id() or os.environ.get("RUNPOD_ENDPOINT_ID"))
