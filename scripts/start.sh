#!/usr/bin/env bash
# Build when running from source, then serve everything from one port. Release
# bundles contain a verified production build and do not require Node.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/project-env.sh

if [[ -f RELEASE-MANIFEST.json ]]; then
  install_hint="./install.sh"
else
  install_hint="make setup"
fi
[[ -d .venv ]] || { echo "!! No .venv - run '$install_hint' first."; exit 1; }
source .venv/bin/activate

if [[ -f RELEASE-MANIFEST.json ]]; then
  [[ -f frontend/dist/index.html ]] || {
    echo "!! Release bundle is missing its prebuilt frontend; download it again." >&2
    exit 1
  }
  echo "==> Using prebuilt release frontend"
else
  if [[ ! -d frontend/node_modules ]]; then
    echo "==> Installing frontend deps (first run)"
    ( cd frontend && npm ci )
  fi

  echo "==> Building frontend"
  ( cd frontend && npm run build )
fi

app_host="$(python -c 'from backend.app.config import settings; print(settings.host)')"
app_port="$(python -c 'from backend.app.config import settings; print(settings.port)')"
api_token_set="$(python -c 'from backend.app.config import settings; print("yes" if settings.api_token else "no")')"
case "$app_host" in
  localhost|127.0.0.1|::1) ;;
  *)
    if [[ "$api_token_set" != "yes" ]]; then
      echo "!! Refusing non-loopback HOST=$app_host without API_TOKEN." >&2
      echo "   Set a strong API_TOKEN and use HTTPS before enabling LAN access." >&2
      exit 1
    fi
    ;;
esac
echo "==> Serving on http://${app_host}:${app_port}"
if [[ "$app_host" != "127.0.0.1" && "$app_host" != "localhost" && "$app_host" != "::1" ]]; then
  echo "==> Authenticated network access enabled; terminate TLS in a trusted reverse proxy"
fi
exec uvicorn backend.app.main:app --host "$app_host" --port "$app_port"
