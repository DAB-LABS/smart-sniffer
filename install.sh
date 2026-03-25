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
#   curl -sSL https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh | sudo UNINSTALL=1 bash
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${BOLD}  --> $*${NC}"; }
success() { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ⚠ $*${NC}"; }
fail()    { echo -e "${RED}  ✗ $*${NC}"; exit 1; }

# ---------------------------------------------------------------------------
# Network interface picker (reused by fresh install and upgrade paths)
#
# Detects interfaces with IPv4 addresses, tags virtual/VPN interfaces,
# and prompts the user to pick one. Sets ADV_IFACE to the chosen interface
# or "" for auto-filter.
#
# Arguments:
#   $1 — default choice label shown in the prompt (e.g., "A" or "current")
# ---------------------------------------------------------------------------
VIRTUAL_PREFIXES="docker|br-|veth|zt|tailscale|ts|wg|virbr|vbox|vmnet|utun|lo"

pick_interface() {
  local default_label="${1:-A}"
  IFACE_LIST=""
  IFACE_COUNT=0
  NON_VIRTUAL_COUNT=0

  # Build numbered interface list.
  for iface in $(ls /sys/class/net 2>/dev/null || ifconfig -l 2>/dev/null | tr ' ' '\n'); do
    # Get first IPv4 address for this interface.
    if command -v ip &>/dev/null; then
      ip4=$(ip -4 addr show "$iface" 2>/dev/null | grep -oP 'inet \K[0-9.]+' | head -1)
    else
      ip4=$(ifconfig "$iface" 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)
    fi
    [ -z "$ip4" ] && continue  # skip interfaces with no IPv4

    IFACE_COUNT=$((IFACE_COUNT + 1))
    label=""
    if echo "$iface" | grep -qiE "^($VIRTUAL_PREFIXES)"; then
      case "$iface" in
        docker*|br-*) label="Docker" ;;
        veth*)        label="Docker container" ;;
        zt*)          label="ZeroTier" ;;
        tailscale*|ts*) label="Tailscale" ;;
        wg*)          label="WireGuard" ;;
        virbr*)       label="libvirt" ;;
        vbox*)        label="VirtualBox" ;;
        vmnet*)       label="VMware" ;;
        utun*)        label="VPN tunnel" ;;
        lo*)          label="loopback" ;;
        *)            label="virtual" ;;
      esac
      echo -e "    ${IFACE_COUNT}) ${iface}$(printf '%*s' $((16 - ${#iface})) '')${ip4}  ${YELLOW}(${label})${NC}"
    else
      echo -e "    ${IFACE_COUNT}) ${iface}$(printf '%*s' $((16 - ${#iface})) '')${ip4}"
      NON_VIRTUAL_COUNT=$((NON_VIRTUAL_COUNT + 1))
    fi
    IFACE_LIST="${IFACE_LIST}${iface} "
  done

  echo -e "    A) All interfaces (auto-filter virtual)"
  echo ""

  ADV_IFACE=""
  if [ "$IFACE_COUNT" -gt 1 ]; then
    read -rp "  Advertise on interface [$default_label]: " IFACE_CHOICE < "$TTY_IN"
    if [ -n "$IFACE_CHOICE" ] && [ "$IFACE_CHOICE" != "A" ] && [ "$IFACE_CHOICE" != "a" ]; then
      # Convert number to interface name.
      local chosen_idx=0
      for iface_name in $IFACE_LIST; do
        chosen_idx=$((chosen_idx + 1))
        if [ "$chosen_idx" = "$IFACE_CHOICE" ]; then
          ADV_IFACE="$iface_name"
          break
        fi
      done
      if [ -z "$ADV_IFACE" ]; then
        warn "Invalid choice — using auto-filter."
      else
        success "mDNS will advertise on: $ADV_IFACE"
      fi
    fi
  elif [ "$IFACE_COUNT" -eq 1 ]; then
    info "Single interface detected — using auto-filter."
  fi
}

# Returns the count of non-virtual interfaces (call after pick_interface or
# after running the same detection loop). Used to decide whether to prompt
# during upgrades from pre-interface-picker configs.
count_non_virtual_interfaces() {
  NON_VIRTUAL_COUNT=0
  for iface in $(ls /sys/class/net 2>/dev/null || ifconfig -l 2>/dev/null | tr ' ' '\n'); do
    if command -v ip &>/dev/null; then
      ip4=$(ip -4 addr show "$iface" 2>/dev/null | grep -oP 'inet \K[0-9.]+' | head -1)
    else
      ip4=$(ifconfig "$iface" 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)
    fi
    [ -z "$ip4" ] && continue
    if ! echo "$iface" | grep -qiE "^($VIRTUAL_PREFIXES)"; then
      NON_VIRTUAL_COUNT=$((NON_VIRTUAL_COUNT + 1))
    fi
  done
}

# ---------------------------------------------------------------------------
# Install-path defaults (may be overridden by resolve_install_paths below)
# ---------------------------------------------------------------------------
INSTALL_BIN="/usr/local/bin/$BINARY_NAME"
INSTALL_CFG="/etc/smartha-agent"

# ---------------------------------------------------------------------------
# Resolve writable install paths
#
# Standard Linux/macOS:  /usr/local/bin  +  /etc/smartha-agent
# Immutable-rootfs (ZimaOS, etc.):  /DATA/smartha-agent  (bin + config)
# Generic fallback:  /opt/smartha-agent  (bin + config)
#
# The probe runs as root (installer requires sudo), so a writability failure
# genuinely means the filesystem is read-only, not a permissions issue.
# ---------------------------------------------------------------------------
resolve_install_paths() {
  # Candidate 1: standard paths (works on most Linux, macOS, Proxmox, etc.)
  if mkdir -p /usr/local/bin 2>/dev/null && [ -w /usr/local/bin ]; then
    INSTALL_BIN="/usr/local/bin/$BINARY_NAME"
    INSTALL_CFG="/etc/smartha-agent"
    return
  fi

  # Candidate 2: /DATA (ZimaOS, CasaOS, and similar NAS distros)
  if [ -d /DATA ] && mkdir -p /DATA/smartha-agent 2>/dev/null && [ -w /DATA/smartha-agent ]; then
    INSTALL_BIN="/DATA/smartha-agent/$BINARY_NAME"
    INSTALL_CFG="/DATA/smartha-agent"
    warn "Immutable root filesystem detected — installing to /DATA/smartha-agent/"
    return
  fi

  # Candidate 3: /opt (generic fallback)
  if mkdir -p /opt/smartha-agent 2>/dev/null && [ -w /opt/smartha-agent ]; then
    INSTALL_BIN="/opt/smartha-agent/$BINARY_NAME"
    INSTALL_CFG="/opt/smartha-agent"
    warn "Standard paths not writable — installing to /opt/smartha-agent/"
    return
  fi

  fail "No writable install location found. Tried /usr/local/bin, /DATA/smartha-agent, /opt/smartha-agent."
}

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

  # Remove binary and config from all candidate locations
  FOUND=false
  for BIN_PATH in \
    "/usr/local/bin/$BINARY_NAME" \
    "/DATA/smartha-agent/$BINARY_NAME" \
    "/opt/smartha-agent/$BINARY_NAME"; do
    if [ -f "$BIN_PATH" ]; then
      info "Removing binary ($BIN_PATH)..."
      rm -f "$BIN_PATH"
      success "Binary removed."
      FOUND=true
    fi
  done

  for CFG_PATH in \
    "/etc/smartha-agent" \
    "/DATA/smartha-agent" \
    "/opt/smartha-agent"; do
    if [ -d "$CFG_PATH" ]; then
      info "Removing config directory ($CFG_PATH)..."
      rm -rf "$CFG_PATH"
      success "Config removed."
      FOUND=true
    fi
  done

  if [ "$FOUND" = "false" ]; then
    warn "No installed files found in any known location."
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
# IMPORTANT: Save env var BEFORE overwriting, since UNINSTALL=1 may come
# from the caller's environment (e.g. curl ... | sudo UNINSTALL=1 bash)
_UNINSTALL_ENV="${UNINSTALL:-}"
UNINSTALL_REQUESTED=false
for arg in "$@"; do
  case "$arg" in
    --uninstall|-u|uninstall) UNINSTALL_REQUESTED=true ;;
  esac
done
if [ "$_UNINSTALL_ENV" = "1" ] || [ "$_UNINSTALL_ENV" = "true" ] || [ "$UNINSTALL_REQUESTED" = "true" ]; then
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

  cat > "$SERVICE_DEST" <<SVCEOF
[Unit]
Description=SMART Sniffer Agent — disk health REST API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$INSTALL_BIN
WorkingDirectory=$INSTALL_CFG
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
  echo "    Uninstall: curl -sSL https://raw.githubusercontent.com/$REPO/main/install.sh | sudo UNINSTALL=1 bash"
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
        <string>${INSTALL_BIN}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${INSTALL_CFG}</string>
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
  echo "    Uninstall: curl -sSL https://raw.githubusercontent.com/$REPO/main/install.sh | sudo UNINSTALL=1 bash"
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

# Probe for writable install paths (must run after root check)
resolve_install_paths
info "Install location: $INSTALL_BIN"

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

# ---------------------------------------------------------------------------
# Check for existing configuration (upgrade detection)
# ---------------------------------------------------------------------------
EXISTING_CONFIG="$INSTALL_CFG/config.yaml"
KEEP_CONFIG=false

if [ -f "$EXISTING_CONFIG" ]; then
  # Parse existing values from config.yaml
  _EXISTING_PORT=$(grep -E '^port:' "$EXISTING_CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '"' || true)
  _EXISTING_TOKEN=$(grep -E '^token:' "$EXISTING_CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '"' || true)
  _EXISTING_INTERVAL=$(grep -E '^scan_interval:' "$EXISTING_CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '"' || true)
  _EXISTING_IFACE=$(grep -E '^advertise_interface:' "$EXISTING_CONFIG" 2>/dev/null | awk '{print $2}' | tr -d '"' || true)

  # Only treat as valid if we got at least a port
  if [ -n "$_EXISTING_PORT" ]; then
    echo ""
    echo -e "${GREEN}${BOLD}  Existing configuration found:${NC}"
    echo "    Port:       $_EXISTING_PORT"
    if [ -n "$_EXISTING_TOKEN" ]; then
      # Mask the token for display
      _TOKEN_LEN=${#_EXISTING_TOKEN}
      if [ "$_TOKEN_LEN" -gt 4 ]; then
        _TOKEN_DISPLAY="${_EXISTING_TOKEN:0:2}$(printf '%*s' $((_TOKEN_LEN - 4)) '' | tr ' ' '•')${_EXISTING_TOKEN:$((_TOKEN_LEN - 2))}"
      else
        _TOKEN_DISPLAY="••••"
      fi
      echo "    Token:      $_TOKEN_DISPLAY"
    else
      echo "    Token:      (none)"
    fi
    echo "    Interval:   ${_EXISTING_INTERVAL:-60s}"
    if [ -n "$_EXISTING_IFACE" ]; then
      echo "    Interface:  $_EXISTING_IFACE"
    else
      echo "    Interface:  auto-filter"
    fi
    echo ""

    if [ -n "$TTY_IN" ]; then
      read -rp "  Keep current settings? [Y/n]: " KEEP_CHOICE < "$TTY_IN"
      case "$KEEP_CHOICE" in
        [nN]|[nN][oO]) KEEP_CONFIG=false ;;
        *)             KEEP_CONFIG=true ;;
      esac
    else
      # Non-interactive upgrade: always keep existing config
      info "Non-interactive mode — keeping existing configuration."
      KEEP_CONFIG=true
    fi

    if [ "$KEEP_CONFIG" = "true" ]; then
      PORT="$_EXISTING_PORT"
      TOKEN="$_EXISTING_TOKEN"
      SCAN_INTERVAL="${_EXISTING_INTERVAL:-60s}"
      ADV_IFACE="$_EXISTING_IFACE"
      success "Keeping existing configuration."

      # --- Upgrade path: offer interface picker if config predates v0.4.25 ---
      # Old configs won't have advertise_interface. On multi-homed hosts this
      # can cause mDNS to advertise on a VPN or Docker IP. Prompt once.
      if [ -z "$ADV_IFACE" ] && [ -n "$TTY_IN" ]; then
        count_non_virtual_interfaces
        if [ "$NON_VIRTUAL_COUNT" -gt 1 ]; then
          echo ""
          warn "Your config doesn't specify a network interface for mDNS."
          echo "  Machines with multiple interfaces may advertise on the wrong IP."
          echo ""

          pick_interface "A"

          if [ -n "$ADV_IFACE" ]; then
            # Append to existing config (don't rewrite it)
            echo "advertise_interface: $ADV_IFACE" >> "$EXISTING_CONFIG"
            success "Interface saved to existing config."
          fi
        fi
      fi
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Configuration prompts (skipped if keeping existing config)
# ---------------------------------------------------------------------------
if [ "$KEEP_CONFIG" = "false" ] && [ -n "$TTY_IN" ]; then
  echo ""
  echo -e "${BOLD}  Configuration${NC}"
  echo "  (Press Enter to accept defaults)"
  echo ""

  read -rp "  Port [9099]: " PORT < "$TTY_IN"
  read -rp "  Bearer token for API auth (leave blank to disable): " TOKEN < "$TTY_IN"
  read -rp "  Scan interval [60s]: " SCAN_INTERVAL < "$TTY_IN"

  # --- Network interface picker for mDNS advertisement ---
  echo ""
  echo -e "${BOLD}  Network interface for mDNS discovery${NC}"
  echo "  (Home Assistant uses this to auto-discover the agent)"
  echo ""

  pick_interface "A"

elif [ "$KEEP_CONFIG" = "false" ]; then
  info "Non-interactive mode detected — using defaults."
  PORT=""
  TOKEN=""
  SCAN_INTERVAL=""
  ADV_IFACE=""
fi

PORT="${PORT:-9099}"
SCAN_INTERVAL="${SCAN_INTERVAL:-60s}"

# If the user entered a bare number (e.g. "30"), append "s" for Go duration.
if echo "$SCAN_INTERVAL" | grep -qE '^[0-9]+$'; then
  SCAN_INTERVAL="${SCAN_INTERVAL}s"
fi

# ---------------------------------------------------------------------------
# Install binary (stop service first to avoid "Text file busy")
# ---------------------------------------------------------------------------
echo ""
if [ "$PLATFORM" = "linux" ] && systemctl is-active --quiet smartha-agent 2>/dev/null; then
  info "Stopping running agent before upgrade..."
  systemctl stop smartha-agent
fi
if [ "$PLATFORM" = "darwin" ] && launchctl list | grep -q com.dablabs.smartha-agent 2>/dev/null; then
  info "Stopping running agent before upgrade..."
  launchctl unload /Library/LaunchDaemons/com.dablabs.smartha-agent.plist 2>/dev/null || true
fi
info "Installing binary to $INSTALL_BIN..."
cp "$TMPDIR/$BINARY_FILE" "$INSTALL_BIN"
chmod +x "$INSTALL_BIN"
success "Binary installed."

# ---------------------------------------------------------------------------
# Write config (skip if keeping existing config)
# ---------------------------------------------------------------------------
if [ "$KEEP_CONFIG" = "true" ]; then
  info "Keeping existing config at $INSTALL_CFG/config.yaml"
else
  info "Writing config to $INSTALL_CFG/config.yaml..."
  mkdir -p "$INSTALL_CFG"

  cat > "$INSTALL_CFG/config.yaml" <<CONFEOF
port: $PORT
scan_interval: $SCAN_INTERVAL
CONFEOF

  if [ -n "$TOKEN" ]; then
    echo "token: \"$TOKEN\"" >> "$INSTALL_CFG/config.yaml"
  fi
  if [ -n "$ADV_IFACE" ]; then
    echo "advertise_interface: $ADV_IFACE" >> "$INSTALL_CFG/config.yaml"
  fi
  success "Config written."
fi

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
HEALTH_CURL="curl -sf http://localhost:$PORT/api/health"
if [ -n "$TOKEN" ]; then
  HEALTH_CURL="curl -sf -H \"Authorization: Bearer $TOKEN\" http://localhost:$PORT/api/health"
fi
for i in 1 2 3 4 5; do
  sleep 2
  if eval "$HEALTH_CURL" &>/dev/null; then
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
