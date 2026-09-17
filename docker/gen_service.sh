#!/bin/bash

# --- Configuration ---
OUTPUT_FILE="docker-compose.yaml"
AGENT_PORT=${PORT:-9099}
AGENT_INTERVAL=${INTERVAL:-60s}
AGENT_TOKEN=${TOKEN:-""}
# Generate a random 5-digit suffix (e.g., smart-agent-54321)
RANDOM_ID=$(shuf -i 10000-99999 -n 1)
AGENT_HOSTNAME="smart-agent-$RANDOM_ID"

echo "🔍 Scanning system for physical disks and required capabilities..."

# 1. Identify all physical disks
DISK_LIST=$(lsblk -dnio NAME,TYPE | grep 'disk')
DISK_DEVICES=$(echo "$DISK_LIST" | awk '{printf "      - \"/dev/%s:/dev/%s\"\n", $1, $1}')

if [ -z "$DISK_DEVICES" ]; then
    echo "❌ Error: No physical disks found!"
    exit 1
fi

# 2. Determine required capabilities based on disk types
CAP_ADD_BLOCK="    cap_add:"
HAS_SATA=false
HAS_NVME=false

if echo "$DISK_LIST" | grep -q '^sd'; then
    HAS_SATA=true
    CAP_ADD_BLOCK="${CAP_ADD_BLOCK}
	# Required for SATA/SAS SMART data
	- SYS_RAWIO"
fi

if echo "$DISK_LIST" | grep -q 'nvme'; then
    HAS_NVME=true
    CAP_ADD_BLOCK="${CAP_ADD_BLOCK}
	# Required for NVMe SMART data
	- SYS_ADMIN"
fi

# 3. Construct optional TOKEN environment variable
ENV_TOKEN_LINE=""
if [ -n "$AGENT_TOKEN" ]; then
    ENV_TOKEN_LINE="      - TOKEN=$AGENT_TOKEN"
fi

echo "📝 Generating $OUTPUT_FILE (Security: Capabilities mode)..."

# Generate YAML template
cat << YAML > $OUTPUT_FILE
services:
  smart-agent:
    build: .
    container_name: smart-agent
    hostname: $AGENT_HOSTNAME
${CAP_ADD_BLOCK}
    # required for mDNS auto discovery
    # or you can use macvlan with home assistant
    network_mode: host
    environment:
      # INTERVAL, TOKEN and PORT for service configuration
      - INTERVAL=$AGENT_INTERVAL
      - PORT=$AGENT_PORT
${ENV_TOKEN_LINE}
    devices:
$DISK_DEVICES
    restart: unless-stopped
YAML

# Clean up empty lines
sed -i '/^[[:space:]]*$/d' $OUTPUT_FILE

echo "✅ Generation complete!"
echo "--------------------------------"
echo "Detected: SATA=$HAS_SATA, NVMe=$HAS_NVME"
echo "--------------------------------"
cat $OUTPUT_FILE
echo "--------------------------------"
