package main

import (
	"flag"
	"fmt"
	"net"
	"os"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// Config holds all agent configuration. Values are resolved with this
// precedence: CLI flags > config file > defaults.
type Config struct {
	Port               int           `yaml:"port"`
	Token              string        `yaml:"token"`
	ScanInterval       time.Duration `yaml:"scan_interval"`
	MDNS               *bool         `yaml:"mdns"`                // pointer so we can detect "not set" vs "set to false"
	AdvertiseInterface string        `yaml:"advertise_interface"` // restrict mDNS to this interface (e.g. "eth0")
	MDNSName           string        `yaml:"mdns_name"`           // custom mDNS instance name (default: smartha-<hostname>)
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
var defaultSkipPrefixes = []string{
	"docker", // Docker bridge (docker0)
	"br-",    // Docker custom networks
	"veth",   // Docker/container veth pairs
	"zt",     // ZeroTier VPN
	"tailscale", "ts", // Tailscale VPN
	"wg",    // WireGuard VPN
	"virbr", // libvirt/KVM virtual bridge
	"vbox",  // VirtualBox host-only
	"vmnet", // VMware host-only
	"lo",    // Loopback
}

// LoadConfig reads configuration from config.yaml (if present) then overlays
// CLI flags. CLI flags always win.
func LoadConfig() (*Config, error) {
	cfg := defaultConfig()

	// --- Parse the --config flag first (before other flags) ---
	configPath := flag.String("config", "", "Path to config.yaml (default: auto-detect)")
	port := flag.Int("port", 0, "HTTP listen port (default 9099)")
	token := flag.String("token", "", "Bearer token for API auth (optional)")
	interval := flag.Duration("scan-interval", 0, "Drive rescan interval (e.g. 30s, 2m)")
	noMDNS := flag.Bool("no-mdns", false, "Disable mDNS/Zeroconf service advertisement")
	advIface := flag.String("interface", "", "Restrict mDNS advertisement to this network interface")
	mdnsName := flag.String("mdns-name", "", "Custom mDNS instance name (default: smartha-<hostname>)")
	flag.Parse()

	// --- Attempt to load config.yaml ---
	if *configPath != "" {
		// Explicit path — must exist.
		data, err := os.ReadFile(*configPath)
		if err != nil {
			return nil, fmt.Errorf("reading config file %s: %w", *configPath, err)
		}
		if err := yaml.Unmarshal(data, &cfg); err != nil {
			return nil, fmt.Errorf("parsing %s: %w", *configPath, err)
		}
	} else {
		// Auto-detect: working directory first, then system path.
		for _, path := range []string{"config.yaml", "/etc/smartha-agent/config.yaml"} {
			data, err := os.ReadFile(path)
			if err != nil {
				continue // file not found — that's fine
			}
			if err := yaml.Unmarshal(data, &cfg); err != nil {
				return nil, fmt.Errorf("parsing %s: %w", path, err)
			}
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
	if *interval != 0 {
		cfg.ScanInterval = *interval
	}
	if *noMDNS {
		f := false
		cfg.MDNS = &f
	}
	if *advIface != "" {
		cfg.AdvertiseInterface = *advIface
	}
	if *mdnsName != "" {
		cfg.MDNSName = *mdnsName
	}

	// Sanity checks
	if cfg.Port < 1 || cfg.Port > 65535 {
		return nil, fmt.Errorf("invalid port: %d", cfg.Port)
	}
	if cfg.ScanInterval < 5*time.Second {
		return nil, fmt.Errorf("scan_interval too short (minimum 5s): %v", cfg.ScanInterval)
	}

	return &cfg, nil
}

// MDNSEnabled returns true if mDNS advertisement is enabled (default: true).
func (c *Config) MDNSEnabled() bool {
	if c.MDNS == nil {
		return true // default on
	}
	return *c.MDNS
}

// ResolveAdvertiseInterfaces returns the list of net.Interface to pass to
// zeroconf.Register(). If advertise_interface is set, it returns just that
// interface. Otherwise, it filters out known virtual/VPN interfaces.
func (c *Config) ResolveAdvertiseInterfaces() ([]net.Interface, string) {
	// Explicit interface configured — use only that one.
	if c.AdvertiseInterface != "" {
		iface, err := net.InterfaceByName(c.AdvertiseInterface)
		if err != nil {
			return nil, fmt.Sprintf("WARNING: interface %q not found, advertising on all", c.AdvertiseInterface)
		}
		return []net.Interface{*iface}, fmt.Sprintf("interface %s", c.AdvertiseInterface)
	}

	// No explicit interface — auto-filter known virtual interfaces.
	allIfaces, err := net.Interfaces()
	if err != nil {
		return nil, "all interfaces (could not enumerate)"
	}

	var filtered []net.Interface
	var skipped []string
	for _, iface := range allIfaces {
		// Skip interfaces that are down.
		if iface.Flags&net.FlagUp == 0 {
			continue
		}
		// Skip loopback.
		if iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		// Skip known virtual/VPN prefixes.
		nameLower := strings.ToLower(iface.Name)
		skip := false
		for _, prefix := range defaultSkipPrefixes {
			if strings.HasPrefix(nameLower, prefix) {
				skip = true
				skipped = append(skipped, iface.Name)
				break
			}
		}
		if !skip {
			filtered = append(filtered, iface)
		}
	}

	if len(filtered) == 0 {
		// All interfaces were filtered — fall back to all.
		return nil, "all interfaces (auto-filter found none)"
	}

	desc := interfaceNames(filtered)
	if len(skipped) > 0 {
		desc += " (skipped: " + strings.Join(skipped, ", ") + ")"
	}
	return filtered, desc
}

// PreferredIP returns the best IP address from the given interfaces for
// inclusion in the mDNS TXT record. Prefers 192.168.x / 10.x over other
// ranges. Returns empty string if no suitable IP is found.
func PreferredIP(ifaces []net.Interface) string {
	// If no interface filter, enumerate all.
	if len(ifaces) == 0 {
		var err error
		ifaces, err = net.Interfaces()
		if err != nil {
			return ""
		}
	}

	type candidate struct {
		ip    string
		score int
	}
	var candidates []candidate

	for _, iface := range ifaces {
		// Skip known virtual interfaces.
		nameLower := strings.ToLower(iface.Name)
		isVirtual := false
		for _, prefix := range defaultSkipPrefixes {
			if strings.HasPrefix(nameLower, prefix) {
				isVirtual = true
				break
			}
		}

		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			var ip net.IP
			switch v := addr.(type) {
			case *net.IPNet:
				ip = v.IP
			case *net.IPAddr:
				ip = v.IP
			}
			if ip == nil || ip.IsLoopback() || ip.To4() == nil {
				continue // skip IPv6 and loopback
			}
			ipStr := ip.String()
			score := 80
			if isVirtual {
				score = 90
			} else if strings.HasPrefix(ipStr, "192.168.") || strings.HasPrefix(ipStr, "10.") {
				score = 10
			} else if strings.HasPrefix(ipStr, "172.") {
				score = 50
			} else if strings.HasPrefix(ipStr, "100.") {
				score = 70
			}
			candidates = append(candidates, candidate{ipStr, score})
		}
	}

	if len(candidates) == 0 {
		return ""
	}

	// Find the best (lowest score).
	best := candidates[0]
	for _, c := range candidates[1:] {
		if c.score < best.score {
			best = c
		}
	}
	return best.ip
}

// interfaceNames returns a comma-separated list of interface names.
func interfaceNames(ifaces []net.Interface) string {
	names := make([]string, len(ifaces))
	for i, iface := range ifaces {
		names[i] = iface.Name
	}
	return strings.Join(names, ", ")
}
