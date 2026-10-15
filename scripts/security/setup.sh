#!/usr/bin/env bash
# One-time local setup for the security gate. Safe to re-run.
#
#   bash scripts/security/setup.sh
#
# Installs uv, pipx, semgrep, trufflehog and trivy at the versions CI uses, turns on the pre-push hook, and
# says what is still missing. Downloads are checked against the release's own checksum file before anything is
# installed. Versions below must match scripts/security/scan.py and .github/actions/security-setup/action.yml.
set -euo pipefail

SEMGREP_VERSION="1.164.0"
TRUFFLEHOG_VERSION="3.88.28"
TRIVY_VERSION="0.70.0"
BIN_DIR="$HOME/.local/bin"

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

missing=0
say()  { printf '%s\n' "$*"; }
ok()   { printf '  ok    %s\n' "$*"; }
todo() { printf '  TODO  %s\n' "$*"; missing=1; }

# Maps this Mac to the arch names the release files use: arm64 (Apple Silicon) or amd64 (Intel).
mac_arch() {
  case "$(uname -m)" in
    arm64|aarch64) echo "arm64" ;;
    x86_64)        echo "amd64" ;;
    *)             echo "" ;;
  esac
}

# fetch <url> <destination>: follows GitHub's redirect to the file host, retries on connection resets too
# (curl's default retry skips them), and says what to check when it still fails.
fetch() {
  if ! curl -sSL --fail --retry 5 --retry-all-errors --retry-delay 2 -o "$2" "$1"; then
    todo "download failed: $1. Check the network (a proxy or VPN can block GitHub downloads), then re-run the script."
    return 1
  fi
}

# install_release <tool> <version> <archive name> <checksum file name> <release URL base>
# Downloads the archive and the checksum file, verifies the archive, extracts the binary into $BIN_DIR.
install_release() {
  local tool="$1" version="$2" archive="$3" sums="$4" base="$5"
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  say "  downloading $archive"
  fetch "$base/$archive" "$tmp/$archive" || return 1
  fetch "$base/$sums" "$tmp/$sums" || return 1
  # Check only the archive we downloaded; the checksum file lists every platform.
  if ! (cd "$tmp" && grep " $archive\$" "$sums" | shasum -a 256 -c - >/dev/null); then
    todo "$tool download failed its checksum; nothing was installed. Re-run the script."
    return 1
  fi
  tar -xzf "$tmp/$archive" -C "$tmp"
  local bin
  bin="$(find "$tmp" -type f -name "$tool" | head -1)"
  if [ -z "$bin" ]; then
    todo "$tool binary not found in $archive"
    return 1
  fi
  mkdir -p "$BIN_DIR"
  install -m 0755 "$bin" "$BIN_DIR/$tool"
  ok "$tool $version installed to $BIN_DIR/$tool"
}

say "1. Checking the OS"
if [ "$(uname -s)" = Darwin ]; then
  ok "macOS $(mac_arch)"
else
  say "  This script installs for macOS only. On Linux, install these tools at the versions above by hand:"
  say "  uv, pipx, semgrep $SEMGREP_VERSION, trufflehog $TRUFFLEHOG_VERSION, trivy $TRIVY_VERSION."
  missing=1
fi

say "2. uv (runs pip-audit without installing it)"
if command -v uv >/dev/null; then
  ok "uv $(uv --version | awk '{print $2}')"
elif command -v brew >/dev/null; then
  brew install uv && ok "uv installed"
else
  todo "install uv: brew install uv  (or https://docs.astral.sh/uv/)"
fi

say "3. semgrep $SEMGREP_VERSION"
if command -v semgrep >/dev/null && semgrep --version | grep -qx "$SEMGREP_VERSION"; then
  ok "semgrep $SEMGREP_VERSION"
else
  if ! command -v pipx >/dev/null && command -v brew >/dev/null; then
    brew install pipx
  fi
  if command -v pipx >/dev/null; then
    pipx install --force "semgrep==$SEMGREP_VERSION" >/dev/null && ok "semgrep $SEMGREP_VERSION installed with pipx"
  else
    todo "install pipx (brew install pipx), then: pipx install semgrep==$SEMGREP_VERSION"
  fi
fi

arch="$(mac_arch)"

say "4. trufflehog $TRUFFLEHOG_VERSION"
if command -v trufflehog >/dev/null && trufflehog --version 2>&1 | grep -q "$TRUFFLEHOG_VERSION"; then  # trufflehog prints its version to stderr
  ok "trufflehog $TRUFFLEHOG_VERSION"
elif command -v trufflehog >/dev/null; then
  # Never overwrite an install we did not make: a second copy would shadow it on PATH.
  todo "found trufflehog $(trufflehog --version 2>&1 | awk '{print $2}') at $(command -v trufflehog); this project needs $TRUFFLEHOG_VERSION. Install $TRUFFLEHOG_VERSION so it is the one on PATH, then re-run this script."
elif [ "$(uname -s)" = Darwin ] && [ -n "$arch" ]; then
  install_release trufflehog "$TRUFFLEHOG_VERSION" \
    "trufflehog_${TRUFFLEHOG_VERSION}_darwin_${arch}.tar.gz" \
    "trufflehog_${TRUFFLEHOG_VERSION}_checksums.txt" \
    "https://github.com/trufflesecurity/trufflehog/releases/download/v${TRUFFLEHOG_VERSION}" || true
else
  todo "install trufflehog $TRUFFLEHOG_VERSION from https://github.com/trufflesecurity/trufflehog/releases/tag/v$TRUFFLEHOG_VERSION"
fi

say "5. trivy $TRIVY_VERSION"
if command -v trivy >/dev/null && trivy --version 2>/dev/null | grep -q "$TRIVY_VERSION"; then
  ok "trivy $TRIVY_VERSION"
elif command -v trivy >/dev/null; then
  # Never overwrite an install we did not make: a second copy would shadow it on PATH.
  todo "found trivy $(trivy --version 2>&1 | awk '{print $2}') at $(command -v trivy); this project needs $TRIVY_VERSION. Install $TRIVY_VERSION so it is the one on PATH, then re-run this script."
elif [ "$(uname -s)" = Darwin ] && [ -n "$arch" ]; then
  # trivy names its macOS builds ARM64 and 64bit, not arm64 and amd64.
  trivy_arch="$([ "$arch" = arm64 ] && echo ARM64 || echo 64bit)"
  install_release trivy "$TRIVY_VERSION" \
    "trivy_${TRIVY_VERSION}_macOS-${trivy_arch}.tar.gz" \
    "trivy_${TRIVY_VERSION}_checksums.txt" \
    "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}" || true
else
  todo "install trivy $TRIVY_VERSION from https://github.com/aquasecurity/trivy/releases/tag/v$TRIVY_VERSION"
fi

say "6. PATH"
case ":$PATH:" in
  *":$BIN_DIR:"*) ok "$BIN_DIR is on PATH" ;;
  *) todo "add $BIN_DIR to PATH, e.g. echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc, then open a new terminal" ;;
esac

say "7. pre-push hook"
make install-hooks >/dev/null
if [ "$(git config core.hooksPath)" = ".githooks" ]; then
  ok "core.hooksPath = .githooks"
else
  todo "hook not active: run make install-hooks"
fi

say
if [ "$missing" -eq 0 ]; then
  say "Setup complete. Check it with: make security"
else
  say "Setup incomplete. Fix the TODO lines above, then re-run this script."
  exit 1
fi
