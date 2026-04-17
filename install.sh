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
# Disk usage picker — detects real block-device mounts, shows numbered list,
# user enters comma-separated numbers, "all", or "none".
# Sets FS_YAML with the config.yaml entries and FS_DISPLAY with mount list.
# ---------------------------------------------------------------------------
FS_YAML=""
FS_DISPLAY=""

pick_filesystems() {
  FS_YAML=""
  FS_DISPLAY=""

  # Parallel arrays for detected filesystems.
  local -a fs_mps=()
  local -a fs_devs=()
  local -a fs_types=()
  local -a fs_uuids=()
  local -a fs_labels=()

  # Detect real block-device mounts.
  local mounts
  if [ -f /proc/mounts ]; then
    mounts=$(cat /proc/mounts)
  else
    mounts=$(mount)
  fi

  while IFS= read -r line; do
    local dev mp fstype

    # macOS mount output: /dev/disk3s1s1 on / (apfs, sealed, local, ...)
    # Linux /proc/mounts: /dev/sda1 / ext4 rw,relatime 0 0
    if [[ "$OSTYPE" == darwin* ]]; then
      dev=$(echo "$line" | awk '{print $1}')
      mp=$(echo "$line" | sed 's/.* on \(.*\) (.*/\1/' | sed 's/ *$//')
      fstype=$(echo "$line" | sed 's/.*(\([^,)]*\).*/\1/' | sed 's/ *$//')
    else
      dev=$(echo "$line" | awk '{print $1}')
      mp=$(echo "$line" | awk '{print $2}')
      fstype=$(echo "$line" | awk '{print $3}')
    fi

    # Filter to real block devices and common filesystems.
    case "$dev" in
      /dev/sd*|/dev/nvme*|/dev/md*|/dev/mapper/*|/dev/vd*|/dev/xvd*|/dev/hd*|/dev/disk*) ;;
      *) case "$fstype" in zfs) ;; *) continue ;; esac ;;
    esac

    # Skip virtual/special filesystems.
    case "$fstype" in
      tmpfs|overlay|squashfs|proc|sysfs|devtmpfs|devpts|cgroup*|autofs|fusectl|securityfs|debugfs|configfs|pstore|binfmt_misc)
        continue ;;
    esac

    # Skip macOS system/virtual volumes and pseudo-filesystems.
    if [[ "$OSTYPE" == darwin* ]]; then
      case "$mp" in
        /System/Volumes/Preboot|/System/Volumes/Recovery|/System/Volumes/VM)
          continue ;;
        /System/Volumes/xarts|/System/Volumes/iSCPreboot|/System/Volumes/Hardware)
          continue ;;
      esac
      case "$fstype" in devfs|autofs|synthfs) continue ;; esac
    fi

    # Skip snap and docker mounts.
    case "$mp" in /snap/*|/var/lib/docker/*) continue ;; esac

    # Get usage info from df.
    # macOS df doesn't support -B1 (GNU coreutils). Use -k for 1K blocks
    # on macOS and -B1 for byte-accurate values on Linux.
    local df_line total pct hr_total
    if [[ "$OSTYPE" == darwin* ]]; then
      df_line=$(df -k "$mp" 2>/dev/null | tail -1)
      total=$(echo "$df_line" | awk '{print $2}')
      # df -k returns 1K blocks; convert to bytes.
      total=$((total * 1024))
      pct=$(echo "$df_line" | awk '{print $5}' | tr -d '%')
    else
      df_line=$(df -B1 "$mp" 2>/dev/null | tail -1)
      total=$(echo "$df_line" | awk '{print $2}')
      pct=$(echo "$df_line" | awk '{print $5}' | tr -d '%')
    fi

    if [ "$total" -gt 1099511627776 ] 2>/dev/null; then
      hr_total="$(echo "$total" | awk '{printf "%.1fT", $1/1099511627776}')";
    elif [ "$total" -gt 1073741824 ] 2>/dev/null; then
      hr_total="$(echo "$total" | awk '{printf "%.0fG", $1/1073741824}')";
    elif [ "$total" -gt 1048576 ] 2>/dev/null; then
      hr_total="$(echo "$total" | awk '{printf "%.0fM", $1/1048576}')";
    else
      hr_total="${total}B"
    fi

    # Get UUID.
    local uuid=""
    if command -v blkid &>/dev/null && [ -b "$dev" ]; then
      uuid=$(blkid -s UUID -o value "$dev" 2>/dev/null || true)
    fi
    if [ -z "$uuid" ] && command -v diskutil &>/dev/null; then
      uuid=$(diskutil info "$dev" 2>/dev/null | grep "Volume UUID" | awk '{print $NF}' || true)
    fi

    fs_mps+=("$mp")
    fs_devs+=("$dev")
    fs_types+=("$fstype")
    fs_uuids+=("$uuid")
    fs_labels+=("$(printf '%-16s %-6s %6s  (%s%% used)' "$mp" "$fstype" "$hr_total" "$pct")")

  done <<< "$mounts"

  local count=${#fs_mps[@]}
  if [ "$count" -eq 0 ]; then
    info "No block-device filesystems detected — skipping disk usage monitoring."
    return
  fi

  echo ""
  echo -e "  ${BOLD}Disk Usage Monitoring${NC}"
  echo "  Select mountpoints to report to Home Assistant."
  echo ""
  for ((i=0; i<count; i++)); do
    echo "    $((i + 1))) ${fs_labels[$i]}"
  done
  echo ""

  local range_hint="1"
  [ "$count" -gt 1 ] && range_hint="1,2..$count"
  read -rp "  Monitor ($range_hint / all / none) [all]: " FS_CHOICE < "$TTY_IN"
  FS_CHOICE="${FS_CHOICE:-all}"

  # Parse selection.
  local -a selected_indices=()
  case "$FS_CHOICE" in
    all|ALL|a|A)
      for ((i=0; i<count; i++)); do selected_indices+=("$i"); done
      ;;
    none|NONE|n|N)
      info "Disk usage monitoring disabled."
      return
      ;;
    *)
      # Comma-separated numbers.
      IFS=',' read -ra nums <<< "$FS_CHOICE"
      for num in "${nums[@]}"; do
        num=$(echo "$num" | tr -d ' ')
        if [[ "$num" =~ ^[0-9]+$ ]] && [ "$num" -ge 1 ] && [ "$num" -le "$count" ]; then
          selected_indices+=("$((num - 1))")
        else
          warn "Skipping invalid choice: $num"
        fi
      done
      ;;
  esac

  if [ ${#selected_indices[@]} -eq 0 ]; then
    info "No valid mountpoints selected — disk usage monitoring disabled."
    return
  fi

  # Build YAML and display string.
  FS_YAML="filesystems:"
  local -a display_mps=()
  for idx in "${selected_indices[@]}"; do
    FS_YAML="${FS_YAML}
  - path: \"${fs_mps[$idx]}\"
    uuid: \"${fs_uuids[$idx]}\"
    device: \"${fs_devs[$idx]}\"
    fstype: \"${fs_types[$idx]}\""
    display_mps+=("${fs_mps[$idx]}")
  done

  FS_DISPLAY=$(IFS=', '; echo "${display_mps[*]}")
  success "Monitoring ${#selected_indices[@]} mountpoint(s): $FS_DISPLAY"
}

# ---------------------------------------------------------------------------
# Network interface picker — shows numbered list, user enters a number
# or "all". Sets ADV_IFACE to the chosen interface or "" for auto-filter.
# ---------------------------------------------------------------------------
VIRTUAL_PREFIXES="docker|br-|veth|zt|tailscale|ts|wg|virbr|vbox|vmnet|utun|lo"

pick_interface() {
  local -a iface_names=()
  local -a iface_labels=()
  IFACE_COUNT=0
  NON_VIRTUAL_COUNT=0

  for iface in $(ls /sys/class/net 2>/dev/null || ifconfig -l 2>/dev/null | tr ' ' '\n'); do
    local ip4=""
    if command -v ip &>/dev/null; then
      ip4=$(ip -4 addr show "$iface" 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)
    else
      ip4=$(ifconfig "$iface" 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)
    fi
    [ -z "$ip4" ] && continue

    IFACE_COUNT=$((IFACE_COUNT + 1))
    local tag_label=""
    if echo "$iface" | grep -qiE "^($VIRTUAL_PREFIXES)"; then
      case "$iface" in
        docker*|br-*) tag_label="${YELLOW}(Docker)${NC}" ;;
        veth*)        tag_label="${YELLOW}(Docker container)${NC}" ;;
        zt*)          tag_label="${YELLOW}(ZeroTier)${NC}" ;;
        tailscale*|ts*) tag_label="${YELLOW}(Tailscale)${NC}" ;;
        wg*)          tag_label="${YELLOW}(WireGuard)${NC}" ;;
        virbr*)       tag_label="${YELLOW}(libvirt)${NC}" ;;
        vbox*)        tag_label="${YELLOW}(VirtualBox)${NC}" ;;
        vmnet*)       tag_label="${YELLOW}(VMware)${NC}" ;;
        utun*)        tag_label="${YELLOW}(VPN tunnel)${NC}" ;;
        lo*)          tag_label="${YELLOW}(loopback)${NC}" ;;
        *)            tag_label="${YELLOW}(virtual)${NC}" ;;
      esac
    else
      NON_VIRTUAL_COUNT=$((NON_VIRTUAL_COUNT + 1))
    fi

    iface_names+=("$iface")
    iface_labels+=("$(printf '%-16s %s  %s' "$iface" "$ip4" "$tag_label")")
  done

  ADV_IFACE=""
  if [ "$IFACE_COUNT" -le 1 ]; then
    info "Single interface detected — using auto-filter."
    return
  fi

  echo ""
  echo -e "  ${BOLD}Network Interface (mDNS)${NC}"
  echo "  Home Assistant uses this to auto-discover the agent."
  echo ""
  for ((i=0; i<${#iface_names[@]}; i++)); do
    echo -e "    $((i + 1))) ${iface_labels[$i]}"
  done
  echo ""

  read -rp "  Advertise on (1-${#iface_names[@]} / all) [all]: " IFACE_CHOICE < "$TTY_IN"
  IFACE_CHOICE="${IFACE_CHOICE:-all}"

  case "$IFACE_CHOICE" in
    all|ALL|a|A|"")
      info "mDNS: auto-filter mode (all physical interfaces)."
      ADV_IFACE=""
      ;;
    *)
      if [[ "$IFACE_CHOICE" =~ ^[0-9]+$ ]] && [ "$IFACE_CHOICE" -ge 1 ] && [ "$IFACE_CHOICE" -le "${#iface_names[@]}" ]; then
        ADV_IFACE="${iface_names[$((IFACE_CHOICE - 1))]}"
        success "mDNS will advertise on: $ADV_IFACE"
      else
        warn "Invalid choice — using auto-filter."
        ADV_IFACE=""
      fi
      ;;
  esac
}

# Returns the count of non-virtual interfaces (call after pick_interface or
# after running the same detection loop). Used to decide whether to prompt
# during upgrades from pre-interface-picker configs.
count_non_virtual_interfaces() {
  NON_VIRTUAL_COUNT=0
  for iface in $(ls /sys/class/net 2>/dev/null || ifconfig -l 2>/dev/null | tr ' ' '\n'); do
    if command -v ip &>/dev/null; then
      ip4=$(ip -4 addr show "$iface" 2>/dev/null | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)
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
  _EXISTING_FS=$(grep -E '^\s+- path:' "$EXISTING_CONFIG" 2>/dev/null | sed 's/.*path:\s*//' | tr -d '"' || true)

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
    if [ -n "$_EXISTING_FS" ]; then
      _FS_LIST=$(echo "$_EXISTING_FS" | paste -sd ', ' -)
      echo "    Disk usage: $_FS_LIST"
    else
      echo "    Disk usage: (not configured)"
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
      # Carry forward filesystem config for the Agent Summary display.
      if [ -n "$_EXISTING_FS" ]; then
        FS_YAML="existing"   # non-empty sentinel — config already has the block
        FS_DISPLAY=$(echo "$_EXISTING_FS" | paste -sd ', ' -)
      fi
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

      # --- Upgrade path: offer filesystem picker if config has no filesystems ---
      if ! grep -q '^filesystems:' "$EXISTING_CONFIG" 2>/dev/null && [ -n "$TTY_IN" ]; then
        echo ""
        info "Disk usage monitoring is now available."
        echo ""
        pick_filesystems
        if [ -n "$FS_YAML" ]; then
          echo "" >> "$EXISTING_CONFIG"
          echo "$FS_YAML" >> "$EXISTING_CONFIG"
          success "Filesystem monitoring added to existing config."
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
  read -rp "  Scan interval (e.g. 60s, 30m, 24h) [60s]: " SCAN_INTERVAL < "$TTY_IN"

  # --- Disk usage picker ---
  echo ""
  pick_filesystems

  # --- Network interface picker ---
  echo ""
  pick_interface

elif [ "$KEEP_CONFIG" = "false" ]; then
  info "Non-interactive mode detected — using defaults."
  PORT=""
  TOKEN=""
  SCAN_INTERVAL=""
  ADV_IFACE=""
  FS_YAML=""
  FS_DISPLAY=""
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
  if [ -n "$FS_YAML" ]; then
    echo "" >> "$INSTALL_CFG/config.yaml"
    echo "$FS_YAML" >> "$INSTALL_CFG/config.yaml"
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
HEALTH_OK=false
HEALTH_CURL="curl -sf http://localhost:$PORT/api/health"
if [ -n "$TOKEN" ]; then
  HEALTH_CURL="curl -sf -H \"Authorization: Bearer $TOKEN\" http://localhost:$PORT/api/health"
fi
for i in 1 2 3 4 5; do
  sleep 2
  if eval "$HEALTH_CURL" &>/dev/null; then
    HEALTH_OK=true
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

# ---------------------------------------------------------------------------
# Post-install summary
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}  ── Agent Summary ──────────────────────────────${NC}"
echo ""

# Detect IP for display.
_AGENT_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$_AGENT_IP" ] && _AGENT_IP=$(ifconfig 2>/dev/null | grep -oE 'inet [0-9.]+' | grep -v '127.0.0.1' | head -1 | awk '{print $2}')
[ -z "$_AGENT_IP" ] && _AGENT_IP="localhost"

if [ "$HEALTH_OK" = "true" ]; then
  echo -e "  ${GREEN}✓${NC} Status:         ${GREEN}running${NC}"
else
  echo -e "  ${RED}✗${NC} Status:         ${RED}not responding${NC}"
fi
echo -e "  ${GREEN}✓${NC} Port:           ${PORT}"
echo -e "  ${GREEN}✓${NC} IP:             ${_AGENT_IP}"
echo -e "  ${GREEN}✓${NC} Endpoints:      http://${_AGENT_IP}:${PORT}"
echo "                    /api/health"
echo "                    /api/drives"
echo "                    /api/drives/{id}"
if [ -n "$FS_YAML" ] && [ -n "$FS_DISPLAY" ]; then
  echo "                    /api/filesystems"
fi
if [ -n "$TOKEN" ]; then
  echo -e "  ${GREEN}✓${NC} Auth:           enabled"
else
  echo -e "  ${YELLOW}○${NC} Auth:           disabled"
fi
echo -e "  ${GREEN}✓${NC} Scan interval:  ${SCAN_INTERVAL}"
# mDNS — show the actual instance name the agent will advertise.
_MDNS_HOSTNAME=$(hostname 2>/dev/null)
_MDNS_HOSTNAME="${_MDNS_HOSTNAME%%.*}"  # strip domain suffix
_MDNS_INSTANCE="smartha-${_MDNS_HOSTNAME}"
if [ -n "$ADV_IFACE" ]; then
  echo -e "  ${GREEN}✓${NC} mDNS:           ${_MDNS_INSTANCE}._smartha._tcp.local. (${ADV_IFACE})"
else
  echo -e "  ${GREEN}✓${NC} mDNS:           ${_MDNS_INSTANCE}._smartha._tcp.local. (all physical)"
fi

# SMART drives — query the running agent if possible.
_DRIVE_CURL="curl -sf http://localhost:$PORT/api/drives"
if [ -n "$TOKEN" ]; then
  _DRIVE_CURL="curl -sf -H \"Authorization: Bearer $TOKEN\" http://localhost:$PORT/api/drives"
fi
_DRIVE_JSON=$(eval "$_DRIVE_CURL" 2>/dev/null || true)
if [ -n "$_DRIVE_JSON" ]; then
  _DRIVE_COUNT=$(echo "$_DRIVE_JSON" | grep -o '"id"' | wc -l)
  _DRIVE_NAMES=$(echo "$_DRIVE_JSON" | sed -n 's/.*"model" *: *"\([^"]*\)".*/\1/p' | paste -sd ', ' -)
  if [ "$_DRIVE_COUNT" -gt 0 ]; then
    echo -e "  ${GREEN}✓${NC} SMART drives:   ${_DRIVE_COUNT} detected"
    if [ -n "$_DRIVE_NAMES" ]; then
      echo "                    ${_DRIVE_NAMES}"
    fi
  else
    echo -e "  ${YELLOW}○${NC} SMART drives:   none detected"
  fi
else
  echo -e "  ${YELLOW}○${NC} SMART drives:   (could not query)"
fi

# Disk usage monitoring.
if [ -n "$FS_YAML" ] && [ -n "$FS_DISPLAY" ]; then
  echo -e "  ${GREEN}✓${NC} Disk usage:     ${FS_DISPLAY}"
else
  echo -e "  ${YELLOW}○${NC} Disk usage:     disabled"
fi

echo -e "  ${GREEN}✓${NC} Config:         ${INSTALL_CFG}/config.yaml"
echo ""

