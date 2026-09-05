#!/usr/bin/env bash
# Install the pinned uv and Node toolchain inside .runtime/tools. Release
# bundles pass --uv-only because their frontend is already built.
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/check-host.sh
source scripts/project-env.sh
source scripts/tool-versions.env

install_node=true
case "${1:-}" in
  "") ;;
  --uv-only) install_node=false ;;
  *) echo "Usage: $0 [--uv-only]" >&2; exit 2 ;;
esac

case "$(uname -s)" in
  Linux) node_os="linux"; uv_os="unknown-linux-gnu"; hash_os="LINUX" ;;
  Darwin) node_os="darwin"; uv_os="apple-darwin"; hash_os="DARWIN" ;;
  *) echo "!! Unsupported tool platform: $(uname -s)"; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64|amd64) node_arch="x64"; uv_arch="x86_64"; hash_arch="X86_64" ;;
  aarch64|arm64) node_arch="arm64"; uv_arch="aarch64"; hash_arch="AARCH64" ;;
  *) echo "!! Unsupported tool architecture: $(uname -m)"; exit 1 ;;
esac

uv_bin="$WB_TOOLS_DIR/uv/uv"
if [[ ! -x "$uv_bin" ]] || [[ "$($uv_bin --version 2>/dev/null || true)" != "uv $UV_VERSION "* ]]; then
  echo "==> Installing uv $UV_VERSION inside the project"
  stage="$(mktemp -d "$WB_TMP_DIR/uv-bootstrap.XXXXXX")"
  trap 'rm -rf "$stage"' EXIT
  uv_target="$uv_arch-$uv_os"
  archive="uv-$uv_target.tar.gz"
  uv_hash_var="UV_SHA256_${hash_os}_${hash_arch}"
  uv_hash="${!uv_hash_var}"
  curl -fL "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$archive" \
    -o "$stage/$archive"
  printf '%s  %s\n' "$uv_hash" "$stage/$archive" | sha256sum -c -
  tar -xzf "$stage/$archive" -C "$stage"
  mkdir -p "$WB_TOOLS_DIR/uv"
  install -m 755 "$stage/uv-$uv_target/uv" "$uv_bin"
  install -m 755 "$stage/uv-$uv_target/uvx" "$WB_TOOLS_DIR/uv/uvx"
  rm -rf "$stage"
  trap - EXIT
fi

if ! $install_node; then
  echo "==> Project toolchain"
  "$uv_bin" --version
  exit 0
fi

node_name="node-v$NODE_VERSION-$node_os-$node_arch"
node_dir="$WB_TOOLS_DIR/$node_name"
if [[ ! -x "$node_dir/bin/node" ]] || [[ "$("$node_dir/bin/node" --version)" != "v$NODE_VERSION" ]]; then
  echo "==> Installing Node $NODE_VERSION inside the project"
  stage="$(mktemp -d "$WB_TMP_DIR/node-bootstrap.XXXXXX")"
  trap 'rm -rf "$stage"' EXIT
  archive="$node_name.tar.xz"
  base="https://nodejs.org/dist/v$NODE_VERSION"
  node_hash_arch="$(printf '%s' "$node_arch" | tr '[:lower:]' '[:upper:]')"
  node_hash_var="NODE_SHA256_${hash_os}_${node_hash_arch}"
  node_hash="${!node_hash_var}"
  curl -fL "$base/$archive" -o "$stage/$archive"
  printf '%s  %s\n' "$node_hash" "$stage/$archive" | sha256sum -c -
  tar -xJf "$stage/$archive" -C "$stage"
  if [[ -e "$node_dir" || -L "$node_dir" ]]; then
    find "$node_dir" -depth -delete
  fi
  mv "$stage/$node_name" "$node_dir"
  rm -rf "$stage"
  trap - EXIT
fi
if [[ -e "$WB_TOOLS_DIR/node" && ! -L "$WB_TOOLS_DIR/node" ]]; then
  echo "!! $WB_TOOLS_DIR/node must be a symlink; refusing to overwrite it."
  exit 1
fi
ln -sfn "$node_name" "$WB_TOOLS_DIR/node"

echo "==> Project toolchain"
"$uv_bin" --version
"$WB_TOOLS_DIR/node/bin/node" --version
"$WB_TOOLS_DIR/node/bin/npm" --version
