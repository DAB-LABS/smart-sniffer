#!/bin/bash
# Creates a Home Assistant OS test VM (ID 999) on Proxmox.
# Run this directly on the Proxmox host shell (SSH in first).
#
# Usage: bash proxmox-create-ha-test.sh
set -e

# ---------------------------------------------------------------------------
# Config — adjust if needed
# ---------------------------------------------------------------------------
VMID=999
VMNAME="ha-test"
NODE="brookdale"
STORAGE="local-lvm"
BRIDGE="vmbr0"
CORES=2
MEMORY=4096   # MB
TMPDIR="/tmp/haos-install"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${BOLD}  --> $*${NC}"; }
success() { echo -e "${GREEN}  ✓ $*${NC}"; }
warn()    { echo -e "${YELLOW}  ⚠ $*${NC}"; }

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  Home Assistant OS — Proxmox VM Creator  ║${NC}"
echo -e "${BOLD}║  VM ID: $VMID  Name: $VMNAME               ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════╝${NC}"
echo ""

# ---------------------------------------------------------------------------
# 1. Check we're on the Proxmox host
# ---------------------------------------------------------------------------
if ! command -v qm &>/dev/null; then
  echo "ERROR: 'qm' not found. Run this script on the Proxmox host, not locally."
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Check VM ID is free
# ---------------------------------------------------------------------------
if qm status $VMID &>/dev/null; then
  echo "ERROR: VM $VMID already exists. Remove it first: qm destroy $VMID"
  exit 1
fi
success "VM ID $VMID is available."

# ---------------------------------------------------------------------------
# 3. Fetch latest HAOS version from GitHub
# ---------------------------------------------------------------------------
info "Fetching latest HAOS release version..."
LATEST=$(curl -fsSL "https://api.github.com/repos/home-assistant/operating-system/releases/latest" \
  | grep '"tag_name"' | sed 's/.*"tag_name": "\(.*\)".*/\1/')

if [ -z "$LATEST" ]; then
  echo "ERROR: Could not fetch latest HAOS version. Check internet access."
  exit 1
fi
success "Latest HAOS version: $LATEST"

# ---------------------------------------------------------------------------
# 4. Download and decompress the HAOS qcow2 image
#    As of HAOS 13+, the image is distributed as .qcow2.xz (xz-compressed).
# ---------------------------------------------------------------------------
IMAGE_XZ="haos_ova-${LATEST}.qcow2.xz"
IMAGE="haos_ova-${LATEST}.qcow2"
URL="https://github.com/home-assistant/operating-system/releases/download/${LATEST}/${IMAGE_XZ}"

mkdir -p "$TMPDIR"
cd "$TMPDIR"

if [ -f "$IMAGE" ]; then
  warn "Decompressed image already exists, skipping download: $IMAGE"
elif [ -f "$IMAGE_XZ" ]; then
  warn "Compressed image already downloaded, skipping download: $IMAGE_XZ"
  info "Decompressing $IMAGE_XZ..."
  xz -d "$IMAGE_XZ"
  success "Decompressed: $IMAGE"
else
  info "Downloading $IMAGE_XZ (~400MB compressed, this will take a moment)..."
  wget --show-progress "$URL" -O "$IMAGE_XZ"
  info "Decompressing $IMAGE_XZ (~1GB uncompressed, this will take a moment)..."
  xz -d "$IMAGE_XZ"
  success "Image ready: $TMPDIR/$IMAGE"
fi

# ---------------------------------------------------------------------------
# 5. Create the VM
# ---------------------------------------------------------------------------
info "Creating VM $VMID ($VMNAME)..."

qm create $VMID \
  --name "$VMNAME" \
  --memory $MEMORY \
  --cores $CORES \
  --cpu host \
  --net0 virtio,bridge=$BRIDGE \
  --bios ovmf \
  --machine q35 \
  --efidisk0 ${STORAGE}:0,efitype=4m,pre-enrolled-keys=0 \
  --scsihw virtio-scsi-pci \
  --boot order=scsi0 \
  --agent enabled=1 \
  --onboot 0

success "VM skeleton created."

# ---------------------------------------------------------------------------
# 6. Import and attach the disk
# ---------------------------------------------------------------------------
info "Importing disk image into $STORAGE (this may take a minute)..."
qm importdisk $VMID "$IMAGE" $STORAGE --format raw

# The imported disk shows up as "unused0" — attach it as scsi0.
qm set $VMID --scsi0 ${STORAGE}:vm-${VMID}-disk-1

success "Disk imported and attached."

# ---------------------------------------------------------------------------
# 7. Clean up the downloaded image
# ---------------------------------------------------------------------------
info "Cleaning up temporary files..."
rm -f "$TMPDIR/$IMAGE" "$TMPDIR/$IMAGE_XZ"
success "Done."

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║   VM $VMID ($VMNAME) is ready to start!             ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Start the VM:"
echo "    qm start $VMID"
echo ""
echo "  Or start it from the Proxmox web UI — look for '$VMNAME' in the left panel."
echo ""
echo "  Once booted (30-60 seconds), Home Assistant will be at:"
echo "    http://<vm-ip>:8123"
echo ""
echo "  Find the VM's IP in Proxmox UI → $VMNAME → Summary → IPs"
echo "  Or on your router's DHCP table — it will show up as '$VMNAME'."
echo ""
warn "  Note: --onboot 0 is set. The VM will NOT auto-start on Proxmox reboot."
echo "  Set --onboot 1 once you're happy with the test instance:"
echo "    qm set $VMID --onboot 1"
echo ""
