#!/bin/bash
# ============================================================================
# SMART Sniffer Agent — Unified Installer (Linux + macOS)
#
# One-liner install:
#   curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo bash
#
# Or pin a specific version:
#   VERSION=0.1.0 curl -sSL ... | sudo bash
#
# Uninstall:
#   UNINSTALL=1 curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo bash
#   (or if already downloaded: sudo bash install.sh --uninstall)
#
# What this script does:
#   1. Detects OS (Linux/macOS) and architecture (amd64/arm64)
#   2. Downloads the correct binary from the latest GitHub Release
#   3. Verifies the download against SHA256 checksums
#   4. Installs smartmontools if missing
#   5. Prompts for port, token, and scan interval
#   6. Installs the binary, config, and system service
#   7. Starts the agent and verifies it's running
# ============================================================================
set -e

REPO="DAB-LABS/smart-sniffer"
BINARY_NAME="smartha-agent"
INSTALL_BIN="/usr/local/bin/$BINARY_NAME"
INSTALL_CFG="/etc/smartha-agent"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BOLD}  --> $*${NC}"; }
success() { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ⚠ $*${NC}"; }
fail()    { echo -e "${RED}  ✗ $*${NC}"; exit 1; }

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
do_uninstall() {
  echo ""
  echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
  echo -e "${BOLD}║   SMART Sniffer Agent — Uninstaller      ║${NC}"
  echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
  echo ""

  OS=$(uname -s | tr '[:upper:]' '[:lower:]')

  # Stop and remove service
  if [ "$OS" = "linux" ]; then
    if systemctl is-active --quiet smartha-agent 2>/dev/null; then
      info "Stopping service..."
      systemctl stop smartha-agent
    fi
    if [ -f /etc/systemd/system/smartha-agent.service ]; then
      info "Removing systemd service..."
      systemctl disable smartha-agent 2>/dev/null || true
      rm -f /etc/systemd/system/smartha-agent.service
      systemctl daemon-reload
      success "Service removed."
    fi
  elif [ "$OS" = "darwin" ]; then
    PLIST="/Library/LaunchDaemons/com.dablabs.smartha-agent.plist"
    if launchctl list | grep -q com.dablabs.smartha-agent 2>/dev/null; then
      info "Unloading launchd service..."
      launchctl unload "$PLIST" 2>/dev/null || true
    fi
    if [ -f "$PLIST" ]; then
      info "Removing plist..."
      rm -f "$PLIST"
      success "Service removed."
    fi
  fi

  # Remove binary
  if [ -f "$INSTALL_BIN" ]; then
    info "Removing binary..."
    rm -f "$INSTALL_BIN"
    success "Binary removed."
  fi

  # Remove config
  if [ -d "$INSTALL_CFG" ]; then
    info "Removing config directory ($INSTALL_CFG)..."
    rm -rf "$INSTALL_CFG"
    success "Config removed."
  fi

  # macOS log files
  if [ "$OS" = "darwin" ]; then
    rm -f /var/log/smartha-agent.log /var/log/smartha-agent.error.log 2>/dev/null
  fi

  echo ""
  echo -e "${GREEN}  ✓ SMART Sniffer Agent has been completely removed.${NC}"
  echo ""
  exit 0
}

# Check for --uninstall flag (via args or environment variable)
UNINSTALL=false
for arg in "$@"; do
  case "$arg" in
    --uninstall|-u|uninstall) UNINSTALL=true ;;
  esac
done
# Also support: UNINSTALL=1 curl ... | sudo bash
if [ "${UNINSTALL:-}" = "1" ] || [ "${UNINSTALL}" = "true" ]; then
  if [ "$EUID" -ne 0 ]; then
    fail "Please run as root: sudo bash $0 --uninstall"
  fi
  do_uninstall
fi

# ---------------------------------------------------------------------------
# Service install functions (defined before use)
# ---------------------------------------------------------------------------

# ===== LINUX: systemd service =====
install_linux_service() {
  SERVICE_NAME="smartha-agent"
  SERVICE_DEST="/etc/systemd/system/${SERVICE_NAME}.service"

  info "Installing systemd service..."

  # Stop existing service if running.
  if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    warn "Stopping existing service..."
    systemctl stop "$SERVICE_NAME"
  fi

  cat > "$SERVICE_DEST" <<'SVCEOF'
[Unit]
Description=SMART Sniffer Agent — disk health REST API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/smartha-agent
WorkingDirectory=/etc/smartha-agent
User=root
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  systemctl start "$SERVICE_NAME"
  success "systemd service installed, enabled, and started."

  echo ""
  echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}${BOLD}║   SMART Sniffer Agent installed successfully  ║${NC}"
  echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
  echo ""
  echo "  Endpoint : http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):$PORT/api/health"
  echo "  Config   : $INSTALL_CFG/config.yaml"
  echo ""
  echo "  Commands:"
  echo "    Status:    systemctl status $SERVICE_NAME"
  echo "    Logs:      journalctl -u $SERVICE_NAME -f"
  echo "    Restart:   systemctl restart $SERVICE_NAME"
  echo "    Uninstall: UNINSTALL=1 curl -sSL https://raw.githubusercontent.com/$REPO/main/install.sh | sudo bash"
  echo ""
}

# ===== MACOS: launchd plist =====
install_macos_service() {
  PLIST_NAME="com.dablabs.smartha-agent"
  PLIST_DEST="/Library/LaunchDaemons/${PLIST_NAME}.plist"

  info "Installing launchd service..."

  # Unload existing service if present.
  if launchctl list | grep -q "$PLIST_NAME" 2>/dev/null; then
    warn "Unloading existing service..."
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
  fi

  cat > "$PLIST_DEST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/smartha-agent</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/etc/smartha-agent</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>StandardOutPath</key>
    <string>/var/log/smartha-agent.log</string>
    <key>StandardErrorPath</key>
    <string>/var/log/smartha-agent.error.log</string>
</dict>
</plist>
PLISTEOF

  chown root:wheel "$PLIST_DEST"
  chmod 644 "$PLIST_DEST"
  launchctl load -w "$PLIST_DEST"
  success "launchd service installed and started."

  echo ""
  echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}${BOLD}║   SMART Sniffer Agent installed successfully  ║${NC}"
  echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════╝${NC}"
  echo ""
  echo "  Endpoint : http://localhost:$PORT/api/health"
  echo "  Config   : $INSTALL_CFG/config.yaml"
  echo "  Logs     : /var/log/smartha-agent.log"
  echo ""
  echo "  Commands:"
  echo "    Stop:      sudo launchctl unload $PLIST_DEST"
  echo "    Start:     sudo launchctl load -w $PLIST_DEST"
  echo "    Restart:   sudo launchctl kickstart -k system/$PLIST_NAME"
  echo "    Logs:      tail -f /var/log/smartha-agent.log"
  echo "    Uninstall: UNINSTALL=1 curl -sSL https://raw.githubusercontent.com/$REPO/main/install.sh | sudo bash"
  echo ""
}

# ---------------------------------------------------------------------------
# Must run as root
# ---------------------------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  fail "Please run as root: sudo bash $0"
fi

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   SMART Sniffer Agent — Installer        ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""

# ---------------------------------------------------------------------------
# Detect platform
# ---------------------------------------------------------------------------
OS=$(uname -s | tr '[:upper:]' '[:lower:]')
ARCH=$(uname -m)

case "$OS" in
  linux)  PLATFORM="linux" ;;
  darwin) PLATFORM="darwin" ;;
  *)      fail "Unsupported OS: $OS. This installer supports Linux and macOS." ;;
esac

case "$ARCH" in
  x86_64)  GOARCH="amd64" ;;
  aarch64) GOARCH="arm64" ;;
  arm64)   GOARCH="arm64" ;;
  *)       fail "Unsupported architecture: $ARCH" ;;
esac

BINARY_FILE="${BINARY_NAME}-${PLATFORM}-${GOARCH}"
info "Detected platform: ${PLATFORM}/${GOARCH}"

# ---------------------------------------------------------------------------
# Resolve version and download URL
# ---------------------------------------------------------------------------
if [ -z "${VERSION:-}" ]; then
  info "Fetching latest release version..."
  VERSION=$(curl -sSf "https://api.github.com/repos/$REPO/releases/latest" \
    | grep '"tag_name"' | head -1 | sed 's/.*"v\([^"]*\)".*/\1/')
  if [ -z "$VERSION" ]; then
    fail "Could not determine latest version. Set VERSION=x.y.z manually."
  fi
fi
success "Version: v${VERSION}"

RELEASE_URL="https://github.com/$REPO/releases/download/v${VERSION}"
BINARY_URL="${RELEASE_URL}/${BINARY_FILE}"
CHECKSUMS_URL="${RELEASE_URL}/checksums.txt"

# ---------------------------------------------------------------------------
# Download binary and verify checksum
# ---------------------------------------------------------------------------
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

info "Downloading ${BINARY_FILE}..."
curl -sSfL -o "$TMPDIR/$BINARY_FILE" "$BINARY_URL" \
  || fail "Download failed. Check that version v${VERSION} exists at:\n  ${BINARY_URL}"

info "Verifying checksum..."
curl -sSfL -o "$TMPDIR/checksums.txt" "$CHECKSUMS_URL" \
  || warn "Could not download checksums — skipping verification."

if [ -f "$TMPDIR/checksums.txt" ]; then
  EXPECTED=$(grep "$BINARY_FILE" "$TMPDIR/checksums.txt" | awk '{print $1}')
  if [ -n "$EXPECTED" ]; then
    if command -v sha256sum &>/dev/null; then
      ACTUAL=$(sha256sum "$TMPDIR/$BINARY_FILE" | awk '{print $1}')
    else
      ACTUAL=$(shasum -a 256 "$TMPDIR/$BINARY_FILE" | awk '{print $1}')
    fi
    if [ "$EXPECTED" = "$ACTUAL" ]; then
      success "Checksum verified."
    else
      fail "Checksum mismatch!\n  Expected: $EXPECTED\n  Got:      $ACTUAL"
    fi
  else
    warn "Binary not found in checksums file — skipping verification."
  fi
fi

# ---------------------------------------------------------------------------
# Install smartmontools
# ---------------------------------------------------------------------------
info "Checking for smartmontools..."
if ! command -v smartctl &>/dev/null; then
  if [ "$PLATFORM" = "darwin" ]; then
    warn "smartctl not found."
    if command -v brew &>/dev/null; then
      info "Installing via Homebrew..."
      sudo -u "${SUDO_USER:-$USER}" brew install smartmontools
    else
      fail "smartctl is required. Install it with: brew install smartmontools"
    fi
  else
    warn "smartctl not found. Installing..."
    if command -v apt-get &>/dev/null; then
      apt-get update -qq && apt-get install -y smartmontools
    elif command -v dnf &>/dev/null; then
      dnf install -y smartmontools
    elif command -v yum &>/dev/null; then
      yum install -y smartmontools
    else
      fail "Could not detect package manager. Install smartmontools manually."
    fi
  fi
fi
success "smartctl found: $(smartctl --version | head -1)"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# When piped through bash (curl | bash), stdin is the script itself, not the
# terminal. Redirect reads from /dev/tty so interactive prompts still work.
# If /dev/tty isn't available (e.g. CI), fall back to defaults silently.
if [ -t 0 ]; then
  TTY_IN="/dev/stdin"
elif [ -e /dev/tty ]; then
  TTY_IN="/dev/tty"
else
  TTY_IN=""
fi

if [ -n "$TTY_IN" ]; then
  echo ""
  echo -e "${BOLD}  Configuration${NC}"
  echo "  (Press Enter to accept defaults)"
  echo ""

  read -rp "  Port [9099]: " PORT < "$TTY_IN"
  read -rp "  Bearer token for API auth (leave blank to disable): " TOKEN < "$TTY_IN"
  read -rp "  Scan interval [60s]: " SCAN_INTERVAL < "$TTY_IN"
else
  info "Non-interactive mode detected — using defaults."
  PORT=""
  TOKEN=""
  SCAN_INTERVAL=""
fi

PORT="${PORT:-9099}"
SCAN_INTERVAL="${SCAN_INTERVAL:-60s}"

# ---------------------------------------------------------------------------
# Install binary
# ---------------------------------------------------------------------------
echo ""
info "Installing binary to $INSTALL_BIN..."
cp "$TMPDIR/$BINARY_FILE" "$INSTALL_BIN"
chmod +x "$INSTALL_BIN"
success "Binary installed."

# ---------------------------------------------------------------------------
# Write config
# ---------------------------------------------------------------------------
info "Writing config to $INSTALL_CFG/config.yaml..."
mkdir -p "$INSTALL_CFG"

cat > "$INSTALL_CFG/config.yaml" <<CONFEOF
port: $PORT
scan_interval: $SCAN_INTERVAL
CONFEOF

if [ -n "$TOKEN" ]; then
  echo "token: \"$TOKEN\"" >> "$INSTALL_CFG/config.yaml"
fi
success "Config written."

# ---------------------------------------------------------------------------
# Platform-specific service installation
# ---------------------------------------------------------------------------
if [ "$PLATFORM" = "linux" ]; then
  install_linux_service
elif [ "$PLATFORM" = "darwin" ]; then
  install_macos_service
fi

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
info "Waiting for agent to start..."
for i in 1 2 3 4 5; do
  sleep 2
  if curl -sf "http://localhost:$PORT/api/health" &>/dev/null; then
    success "Health check passed — agent is running!"
    break
  fi
  if [ "$i" -eq 5 ]; then
    warn "Health check didn't respond after 10s."
    if [ "$PLATFORM" = "linux" ]; then
      warn "Check logs: journalctl -u smartha-agent -f"
    else
      warn "Check logs: tail -f /var/log/smartha-agent.log"
    fi
  fi
done
echo ""
