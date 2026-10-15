#!/usr/bin/env bash
# Install the command-line tools the pipeline runs: uv, ripgrep (rg) and git.
# No Homebrew, no password: uv and rg go into ~/.local/bin (where Claude Code's own installer puts `claude`).
# Safe to run again: a tool that is already installed is left alone.
#
#   bash scripts/install-deps.sh
set -euo pipefail

RG_VERSION="15.2.0"
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
export PATH="$BIN:$PATH"

have() { command -v "$1" >/dev/null 2>&1; }

# git — every Mac has it once the Command Line Tools are installed. macOS shows its own "Install" dialog.
if git --version >/dev/null 2>&1; then
  echo "git: ok ($(git --version))"
else
  echo "git: missing — opening the macOS installer. Click Install, wait for it to finish, then run this again."
  xcode-select --install || true
  exit 1
fi

# uv — its official installer; it also adds ~/.local/bin to your shell's PATH.
if have uv; then
  echo "uv: ok ($(uv --version))"
else
  echo "uv: installing…"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$BIN" sh
  echo "uv: ok ($("$BIN/uv" --version))"
fi

# ripgrep — the official release binary from GitHub, checked against its published sha256.
# A real binary is needed: Claude Code's `rg` is a shell function the pipeline can't call.
if [ "$(type -t rg)" = "file" ]; then
  echo "rg: ok ($(rg --version | head -1))"
else
  case "$(uname -m)" in
    arm64|aarch64) target="aarch64-apple-darwin" ;;
    x86_64)        target="x86_64-apple-darwin" ;;
    *) echo "rg: unsupported CPU $(uname -m)"; exit 1 ;;
  esac
  name="ripgrep-${RG_VERSION}-${target}"
  url="https://github.com/BurntSushi/ripgrep/releases/download/${RG_VERSION}/${name}.tar.gz"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  echo "rg: installing ${RG_VERSION}…"
  curl -fsSL --connect-timeout 15 --max-time 300 --retry 3 "$url" -o "$tmp/rg.tar.gz"
  curl -fsSL --connect-timeout 15 --max-time 60 --retry 3 "$url.sha256" -o "$tmp/rg.sha256"
  want="$(awk '{print $1}' "$tmp/rg.sha256")"
  got="$(shasum -a 256 "$tmp/rg.tar.gz" | awk '{print $1}')"
  if [ "$want" != "$got" ]; then
    echo "rg: checksum mismatch — not installed"; exit 1
  fi
  tar -xzf "$tmp/rg.tar.gz" -C "$tmp"
  install -m 755 "$tmp/$name/rg" "$BIN/rg"
  echo "rg: ok ($("$BIN/rg" --version | head -1))"
fi

echo "done. Tools are in $BIN — open a new terminal if a command isn't found."
