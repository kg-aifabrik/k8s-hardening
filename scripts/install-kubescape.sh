#!/usr/bin/env bash
# Install the kubescape CLI to /usr/local/bin.
#
# The official curl|bash installer silently no-ops on some Linux
# distros (Ubuntu 24.04 in particular) — we observed it creating a
# /root/.kubescape/bin/ symlink target that doesn't exist. This script
# fetches the release binary directly and verifies it runs.
#
# Detects OS + arch automatically. Works on Linux (amd64/arm64) and
# macOS (arm64).
#
# Usage:
#   sudo bash install-kubescape.sh           # latest release
#   sudo bash install-kubescape.sh v4.0.8    # pinned version
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "install-kubescape.sh must run as root (it writes to /usr/local/bin)" >&2
  exit 1
fi

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  VERSION=$(curl -sf https://api.github.com/repos/kubescape/kubescape/releases/latest \
    | grep -m1 '"tag_name"' | cut -d'"' -f4)
  echo "Resolved latest version: ${VERSION}"
fi

# OS + arch detection
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
case "$OS" in
  linux|darwin) ;;
  *) echo "Unsupported OS: $OS" >&2; exit 1 ;;
esac

ARCH=$(uname -m)
case "$ARCH" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "Unsupported arch: $ARCH" >&2; exit 1 ;;
esac

TARBALL="kubescape_${VERSION#v}_${OS}_${ARCH}.tar.gz"
URL="https://github.com/kubescape/kubescape/releases/download/${VERSION}/${TARBALL}"

echo "Downloading ${URL}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -sfL "$URL" -o "${TMP}/${TARBALL}"
tar -xzf "${TMP}/${TARBALL}" -C "$TMP" kubescape
install -m 0755 "${TMP}/kubescape" /usr/local/bin/kubescape

echo "Installed:"
/usr/local/bin/kubescape version 2>&1 | head -3
