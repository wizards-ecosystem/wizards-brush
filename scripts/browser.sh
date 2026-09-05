#!/usr/bin/env bash
# Launch the app in a repository-owned browser profile so cookies, localStorage,
# IndexedDB, service workers, and HTTP caches also stay inside the project.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/project-env.sh

browser="${BROWSER_BIN:-}"
if [[ -z "$browser" ]]; then
  for candidate in google-chrome-stable google-chrome chromium chromium-browser microsoft-edge; do
    if command -v "$candidate" >/dev/null 2>&1; then
      browser="$(command -v "$candidate")"
      break
    fi
  done
fi
if [[ -z "$browser" ]]; then
  echo "!! No Chromium-based browser found. Set BROWSER_BIN to its executable."
  exit 1
fi

profile="$WB_RUNTIME_DIR/browser"
cache="$WB_CACHE_DIR/browser"
wb_assert_project_path "$profile"
wb_assert_project_path "$cache"
mkdir -p "$profile" "$cache"
url="${WB_URL:-http://localhost:${PORT:-8000}}"
exec "$browser" \
  --user-data-dir="$profile" \
  --disk-cache-dir="$cache" \
  --no-first-run \
  --no-default-browser-check \
  "$url"
