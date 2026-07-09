#!/usr/bin/env bash
# Obsidian Semantic MCP — one-liner installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/artificemachine/obsidian-semantic-mcp/main/install.sh | bash
#
# Or with init flags passed through:
#   bash <(curl -fsSL https://raw.githubusercontent.com/artificemachine/obsidian-semantic-mcp/main/install.sh) \
#       --mode 3 --vault /path/to/vault --persistent

set -euo pipefail

REPO_URL="https://github.com/artificemachine/obsidian-semantic-mcp.git"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/obsidian-semantic-mcp"
BIN_DIR="$HOME/.local/bin"

_c()  { printf '\033[%sm%s\033[0m' "$1" "$2"; }
ok()  { printf '  %s  %s\n' "$(_c 92 '✓')" "$1"; }
info(){ printf '  %s  %s\n' "$(_c 94 '→')" "$1"; }
warn(){ printf '  %s  %s\n' "$(_c 93 '⚠')" "$1"; }
fail(){ printf '  %s  %s\n' "$(_c 91 '✗')" "$1"; }
hr()  { printf '%s\n' "────────────────────────────────────────────────────────────"; }

hr
printf '\n  %s\n\n' "$(_c 1 'Obsidian Semantic MCP — Installer')"

# ── Prerequisites ─────────────────────────────────────────────────────────────

if ! command -v git &>/dev/null; then
    fail "git not found — install git and re-run"
    exit 1
fi
ok "git found"

# ── Clone or update ───────────────────────────────────────────────────────────

if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing install at ${INSTALL_DIR}..."
    git -C "$INSTALL_DIR" pull --ff-only
    ok "Up to date"
elif [ -e "$INSTALL_DIR" ]; then
    backup="${INSTALL_DIR}.backup-$(date +%Y%m%d%H%M%S)"
    warn "Existing non-git install directory found"
    info "Moving it to ${backup}"
    mv "$INSTALL_DIR" "$backup"
    info "Cloning to ${INSTALL_DIR}..."
    git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
    ok "Cloned"
else
    info "Cloning to ${INSTALL_DIR}..."
    git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
    ok "Cloned"
fi

# ── Symlink osm to PATH ───────────────────────────────────────────────────────

mkdir -p "$BIN_DIR"
ln -sf "$INSTALL_DIR/scripts/osm" "$BIN_DIR/osm"
chmod +x "$INSTALL_DIR/scripts/osm"
ok "Linked: $BIN_DIR/osm"

# PATH hint (non-fatal — wizard still runs via full path)
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        warn "$BIN_DIR is not in your PATH."
        info "Add this line to your shell profile (.zshrc / .bashrc):"
        printf '\n    export PATH="%s:$PATH"\n\n' "$BIN_DIR"
        ;;
esac

hr
printf '\n'

# ── Run setup wizard ──────────────────────────────────────────────────────────

if [ -t 0 ]; then
    exec "$INSTALL_DIR/scripts/osm" init "$@"
elif [ -r /dev/tty ]; then
    # Reattach stdin to the user's terminal when the installer was piped in.
    exec "$INSTALL_DIR/scripts/osm" init "$@" < /dev/tty
else
    warn "No interactive terminal available for the setup wizard"
    info "Run setup manually:"
    printf '\n    %s init\n\n' "$BIN_DIR/osm"
    exit 0
fi
