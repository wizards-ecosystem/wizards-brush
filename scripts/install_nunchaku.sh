#!/usr/bin/env bash
# Explicit advanced install for a reviewed Nunchaku (SVDQuant) wheel.
# The app defaults to bitsandbytes because native wheels are tied to the exact
# Python/torch/CUDA combination. Supply both an immutable URL and its SHA-256.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/project-env.sh

PY=.venv/bin/python
[[ -x "$PY" ]] || { echo "!! no .venv — run scripts/setup.sh first"; exit 1; }

: "${NUNCHAKU_WHEEL_URL:?Set NUNCHAKU_WHEEL_URL to a reviewed immutable .whl URL}"
: "${NUNCHAKU_WHEEL_SHA256:?Set NUNCHAKU_WHEEL_SHA256 to its published SHA-256}"
case "$NUNCHAKU_WHEEL_URL" in
  https://*.whl) ;;
  *) echo "!! NUNCHAKU_WHEEL_URL must be an HTTPS .whl URL" >&2; exit 2 ;;
esac

wheel="$(mktemp "$WB_TMP_DIR/nunchaku.XXXXXX.whl")"
trap 'rm -f "$wheel"' EXIT
curl -fL "$NUNCHAKU_WHEEL_URL" -o "$wheel"
printf '%s  %s\n' "$NUNCHAKU_WHEEL_SHA256" "$wheel" | sha256sum -c -
uv pip install --python .venv "$wheel"
"$PY" -c "from nunchaku.utils import get_precision; print('nunchaku ok, precision:', get_precision())"
