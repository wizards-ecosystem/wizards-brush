#!/usr/bin/env bash
# Repository-owned process environment. Source this before every setup, build,
# test, or runtime command so third-party libraries cannot spill mutable state
# into ~/.cache, ~/.config, ~/.local, or /tmp.

wb_requested_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
WB_PROJECT_ROOT="$wb_requested_root"
WB_RUNTIME_DIR="$WB_PROJECT_ROOT/.runtime"
WB_CACHE_DIR="$WB_RUNTIME_DIR/cache"
WB_CONFIG_DIR="$WB_RUNTIME_DIR/config"
WB_DATA_DIR="$WB_RUNTIME_DIR/data"
WB_STATE_DIR="$WB_RUNTIME_DIR/state"
WB_TMP_DIR="$WB_RUNTIME_DIR/tmp"
WB_TOOLS_DIR="$WB_RUNTIME_DIR/tools"
WB_MODELS_DIR="$WB_PROJECT_ROOT/models"

# Refuse managed roots that have been symlinked outside the checkout. Merely
# spelling an external target as `.runtime/cache` is not containment.
wb_assert_project_path() {
  local candidate resolved
  candidate="$1"
  resolved="$(realpath -m -- "$candidate")" || return 1
  case "$resolved" in
    "$WB_PROJECT_ROOT"|"$WB_PROJECT_ROOT"/*) return 0 ;;
    *)
      echo "!! Refusing project path that resolves outside the checkout: $candidate -> $resolved" >&2
      return 1
      ;;
  esac
}

for wb_managed_path in \
  "$WB_RUNTIME_DIR" "$WB_CACHE_DIR" "$WB_CONFIG_DIR" "$WB_DATA_DIR" \
  "$WB_STATE_DIR" "$WB_TMP_DIR" "$WB_TOOLS_DIR" \
  "$WB_PROJECT_ROOT/.venv" "$WB_MODELS_DIR" "$WB_MODELS_DIR/huggingface" \
  "$WB_MODELS_DIR/weights" "$WB_MODELS_DIR/loras" \
  "$WB_PROJECT_ROOT/output" "$WB_PROJECT_ROOT/frontend/node_modules" \
  "$WB_PROJECT_ROOT/frontend/dist"; do
  if ! wb_assert_project_path "$wb_managed_path"; then
    return 1 2>/dev/null || exit 1
  fi
done
if [[ ( -e "$WB_PROJECT_ROOT/.env" || -L "$WB_PROJECT_ROOT/.env" ) ]] \
  && ! wb_assert_project_path "$WB_PROJECT_ROOT/.env"; then
  return 1 2>/dev/null || exit 1
fi

export WB_PROJECT_ENV_LOADED=1
export WB_PROJECT_ROOT WB_RUNTIME_DIR WB_CACHE_DIR WB_CONFIG_DIR WB_DATA_DIR
export WB_STATE_DIR WB_TMP_DIR WB_TOOLS_DIR WB_MODELS_DIR

# A project-specific HOME is the final containment boundary for dependencies
# that do not honor XDG or their own cache variable.
export HOME="$WB_RUNTIME_DIR/home"
export XDG_CACHE_HOME="$WB_CACHE_DIR/xdg"
export XDG_CONFIG_HOME="$WB_CONFIG_DIR/xdg"
export XDG_DATA_HOME="$WB_DATA_DIR/xdg"
export XDG_STATE_HOME="$WB_STATE_DIR/xdg"
export TMPDIR="$WB_TMP_DIR"
export TMP="$WB_TMP_DIR"
export TEMP="$WB_TMP_DIR"

# Python and uv.
export UV_CACHE_DIR="$WB_CACHE_DIR/uv"
export UV_PYTHON_INSTALL_DIR="$WB_RUNTIME_DIR/python"
export UV_TOOL_DIR="$WB_RUNTIME_DIR/uv-tools"
export UV_TOOL_BIN_DIR="$WB_RUNTIME_DIR/bin"
export UV_PROJECT_ENVIRONMENT="$WB_PROJECT_ROOT/.venv"
export PIP_CACHE_DIR="$WB_CACHE_DIR/pip"
export PIP_CONFIG_FILE="$WB_CONFIG_DIR/pip/pip.conf"
export PYTHONUSERBASE="$WB_RUNTIME_DIR/python-user"
export PYTHONPYCACHEPREFIX="$WB_CACHE_DIR/python-bytecode"
export RUFF_CACHE_DIR="$WB_CACHE_DIR/ruff"

# Node and browser tooling.
export NPM_CONFIG_CACHE="$WB_CACHE_DIR/npm"
export NPM_CONFIG_PREFIX="$WB_RUNTIME_DIR/npm-prefix"
export NPM_CONFIG_USERCONFIG="$WB_CONFIG_DIR/npm/npmrc"
export COREPACK_HOME="$WB_CACHE_DIR/corepack"
export PLAYWRIGHT_BROWSERS_PATH="$WB_CACHE_DIR/playwright"
export ELECTRON_CACHE="$WB_CACHE_DIR/electron"
export NODE_COMPILE_CACHE="$WB_CACHE_DIR/node-compile"

# Models, ML runtimes, compilers, and model-adjacent libraries.
export HF_HOME="$WB_MODELS_DIR/huggingface"
export HF_HUB_CACHE="$HF_HOME/hub"
export HF_XET_CACHE="$HF_HOME/xet"
export HF_ASSETS_CACHE="$HF_HOME/assets"
export HF_MODULES_CACHE="$HF_HOME/modules"
export HUGGINGFACE_HUB_CACHE="$HF_HUB_CACHE"
export SENTENCE_TRANSFORMERS_HOME="$WB_MODELS_DIR/sentence-transformers"
export TORCH_HOME="$WB_MODELS_DIR/torch"
export TORCH_EXTENSIONS_DIR="$WB_CACHE_DIR/torch-extensions"
export TORCHINDUCTOR_CACHE_DIR="$WB_CACHE_DIR/torch-inductor"
export TRITON_CACHE_DIR="$WB_CACHE_DIR/triton"
export CUDA_CACHE_PATH="$WB_CACHE_DIR/cuda"
export NUMBA_CACHE_DIR="$WB_CACHE_DIR/numba"
export MPLCONFIGDIR="$WB_CONFIG_DIR/matplotlib"
export IMAGEIO_USERDIR="$WB_DATA_DIR/imageio"
export KERAS_HOME="$WB_MODELS_DIR/keras"
export ONNX_HOME="$WB_MODELS_DIR/onnx"

# Build tools and optional integrations sometimes imported by ML packages.
export CARGO_HOME="$WB_RUNTIME_DIR/cargo"
export RUSTUP_HOME="$WB_RUNTIME_DIR/rustup"
export CCACHE_DIR="$WB_CACHE_DIR/ccache"
export SCCACHE_DIR="$WB_CACHE_DIR/sccache"
export IPYTHONDIR="$WB_CONFIG_DIR/ipython"
export JUPYTER_CONFIG_DIR="$WB_CONFIG_DIR/jupyter"
export WANDB_DIR="$WB_STATE_DIR/wandb"
export WANDB_CACHE_DIR="$WB_CACHE_DIR/wandb"
export WANDB_CONFIG_DIR="$WB_CONFIG_DIR/wandb"

# Application-owned durable paths. Exporting them makes an inherited host value
# unable to redirect the app outside the checkout.
export OUTPUT_DIR="$WB_PROJECT_ROOT/output"
export LORA_DIR="$WB_MODELS_DIR/loras"
export WEIGHTS_DIR="$WB_MODELS_DIR/weights"
export RUNTIME_DIR="$WB_RUNTIME_DIR"

# Check the concrete destination of every writable environment path before the
# first mkdir/touch. This also catches a nested cache directory symlink.
for wb_env_name in \
  HOME XDG_CACHE_HOME XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME \
  TMPDIR UV_CACHE_DIR UV_PYTHON_INSTALL_DIR UV_TOOL_DIR UV_TOOL_BIN_DIR \
  UV_PROJECT_ENVIRONMENT PIP_CACHE_DIR PIP_CONFIG_FILE PYTHONUSERBASE \
  PYTHONPYCACHEPREFIX RUFF_CACHE_DIR NPM_CONFIG_CACHE NPM_CONFIG_PREFIX \
  NPM_CONFIG_USERCONFIG \
  COREPACK_HOME PLAYWRIGHT_BROWSERS_PATH ELECTRON_CACHE HF_HOME HF_HUB_CACHE \
  HF_XET_CACHE HF_ASSETS_CACHE HF_MODULES_CACHE HUGGINGFACE_HUB_CACHE \
  SENTENCE_TRANSFORMERS_HOME TORCH_HOME TORCH_EXTENSIONS_DIR \
  TORCHINDUCTOR_CACHE_DIR TRITON_CACHE_DIR CUDA_CACHE_PATH NUMBA_CACHE_DIR \
  MPLCONFIGDIR IMAGEIO_USERDIR KERAS_HOME ONNX_HOME CARGO_HOME RUSTUP_HOME \
  CCACHE_DIR SCCACHE_DIR \
  NODE_COMPILE_CACHE IPYTHONDIR JUPYTER_CONFIG_DIR WANDB_DIR WANDB_CACHE_DIR \
  WANDB_CONFIG_DIR \
  OUTPUT_DIR LORA_DIR WEIGHTS_DIR RUNTIME_DIR; do
  if ! wb_assert_project_path "${!wb_env_name}"; then
    return 1 2>/dev/null || exit 1
  fi
done

mkdir -p \
  "$HOME" "$WB_TMP_DIR" "$WB_TOOLS_DIR" "$WB_MODELS_DIR" \
  "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" \
  "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR" "$PIP_CACHE_DIR" \
  "$(dirname "$PIP_CONFIG_FILE")" "$(dirname "$NPM_CONFIG_USERCONFIG")" \
  "$NPM_CONFIG_CACHE" "$HF_HUB_CACHE" "$HF_XET_CACHE" "$HF_ASSETS_CACHE" \
  "$HF_MODULES_CACHE" "$TORCH_HOME" "$TORCH_EXTENSIONS_DIR" \
  "$TORCHINDUCTOR_CACHE_DIR" "$TRITON_CACHE_DIR" "$CUDA_CACHE_PATH" \
  "$NUMBA_CACHE_DIR" "$MPLCONFIGDIR" "$IMAGEIO_USERDIR" "$RUFF_CACHE_DIR" \
  "$NODE_COMPILE_CACHE" "$WB_CACHE_DIR/typescript" "$LORA_DIR" "$WEIGHTS_DIR"

touch "$PIP_CONFIG_FILE" "$NPM_CONFIG_USERCONFIG"

wb_prepend_path() {
  case ":$PATH:" in
    *":$1:"*) ;;
    *) PATH="$1:$PATH" ;;
  esac
}
wb_prepend_path "$WB_RUNTIME_DIR/bin"
wb_prepend_path "$WB_TOOLS_DIR/uv"
wb_prepend_path "$WB_TOOLS_DIR/node/bin"
export PATH
