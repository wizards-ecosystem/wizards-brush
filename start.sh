#!/usr/bin/env bash
# Start the built application from either a source checkout or release bundle.
set -euo pipefail
cd "$(dirname "$0")"
exec bash scripts/start.sh
