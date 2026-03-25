"""Constants for the SMART Sniffer integration."""

DOMAIN = "smart_sniffer"

# Config flow keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_TOKEN = "token"
CONF_SCAN_INTERVAL = "scan_interval"

# Defaults
DEFAULT_PORT = 9099
DEFAULT_SCAN_INTERVAL = 60  # seconds

# Agent version enforcement — bump MIN_AGENT_VERSION when a release requires
# agent-side changes.  The coordinator checks this every poll cycle and raises
# a HA repair notification when the running agent is older.
MIN_AGENT_VERSION = "0.4.28"
AGENT_RELEASES_URL = "https://github.com/DAB-LABS/smart-sniffer/releases"
