#!/usr/bin/env bash
# One-command self-contained setup: pinned local tools, Python, dependencies,
# frontend, and models. Re-runnable; writes only inside this checkout.
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/check-host.sh
source scripts/project-env.sh
source scripts/tool-versions.env

download_models=true
case "${1:-}" in
  "") ;;
  --skip-models) download_models=false ;;
  *)
    echo "Usage: $0 [--skip-models]" >&2
    exit 2
    ;;
esac

release_bundle=false
if [[ -f RELEASE-MANIFEST.json ]]; then
  release_bundle=true
  [[ -f frontend/dist/index.html ]] || {
    echo "!! Release bundle is missing frontend/dist/index.html; download it again." >&2
    exit 1
  }
fi

[[ -f .env ]] || {
  install -m 600 .env.example .env
  echo "==> Created .env - review model and network settings."
}
chmod 600 .env

if $release_bundle; then
  echo "==> Using the bundled production frontend; Node is not required"
  bash scripts/bootstrap-tools.sh --uv-only
else
  echo "==> Project-owned uv and Node"
  bash scripts/bootstrap-tools.sh
fi

echo "==> Python $PYTHON_VERSION inside .runtime/python"
uv python install "$PYTHON_VERSION"
project_python="$(uv python find "$PYTHON_VERSION" --managed-python)"

if [[ ! -d .venv ]]; then
  uv venv --relocatable --python "$project_python" .venv
fi

echo "==> Locked backend dependencies (runtime + tools + optional processors)"
if $release_bundle; then
  uv sync --frozen --extra faces --extra detailer --extra rife --extra control \
    --no-dev --python "$project_python"
else
  uv sync --frozen --all-extras --group test --python "$project_python"
fi
"$project_python" scripts/migrate_local_state.py

echo "==> Frontend deps + build"
if $release_bundle; then
  echo "==> Using verified prebuilt frontend from the release archive"
else
  ( cd frontend && npm ci && npm run build )
fi

if $download_models; then
  echo "==> Downloading enabled local models (one-time, large)"
  .venv/bin/python scripts/download_models.py
else
  echo "==> Skipping model downloads; run 'make models' when ready"
fi

echo "==> Verifying the containment boundary"
.venv/bin/python scripts/doctor.py

echo
echo "Setup complete.  Next steps:"
if ! $download_models; then
  if $release_bundle; then
    echo "  .venv/bin/python scripts/download_models.py  # download enabled models"
  else
    echo "  make models            # download the enabled local model profile"
  fi
fi
if $release_bundle; then
  echo "  ./start.sh              # open http://127.0.0.1:8000"
  echo "  .venv/bin/python scripts/doctor.py  # verify project containment"
else
  echo "  bash scripts/dev.sh     # dev: backend :8000 + Vite :5173"
  echo "  bash scripts/start.sh   # one port using HOST/PORT from .env"
  echo "  make browser            # browser profile + cache inside this project"
  echo "  make browser-dev        # same contained profile against Vite :5173"
  echo "  make doctor             # verify that no writable path escapes"
fi
