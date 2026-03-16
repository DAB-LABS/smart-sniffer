package main

import (
	"flag"
	"fmt"
	"os"
	"time"

	"gopkg.in/yaml.v3"
)

// Config holds all agent configuration. Values are resolved with this
// precedence: CLI flags > config file > defaults.
type Config struct {
	Port         int           `yaml:"port"`
	Token        string        `yaml:"token"`
	ScanInterval time.Duration `yaml:"scan_interval"`
}

// defaultConfig returns sane defaults.
func defaultConfig() Config {
	return Config{
		Port:         9099,
		ScanInterval: 60 * time.Second,
	}
}

// LoadConfig reads configuration from config.yaml (if present) then overlays
// CLI flags. CLI flags always win.
func LoadConfig() (*Config, error) {
	cfg := defaultConfig()

	// --- Attempt to load config.yaml ---
	// We look in the working directory first, then next to the binary.
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

	// --- CLI flags (override file values) ---
	port := flag.Int("port", 0, "HTTP listen port (default 9099)")
	token := flag.String("token", "", "Bearer token for API auth (optional)")
	interval := flag.Duration("scan-interval", 0, "Drive rescan interval (e.g. 30s, 2m)")
	flag.Parse()

	if *port != 0 {
		cfg.Port = *port
	}
	if *token != "" {
		cfg.Token = *token
	}
	if *interval != 0 {
		cfg.ScanInterval = *interval
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
