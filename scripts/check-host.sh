#!/usr/bin/env bash
# Fast, download-free validation of the host tools used by bootstrap and setup.
set -euo pipefail

missing=()
for tool in curl grep realpath sha256sum tar; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    missing+=("$tool")
  fi
done

if ((${#missing[@]})); then
  printf 'Missing required host tools: %s\n' "${missing[*]}" >&2
  printf 'Install the prerequisites from docs/getting-started.md and retry.\n' >&2
  exit 1
fi

if ! tar --help 2>&1 | grep -Eq -- '(^|[[:space:],])-J|--xz'; then
  echo "The installed tar does not report xz archive support (-J/--xz)." >&2
  echo "Install tar with xz support and retry." >&2
  exit 1
fi

echo "Host prerequisites: ok"
