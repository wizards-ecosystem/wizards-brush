#!/usr/bin/env bash
# Friendly installer for both source checkouts and standalone release bundles.
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-}" in
  "") exec bash scripts/setup.sh --skip-models ;;
  --with-models) exec bash scripts/setup.sh ;;
  --check)
    [[ -f RELEASE-MANIFEST.json && -f MANIFEST.sha256 ]] || {
      echo "!! --check is for an extracted standalone release bundle." >&2
      exit 1
    }
    bash scripts/check-host.sh
    sha256sum -c --quiet MANIFEST.sha256
    [[ -f frontend/dist/index.html ]] || {
      echo "!! The prebuilt frontend is missing." >&2
      exit 1
    }
    echo "Release bundle integrity and host checks passed."
    ;;
  *) echo "Usage: $0 [--with-models|--check]" >&2; exit 2 ;;
esac
