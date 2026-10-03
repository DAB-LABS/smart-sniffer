"""Constants for the SMART Sniffer integration."""

DOMAIN = "smart_sniffer"

# Config flow keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_TOKEN = "token"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_FORCE_UPDATE = "force_update"

# Defaults
DEFAULT_PORT = 9099
DEFAULT_SCAN_INTERVAL = 60  # seconds
DEFAULT_FORCE_UPDATE = False

# Agent version enforcement — bump MIN_AGENT_VERSION when a release requires
# agent-side changes.  The coordinator checks this every poll cycle and raises
# a HA repair notification when the running agent is older.
MIN_AGENT_VERSION = "0.4.28"
AGENT_RELEASES_URL = "https://github.com/DAB-LABS/smart-sniffer/releases"
# Supplied to the agent_outdated repair notice as a translation placeholder.
# Home Assistant's validator rejects a URL written into a translation string,
# so the text carries {install_url} and this is what fills it.
AGENT_INSTALL_URL = "https://raw.githubusercontent.com/DAB-LABS/smart-sniffer/main/install.sh"

# Key under hass.data[DOMAIN][entry_id] holding the agent device's registry
# id, which drive and filesystem devices point at with via_device_id.
AGENT_DEVICE_ID = "agent_device_id"

# Key used in coordinator data dict to store filesystem info.
# Underscore prefix avoids collision with drive ID keys.
FILESYSTEMS_KEY = "_filesystems"

# Key used in coordinator data dict to store ZFS pool status (GH #50): a list
# of pool dicts from the agent's /api/pools, [] when the agent does not
# advertise pools, None when it does but the fetch failed.
POOLS_KEY = "_pools"

# Names of the registered ZFS pools the agent's last pool list left out
# (exported, or failed to import): a sorted list, [] when none or when the
# agent does not advertise pools, None when the pool fetch failed.
POOLS_MISSING_KEY = "_pools_missing"

# Service name for the get_drive_data action.
SERVICE_GET_DRIVE_DATA = "get_drive_data"

# Device Statistics (v0.8.0): one Store per config entry, holding the readings
# kept across polls and which drives have been announced (D11). The key is
# STORE_KEY_PREFIX + "." + entry_id; removed with the entry. A changed hold is
# written at most once per STORE_SAVE_DELAY seconds; HA flushes it at stop.
STORE_KEY_PREFIX = "smart_sniffer.devstat"
STORE_VERSION = 1
STORE_SAVE_DELAY = 600
