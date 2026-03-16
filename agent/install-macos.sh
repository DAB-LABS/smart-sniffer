#!/bin/bash
# SMART Sniffer Agent — macOS installer
# Usage: sudo bash install-macos.sh
set -e

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BOLD}  --> $*${NC}"; }
success() { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ⚠ $*${NC}"; }
fail()    { echo -e "${RED}  ✗ $*${NC}"; exit 1; }

BINARY_NAME="smartha-agent"
INSTALL_BIN="/usr/local/bin/$BINARY_NAME"
INSTALL_CFG="/etc/smartha-agent"
PLIST_NAME="com.dablabs.smartha-agent.plist"
PLIST_SRC="$(dirname "$0")/launchd/$PLIST_NAME"
PLIST_DEST="/Library/LaunchDaemons/$PLIST_NAME"

# ---------------------------------------------------------------------------
# Must run as root
# ---------------------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  fail "Please run as root: sudo bash $0"
fi

echo ""
echo -e "${BOLD}╔══════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   SMART Sniffer Agent — Installer    ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════╝${NC}"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Check for smartmontools
# ---------------------------------------------------------------------------
info "Checking for smartmontools..."
if ! command -v smartctl &>/dev/null; then
  warn "smartctl not found."
  echo ""
  echo "  Install it now with Homebrew? (recommended)"
  read -rp "  [Y/n]: " INSTALL_SMART
  INSTALL_SMART="${INSTALL_SMART:-Y}"
  if [[ "$INSTALL_SMART" =~ ^[Yy]$ ]]; then
    if ! command -v brew &>/dev/null; then
      fail "Homebrew not found. Install it first: https://brew.sh"
    fi
    info "Installing smartmontools..."
    brew install smartmontools
  else
    fail "smartmontools is required. Install it with: brew install smartmontools"
  fi
fi
success "smartctl found: $(smartctl --version | head -1)"

# ---------------------------------------------------------------------------
# Step 2: Locate or build the binary
# ---------------------------------------------------------------------------
info "Locating binary..."

# Check if we're running from the repo (build/ exists or we can build it).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
  PREBUILT="$SCRIPT_DIR/build/${BINARY_NAME}-darwin-arm64"
else
  PREBUILT="$SCRIPT_DIR/build/${BINARY_NAME}-darwin-amd64"
fi

if [ -f "$PREBUILT" ]; then
  BINARY_SRC="$PREBUILT"
  success "Found pre-built binary: $BINARY_SRC"
elif command -v go &>/dev/null; then
  info "No pre-built binary found — building from source..."
  cd "$SCRIPT_DIR"
  if [ "$ARCH" = "arm64" ]; then
    make darwin-arm64
    BINARY_SRC="$SCRIPT_DIR/build/${BINARY_NAME}-darwin-arm64"
  else
    make darwin-amd64
    BINARY_SRC="$SCRIPT_DIR/build/${BINARY_NAME}-darwin-amd64"
  fi
  success "Build complete."
else
  fail "No binary found in build/ and Go is not installed.\nEither run 'make darwin-arm64' first, or install Go: https://go.dev/dl"
fi

# ---------------------------------------------------------------------------
# Step 3: Configuration
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}  Configuration${NC}"
echo "  (Press Enter to accept defaults)"
echo ""

read -rp "  Port [9099]: " PORT
PORT="${PORT:-9099}"

read -rp "  Bearer token for API auth (leave blank to disable): " TOKEN

read -rp "  Scan interval [60s]: " SCAN_INTERVAL
SCAN_INTERVAL="${SCAN_INTERVAL:-60s}"

# ---------------------------------------------------------------------------
# Step 4: Install binary
# ---------------------------------------------------------------------------
echo ""
info "Installing binary to $INSTALL_BIN..."
cp "$BINARY_SRC" "$INSTALL_BIN"
chmod +x "$INSTALL_BIN"
success "Binary installed."

# ---------------------------------------------------------------------------
# Step 5: Write config
# ---------------------------------------------------------------------------
info "Writing config to $INSTALL_CFG/config.yaml..."
mkdir -p "$INSTALL_CFG"

cat > "$INSTALL_CFG/config.yaml" <<EOF
port: $PORT
scan_interval: $SCAN_INTERVAL
EOF

if [ -n "$TOKEN" ]; then
  echo "token: \"$TOKEN\"" >> "$INSTALL_CFG/config.yaml"
fi

success "Config written."

# ---------------------------------------------------------------------------
# Step 6: Install launchd plist
# ---------------------------------------------------------------------------
info "Installing launchd service..."

if [ ! -f "$PLIST_SRC" ]; then
  fail "Plist not found at $PLIST_SRC — make sure you're running this from the agent/ directory."
fi

# Unload existing service if running.
if launchctl list | grep -q "com.dablabs.smartha-agent" 2>/dev/null; then
  warn "Existing service found — unloading it first..."
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi

cp "$PLIST_SRC" "$PLIST_DEST"
chown root:wheel "$PLIST_DEST"
chmod 644 "$PLIST_DEST"
launchctl load -w "$PLIST_DEST"

success "Service installed and started."

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║   SMART Sniffer Agent installed successfully  ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo "  Endpoint : http://localhost:$PORT/api/health"
echo "  Config   : $INSTALL_CFG/config.yaml"
echo "  Logs     : /var/log/smartha-agent.log"
echo ""
echo "  Useful commands:"
echo "    Stop:    sudo launchctl unload $PLIST_DEST"
echo "    Start:   sudo launchctl load -w $PLIST_DEST"
echo "    Restart: sudo launchctl kickstart -k system/com.dablabs.smartha-agent"
echo "    Logs:    tail -f /var/log/smartha-agent.log"
echo "    Errors:  tail -f /var/log/smartha-agent.error.log"
echo ""

# Quick health check — retry a few times to give the agent time to start.
info "Waiting for agent to start..."
for i in 1 2 3 4 5; do
  sleep 2
  if curl -sf "http://localhost:$PORT/api/health" &>/dev/null; then
    success "Health check passed — agent is running."
    break
  fi
  if [ "$i" -eq 5 ]; then
    warn "Health check didn't respond after 10s. Check logs: tail -f /var/log/smartha-agent.log"
  fi
done
echo ""
