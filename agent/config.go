package main

import (
	"flag"
	"fmt"
	"log"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// FilesystemConfig describes a single mountpoint to monitor for disk usage.
// Populated by the installer's Disk Usage picker and written to config.yaml.
type FilesystemConfig struct {
	Path   string `yaml:"path"`
	UUID   string `yaml:"uuid"`
	FSType string `yaml:"fstype"`
	Device string `yaml:"device"`
}

// DeviceOverride allows manual protocol specification for drives that
// auto-detection cannot handle (Synology paths, RAID controllers, etc.).
type DeviceOverride struct {
	Device   string `yaml:"device"`
	Protocol string `yaml:"protocol"`
}

// Config holds all agent configuration. Values are resolved with this
// precedence: CLI flags > config file > defaults.
type Config struct {
	Port               int                `yaml:"port"`
	Token              string             `yaml:"token"`
	ScanInterval       time.Duration      `yaml:"-"`                   // read by decodeConfigFile: a duration (60s, 5m) or a bare number of seconds (3600)
	MDNS               *bool              `yaml:"mdns"`                // pointer so we can detect "not set" vs "set to false"
	AdvertiseInterface string             `yaml:"advertise_interface"` // restrict mDNS to this interface (e.g. "eth0")
	MDNSName           string             `yaml:"mdns_name"`           // custom mDNS instance name (default: smartha-<hostname>)
	Filesystems        []FilesystemConfig `yaml:"filesystems"`         // empty = disk usage monitoring disabled
	MountPrefix        string             `yaml:"mount_prefix"`        // host mount root when the agent runs in a container (e.g. "/host"); env SMARTHA_MOUNT_PREFIX overrides
	StandbyMode        string             `yaml:"standby_mode"`        // never, standby, sleep, idle (default: never)
	Verbose            bool               `yaml:"verbose"`             // log every poll cycle instead of suppressing repeats; --verbose enables (never disables a file setting)
	DeviceOverrides    []DeviceOverride   `yaml:"device_overrides"`    // manual protocol overrides per device path
	ExcludeDevices     []string           `yaml:"exclude_devices"`     // device paths to skip during scan
	ZFSPoolStatus      *bool              `yaml:"zfs_pool_status"`     // report ZFS pool health when zpool is present (default: true); pointer to tell "not set" from false
	ZpoolPath          string             `yaml:"zpool_path"`          // zpool binary to run; empty = PATH, then the usual sbin locations
	DeviceStatistics   *bool              `yaml:"device_statistics"`   // read ATA Device Statistics with a second smartctl call per poll (default: true); pointer to tell "not set" from false
	Discover           bool               `yaml:"-"`                   // set by --discover flag; not read from config file
	NoWrite            bool               `yaml:"-"`                   // set by --no-write flag; skips config write in discover mode
	AssumeYes          bool               `yaml:"-"`                   // set by --yes / -y; with --discover, answers yes to its questions without reading stdin
	SmartctlPathConfig string             `yaml:"smartctl_path"`       // smartctl binary from config; empty = PATH, then the usual install locations
	SmartctlPath       string             `yaml:"-"`                   // resolved path to smartctl binary; set by resolveSmartctlPath()
	AgentID            string             `yaml:"agent_id"`            // pins the agent ID; empty = random ID saved in agent_id.json
	ConfigPath         string             `yaml:"-"`                   // absolute path of the config file actually read; "" when none
	excludeSet         map[string]bool    // normalized set built once at load time (unexported, not serialized)
}

// defaultConfig returns sane defaults.
func defaultConfig() Config {
	return Config{
		Port:         9099,
		ScanInterval: 60 * time.Second,
	}
}

// defaultSkipPrefixes are interface name prefixes that are skipped when no
// explicit advertise_interface is configured. These are almost never the
// real LAN interface and cause duplicate/unreachable mDNS discoveries.
//
// Intentionally EXCLUDED (ambiguous -- can be primary LAN on some platforms):
//
//	bond*  -- Synology bonded NICs, NAS LACP
//	br     -- (bare, no dash) Unraid br0 is the host bridge
//	vlan*  -- may carry the only routable IP
//	qvs*   -- QNAP virtual switch AND primary management interface
//	qbr*   -- QNAP/OpenStack, ambiguous
//	qvo*   -- QNAP/OpenStack, ambiguous
//	qvb*   -- QNAP/OpenStack, ambiguous
//
// See docs/internal/research/mdns-interface-prefixes.md for full rationale.
var defaultSkipPrefixes = []string{
	// --- Loopback ---
	"lo", // Loopback

	// --- Container / Docker ---
	"docker",          // Docker bridge (docker0)
	"docker_gwbridge", // Docker Swarm gateway bridge
	"br-",             // Docker custom networks (br-<hash>)
	"veth",            // Docker/container veth pairs
	"podman",          // Podman container bridge
	"hassio",          // Home Assistant OS supervisor bridge

	// --- LXC / LXD ---
	"lxcbr", // LXC container bridge (caused #19 on QNAP)
	"lxdbr", // LXD container bridge

	// --- Kubernetes CNI ---
	"flannel", // Flannel overlay (default on TrueNAS SCALE K3s)
	"cni",     // Generic CNI bridge
	"calico",  // Calico overlay
	"cali",    // Calico veth pairs (cali12345)
	"cilium_", // Cilium overlay (cilium_host, cilium_net, cilium_vxlan)
	"weave",   // Weave Net overlay
	"crc",     // CodeReady Containers (OpenShift local)

	// --- VPN / Tunnel ---
	"zt",        // ZeroTier
	"tailscale", // Tailscale (long form)
	"ts",        // Tailscale (short form)
	"wg",        // WireGuard
	"tun",       // OpenVPN / generic tunnel
	"tap",       // Generic TAP
	"utun",      // macOS userspace tunnel
	"ipsec",     // IPsec tunnel
	"gre",       // GRE tunnel
	"geneve",    // Geneve encapsulation
	"vxlan",     // VXLAN overlay
	"erspan",    // Encapsulated Remote SPAN

	// --- Hypervisor / VM ---
	"virbr", // libvirt/KVM virtual bridge
	"vbox",  // VirtualBox host-only
	"vmnet", // VMware host-only
	"vmbr",  // Proxmox virtual bridge
	"xenbr", // Xen hypervisor bridge
	"qemu",  // QEMU virtual NIC
	"vmk",   // VMware ESXi VMkernel
	"hv_",   // Hyper-V virtual interfaces
	"fwbr",  // Proxmox firewall bridge
	"fwpr",  // Proxmox firewall proxy
	"fwln",  // Proxmox firewall link

	// --- macOS virtual ---
	"ap",     // Apple access point (ap1)
	"awdl",   // Apple Wireless Direct Link (AirDrop)
	"llw",    // Low Latency WLAN (Apple)
	"bridge", // macOS VM bridging (bridge0)
	"gif",    // macOS/BSD generic tunnel
	"stf",    // macOS/BSD 6to4 tunnel
	"anpi",   // Apple Network Privacy Interface
	"qlf",    // Apple internal (qlf0)

	// --- Windows virtual ---
	"vethernet", // Hyper-V virtual Ethernet
	"isatap",    // ISATAP tunnel adapter

	// --- Linux misc ---
	"dummy", // Dummy interfaces (dummy0)
	"ifb",   // Intermediate Functional Block
	"ovs",   // Open vSwitch
	"ham",   // FreeBSD HAST mirror
	"epair", // FreeBSD jail virtual pair
	"vnet",  // FreeBSD bhyve/jail virtual NIC
}

// LoadConfig reads configuration from config.yaml (if present) then overlays
// CLI flags. CLI flags always win.
func LoadConfig() (*Config, error) {
	cfg := defaultConfig()

	// --- Parse the --config flag first (before other flags) ---
	configPath := flag.String("config", "", "Path to config.yaml (default: auto-detect)")
	port := flag.Int("port", 0, "HTTP listen port (default 9099)")
	token := flag.String("token", "", "Bearer token for API auth (optional)")
	var interval scanIntervalFlag
	flag.Var(&interval, "scan-interval", "Drive rescan interval: a duration (30s, 5m, 1h) or a number of seconds (3600)")
	noMDNS := flag.Bool("no-mdns", false, "Disable mDNS/Zeroconf service advertisement")
	advIface := flag.String("interface", "", "Restrict mDNS advertisement to this network interface")
	mdnsName := flag.String("mdns-name", "", "Custom mDNS instance name (default: smartha-<hostname>)")
	discover := flag.Bool("discover", false, "Probe drives and detect protocols (diagnostic tool)")
	noWrite := flag.Bool("no-write", false, "With --discover: print proposed overrides but do not write config")
	assumeYes := flag.Bool("yes", false, "With --discover: write without asking, replacing any device_overrides already in config.yaml")
	flag.BoolVar(assumeYes, "y", false, "Short for --yes")
	verbose := flag.Bool("verbose", false, "Log every poll cycle instead of suppressing repeated lines")
	noDevstat := flag.Bool("no-device-statistics", false, "Do not read ATA Device Statistics (one extra smartctl call per SATA drive per poll)")
	flag.Parse()

	// --- Attempt to load config.yaml ---
	if *configPath != "" {
		// Explicit path — must exist.
		data, err := os.ReadFile(*configPath)
		if err != nil {
			return nil, fmt.Errorf("reading config file %s: %w", *configPath, err)
		}
		if err := decodeConfigFile(data, &cfg); err != nil {
			return nil, fmt.Errorf("parsing %s: %w", *configPath, err)
		}
		cfg.ConfigPath = absPath(*configPath)
	} else {
		// Auto-detect: working directory first, then system path.
		for _, path := range []string{"config.yaml", "/etc/smartha-agent/config.yaml"} {
			data, err := os.ReadFile(path)
			if err != nil {
				continue // file not found — that's fine
			}
			if err := decodeConfigFile(data, &cfg); err != nil {
				return nil, fmt.Errorf("parsing %s: %w", path, err)
			}
			cfg.ConfigPath = absPath(path)
			break
		}
	}

	// --- CLI flags (override file values) ---
	if *port != 0 {
		cfg.Port = *port
	}
	if *token != "" {
		cfg.Token = *token
	}
	if interval.set {
		cfg.ScanInterval = interval.d
	}
	if *noMDNS {
		f := false
		cfg.MDNS = &f
	}
	if *noDevstat {
		f := false
		cfg.DeviceStatistics = &f
	}
	if *advIface != "" {
		cfg.AdvertiseInterface = *advIface
	}
	if *mdnsName != "" {
		cfg.MDNSName = *mdnsName
	}
	if *discover {
		cfg.Discover = true
	}
	if *noWrite {
		cfg.NoWrite = true
	}
	if *assumeYes {
		cfg.AssumeYes = true
	}
	// --verbose is enable-only: the flag can turn verbose logging on, but its
	// absence must not clobber a "verbose: true" config-file setting back to
	// false. A plain bool flag cannot distinguish "unset" from "set false", so
	// we only ever OR it in.
	if *verbose {
		cfg.Verbose = true
	}

	// Environment override for the container mount prefix. Takes precedence
	// over the config file so containerized deployments can set it without
	// editing config.yaml. Used by container-aware filesystem reporting.
	if v := os.Getenv("SMARTHA_MOUNT_PREFIX"); v != "" {
		cfg.MountPrefix = v
	}

	// Sanity checks
	if cfg.Port < 1 || cfg.Port > 65535 {
		return nil, fmt.Errorf("invalid port: %d", cfg.Port)
	}
	if cfg.ScanInterval < 5*time.Second {
		return nil, fmt.Errorf("scan_interval too short (minimum 5s): %v", cfg.ScanInterval)
	}

	if cfg.AgentID != "" {
		id, ok := normAgentID(cfg.AgentID)
		if !ok {
			return nil, fmt.Errorf("invalid agent_id %q (use 8 to 64 characters: a-z, 0-9 and -, starting with a letter or digit)", cfg.AgentID)
		}
		cfg.AgentID = id
	}

	// Standby mode validation -- default to "never" if not set.
	if cfg.StandbyMode == "" {
		cfg.StandbyMode = "never"
	}
	validStandbyModes := map[string]bool{
		"never": true, "standby": true, "sleep": true, "idle": true,
	}
	if !validStandbyModes[cfg.StandbyMode] {
		return nil, fmt.Errorf("invalid standby_mode %q (must be never, standby, sleep, or idle)", cfg.StandbyMode)
	}

	// Validate device_overrides
	for i, ov := range cfg.DeviceOverrides {
		if ov.Device == "" {
			return nil, fmt.Errorf("device_overrides[%d]: device path is required", i)
		}
		if ov.Protocol == "" {
			return nil, fmt.Errorf("device_overrides[%d]: protocol is required for %s", i, ov.Device)
		}
	}

	// Validate and normalize exclude_devices.
	// Build a resolved set once so Refresh() does a simple map lookup per poll.
	cfg.excludeSet = make(map[string]bool, len(cfg.ExcludeDevices))
	seen := make(map[string]bool, len(cfg.ExcludeDevices))
	for i, raw := range cfg.ExcludeDevices {
		if !strings.HasPrefix(raw, "/dev/") && !strings.HasPrefix(raw, `\\.\`) {
			return nil, fmt.Errorf("exclude_devices[%d]: %q is not a valid device path", i, raw)
		}
		if seen[raw] {
			log.Printf("WARNING: duplicate entry in exclude_devices: %s", raw)
		}
		seen[raw] = true

		// Resolve symlinks so /dev/disk/by-id/... and /dev/sdX both match.
		resolved, err := filepath.EvalSymlinks(raw)
		if err != nil {
			log.Printf("WARNING: excluded device %s not found: %v (will still exclude if it appears later)", raw, err)
			cfg.excludeSet[raw] = true
			continue
		}
		cfg.excludeSet[resolved] = true
		if resolved != raw {
			cfg.excludeSet[raw] = true // match on either form
		}
	}

	// Warn on exclude + override conflicts.
	for _, ov := range cfg.DeviceOverrides {
		if cfg.excludeSet[ov.Device] {
			log.Printf("WARNING: %s is in both exclude_devices and device_overrides; excluding", ov.Device)
		}
	}

	return &cfg, nil
}

// decodeConfigFile reads config.yaml into cfg. scan_interval is read on its
// own (scanIntervalYAML) so that a bare number of seconds is accepted as well
// as a duration with a unit; every other key is decoded as before.
func decodeConfigFile(data []byte, cfg *Config) error {
	if err := yaml.Unmarshal(data, cfg); err != nil {
		return err
	}
	// A yaml.Node, not a pointer: an empty "scan_interval:" is an error, as
	// it was before, rather than quietly the default.
	var raw struct {
		ScanInterval yaml.Node `yaml:"scan_interval"`
	}
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return err
	}
	if raw.ScanInterval.Kind == 0 {
		return nil // not in the file: the default stands
	}
	var v scanIntervalYAML
	if err := v.UnmarshalYAML(&raw.ScanInterval); err != nil {
		return err
	}
	cfg.ScanInterval = time.Duration(v)
	return nil
}

// parseScanInterval reads a scan interval as written in config.yaml or on
// the command line: a Go duration with a unit ("30s", "5m", "1h30m") or a
// bare whole number of seconds ("3600"). Since v1.0; before that a bare
// number stopped the agent at startup.
func parseScanInterval(s string) (time.Duration, error) {
	v := strings.TrimSpace(s)
	if v == "" {
		return 0, fmt.Errorf("scan_interval is empty (use a number of seconds such as 3600, or a duration such as 30s, 5m or 1h)")
	}
	if n, err := strconv.ParseUint(v, 10, 64); err == nil {
		if n > uint64(math.MaxInt64/int64(time.Second)) {
			return 0, fmt.Errorf("invalid scan_interval %q: too long", s)
		}
		return time.Duration(n) * time.Second, nil
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return 0, fmt.Errorf("invalid scan_interval %q (use a number of seconds such as 3600, or a duration such as 30s, 5m or 1h)", s)
	}
	return d, nil
}

// scanIntervalYAML is scan_interval as read from config.yaml: 3600, "3600"
// and 60s all decode.
type scanIntervalYAML time.Duration

func (v *scanIntervalYAML) UnmarshalYAML(node *yaml.Node) error {
	if node.Kind != yaml.ScalarNode || node.Tag == "!!null" {
		return fmt.Errorf("line %d: scan_interval must be a number of seconds such as 3600, or a duration such as 30s, 5m or 1h", node.Line)
	}
	d, err := parseScanInterval(node.Value)
	if err != nil {
		return fmt.Errorf("line %d: %w", node.Line, err)
	}
	*v = scanIntervalYAML(d)
	return nil
}

// scanIntervalFlag is --scan-interval, read with the same rules as the file.
type scanIntervalFlag struct {
	d   time.Duration
	set bool
}

func (f *scanIntervalFlag) String() string {
	if f == nil || !f.set {
		return ""
	}
	return f.d.String()
}

func (f *scanIntervalFlag) Set(s string) error {
	d, err := parseScanInterval(s)
	if err != nil {
		return err
	}
	f.d, f.set = d, true
	return nil
}

// absPath returns p made absolute, or p unchanged if that fails.
func absPath(p string) string {
	if a, err := filepath.Abs(p); err == nil {
		return a
	}
	return p
}

// IsDeviceExcluded returns true if the given device path (or its symlink
// target) is in the exclude_devices set. Safe to call with a nil Config.
func (c *Config) IsDeviceExcluded(devPath string) bool {
	if c == nil || len(c.excludeSet) == 0 {
		return false
	}
	if c.excludeSet[devPath] {
		return true
	}
	// Resolve the scanned path in case it's a symlink not in the raw set.
	if resolved, err := filepath.EvalSymlinks(devPath); err == nil && resolved != devPath {
		return c.excludeSet[resolved]
	}
	return false
}

// MDNSEnabled returns true if mDNS advertisement is enabled (default: true).
func (c *Config) MDNSEnabled() bool {
	if c.MDNS == nil {
		return true // default on
	}
	return *c.MDNS
}

// PoolStatusEnabled returns true unless zfs_pool_status is set to false.
// Pool status still needs a zpool binary; see NewPoolCacheFromConfig.
func (c *Config) PoolStatusEnabled() bool {
	if c.ZFSPoolStatus == nil {
		return true // default on
	}
	return *c.ZFSPoolStatus
}

// DeviceStatisticsEnabled returns true unless device_statistics is set to
// false (or --no-device-statistics is given). macOS never reads them; see
// the skip table in devstat.go.
func (c *Config) DeviceStatisticsEnabled() bool {
	if c.DeviceStatistics == nil {
		return true // default on
	}
	return *c.DeviceStatistics
}
