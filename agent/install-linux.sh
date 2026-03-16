#!/bin/bash
# SMART Sniffer Agent — Linux installer (systemd)
# Tested on Debian/Ubuntu/Proxmox. Adapt package manager for RHEL/Fedora.
# Usage: sudo bash install-linux.sh
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
SERVICE_NAME="smartha-agent"
SERVICE_DEST="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_SRC="$(dirname "$0")/systemd/${SERVICE_NAME}.service"

# Detect architecture for the pre-built binary name.
ARCH=$(uname -m)
case "$ARCH" in
  x86_64)  BINARY_ARCH="linux-amd64" ;;
  aarch64) BINARY_ARCH="linux-arm64" ;;
  *)       BINARY_ARCH="" ;;
esac

# ---------------------------------------------------------------------------
# Must run as root
# ---------------------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  fail "Please run as root: sudo bash $0"
fi

echo ""
echo -e "${BOLD}╔══════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   SMART Sniffer Agent — Installer    ║${NC}"
echo -e "${BOLD}║   Linux / systemd                    ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════╝${NC}"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Check for smartmontools
# ---------------------------------------------------------------------------
info "Checking for smartmontools..."
if ! command -v smartctl &>/dev/null; then
  warn "smartctl not found. Installing via apt..."
  if command -v apt-get &>/dev/null; then
    apt-get update -qq && apt-get install -y smartmontools
  elif command -v dnf &>/dev/null; then
    dnf install -y smartmontools
  elif command -v yum &>/dev/null; then
    yum install -y smartmontools
  else
    fail "Could not detect package manager. Install smartmontools manually and re-run."
  fi
fi
success "smartctl found: $(smartctl --version | head -1)"

# ---------------------------------------------------------------------------
# Step 2: Locate or build the binary
# ---------------------------------------------------------------------------
info "Locating binary..."

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -n "$BINARY_ARCH" ] && [ -f "$SCRIPT_DIR/build/${BINARY_NAME}-${BINARY_ARCH}" ]; then
  BINARY_SRC="$SCRIPT_DIR/build/${BINARY_NAME}-${BINARY_ARCH}"
  success "Found pre-built binary: $BINARY_SRC"
elif [ -f "$SCRIPT_DIR/build/${BINARY_NAME}" ]; then
  BINARY_SRC="$SCRIPT_DIR/build/${BINARY_NAME}"
  success "Found binary: $BINARY_SRC"
elif command -v go &>/dev/null; then
  info "No pre-built binary found — building from source..."
  cd "$SCRIPT_DIR"
  make build
  BINARY_SRC="$SCRIPT_DIR/build/${BINARY_NAME}"
  success "Build complete."
else
  fail "No binary found in build/ and Go is not installed.\nRun 'make linux-amd64' on your Mac and copy the binary to agent/build/ first."
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
# Step 6: Install systemd service
# ---------------------------------------------------------------------------
info "Installing systemd service..."

if [ ! -f "$SERVICE_SRC" ]; then
  fail "Service file not found at $SERVICE_SRC — make sure you're running this from the agent/ directory."
fi

# Stop existing service if running.
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
  warn "Existing service is running — stopping it first..."
  systemctl stop "$SERVICE_NAME"
fi

cp "$SERVICE_SRC" "$SERVICE_DEST"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME"

success "Service installed, enabled, and started."

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║   SMART Sniffer Agent installed successfully  ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
echo ""
echo "  Endpoint : http://$(hostname -I | awk '{print $1}'):$PORT/api/health"
echo "  Config   : $INSTALL_CFG/config.yaml"
echo ""
echo "  Useful commands:"
echo "    Status:  systemctl status $SERVICE_NAME"
echo "    Stop:    systemctl stop $SERVICE_NAME"
echo "    Start:   systemctl start $SERVICE_NAME"
echo "    Restart: systemctl restart $SERVICE_NAME"
echo "    Logs:    journalctl -u $SERVICE_NAME -f"
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
    warn "Health check didn't respond after 10s."
    warn "Check logs: journalctl -u $SERVICE_NAME -f"
  fi
done
echo ""
