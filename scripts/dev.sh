#!/usr/bin/env bash
# Dev mode: FastAPI (reload) on :8000 + Vite dev server on :5173 (proxies /api).
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/project-env.sh

source .venv/bin/activate
app_host="$(python -c 'from backend.app.config import settings; print(settings.host)')"
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

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "==> Backend  http://${app_host}:8000  (API + /docs)"
uvicorn backend.app.main:app --host "$app_host" --port 8000 --reload &

echo "==> Frontend http://${app_host}:5173"
cd frontend && npm run dev -- --host "$app_host" &

wait
