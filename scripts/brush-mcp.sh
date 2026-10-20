#!/usr/bin/env bash
# Stdio MCP for clients that cannot open Streamable HTTP. Relays to the app
# already running in this checkout; it does not start a second server.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/project-env.sh
exec .venv/bin/python -m backend.app.agent.stdio_bridge "$@"
