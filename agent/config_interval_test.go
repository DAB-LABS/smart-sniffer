package main

import (
	"encoding/json"
	"flag"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// loadWith runs LoadConfig on a config file with these contents and these
// extra command-line arguments, as the agent does at startup.
func loadWith(t *testing.T, yaml string, args ...string) (*Config, error) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(yaml), 0o644); err != nil {
		t.Fatal(err)
	}
	savedArgs, savedFlags := os.Args, flag.CommandLine
	t.Cleanup(func() { os.Args, flag.CommandLine = savedArgs, savedFlags })
	flag.CommandLine = flag.NewFlagSet("smartha-agent", flag.ContinueOnError)
	flag.CommandLine.SetOutput(new(strings.Builder))
	os.Args = append([]string{"smartha-agent", "--config", path}, args...)
	return LoadConfig()
}

func TestParseScanInterval(t *testing.T) {
	good := map[string]time.Duration{
		"3600":   time.Hour,
		"60":     time.Minute,
		" 90 ":   90 * time.Second,
		"5":      5 * time.Second,
		"60s":    time.Minute,
		"5m":     5 * time.Minute,
		"1h30m":  90 * time.Minute,
		"24h":    24 * time.Hour,
		"1.5m":   90 * time.Second,
		"000120": 2 * time.Minute,
	}
	for in, want := range good {
		got, err := parseScanInterval(in)
		if err != nil || got != want {
			t.Errorf("parseScanInterval(%q) = %v, %v; want %v", in, got, err, want)
		}
	}
	for _, in := range []string{"", "  ", "abc", "60x", "-60", "1e3", "60 s", "99999999999999999999", "18446744073709551615"} {
		if _, err := parseScanInterval(in); err == nil {
			t.Errorf("parseScanInterval(%q) accepted", in)
		}
	}
}

func TestScanIntervalInConfigFile(t *testing.T) {
	cases := map[string]time.Duration{
		"scan_interval: 3600\n":     time.Hour,
		"scan_interval: \"3600\"\n": time.Hour,
		"scan_interval: '120'\n":    2 * time.Minute,
		"scan_interval: 60s\n":      time.Minute,
		"scan_interval: 30m\n":      30 * time.Minute,
		"scan_interval: \"5m\"\n":   5 * time.Minute,
		"port: 9099\n":              60 * time.Second, // unset: the default
	}
	for yaml, want := range cases {
		cfg, err := loadWith(t, yaml)
		if err != nil {
			t.Errorf("%q: %v", yaml, err)
			continue
		}
		if cfg.ScanInterval != want {
			t.Errorf("%q: ScanInterval = %v, want %v", yaml, cfg.ScanInterval, want)
		}
	}
}

func TestScanIntervalInConfigFileStillRejectsBadValues(t *testing.T) {
	cases := map[string]string{
		"scan_interval: abc\n":             `invalid scan_interval "abc"`,
		"scan_interval: -5\n":              `invalid scan_interval "-5"`,
		"scan_interval:\n":                 "scan_interval must be a number of seconds",
		"scan_interval: [60]\n":            "scan_interval must be a number of seconds",
		"scan_interval: 2\n":               "scan_interval too short (minimum 5s)",
		"scan_interval: 2s\n":              "scan_interval too short (minimum 5s)",
		"scan_interval: 0\n":               "scan_interval too short (minimum 5s)",
		"port: 9099\nscan_interval: 1.5\n": `invalid scan_interval "1.5"`,
	}
	for yaml, want := range cases {
		_, err := loadWith(t, yaml)
		if err == nil {
			t.Errorf("%q: accepted", yaml)
			continue
		}
		if !strings.Contains(err.Error(), want) {
			t.Errorf("%q: error %q does not say %q", yaml, err, want)
		}
	}
}

func TestScanIntervalFlag(t *testing.T) {
	for arg, want := range map[string]time.Duration{"3600": time.Hour, "45": 45 * time.Second, "2m": 2 * time.Minute} {
		cfg, err := loadWith(t, "scan_interval: 60s\n", "--scan-interval", arg)
		if err != nil {
			t.Errorf("--scan-interval %s: %v", arg, err)
			continue
		}
		if cfg.ScanInterval != want {
			t.Errorf("--scan-interval %s: %v, want %v", arg, cfg.ScanInterval, want)
		}
	}
	// A bad value: the flag refuses it, and the agent's flag set (ExitOnError)
	// prints that and stops.
	var f scanIntervalFlag
	if err := f.Set("soon"); err == nil || !strings.Contains(err.Error(), `invalid scan_interval "soon"`) {
		t.Errorf("--scan-interval soon: err = %v", err)
	}
	fs := flag.NewFlagSet("smartha-agent", flag.ContinueOnError)
	fs.SetOutput(new(strings.Builder))
	fs.Var(&f, "scan-interval", "")
	if err := fs.Parse([]string{"--scan-interval", "soon"}); err == nil || !strings.Contains(err.Error(), "invalid scan_interval") {
		t.Errorf("parse --scan-interval soon: err = %v", err)
	}
	// Without the flag the file's value stands.
	cfg, err := loadWith(t, "scan_interval: 300\n")
	if err != nil || cfg.ScanInterval != 5*time.Minute {
		t.Errorf("file only: %v, %v", cfg, err)
	}
}

// The other keys still decode with scan_interval read on its own.
func TestConfigFileOtherKeysUnchanged(t *testing.T) {
	cfg, err := loadWith(t, "port: 9100\nscan_interval: 120\nstandby_mode: standby\ndevice_overrides:\n  - device: /dev/sata1\n    protocol: sat\n")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Port != 9100 || cfg.StandbyMode != "standby" || len(cfg.DeviceOverrides) != 1 || cfg.ScanInterval != 2*time.Minute {
		t.Errorf("cfg = %+v", cfg)
	}
}

func healthBody(t *testing.T, cfg *Config) map[string]any {
	t.Helper()
	rec := httptest.NewRecorder()
	handleHealth(NewDriveCache(cfg))(rec, httptest.NewRequest("GET", "/api/health", nil))
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	return body
}

// v1.0: api_version is the integer 1, and scan_interval is the interval in
// use in whole seconds.
func TestHealthAPIVersionAndScanInterval(t *testing.T) {
	body := healthBody(t, &Config{ScanInterval: time.Hour})
	if v, ok := body["api_version"].(float64); !ok || v != 1 {
		t.Errorf("api_version = %#v, want 1", body["api_version"])
	}
	if v, ok := body["scan_interval"].(float64); !ok || v != 3600 {
		t.Errorf("scan_interval = %#v, want 3600", body["scan_interval"])
	}
	if got := healthBody(t, &Config{ScanInterval: 90 * time.Second})["scan_interval"]; got != float64(90) {
		t.Errorf("scan_interval for 90s = %#v", got)
	}
	// Whole seconds: 1.5m is 90, 7.6s rounds to 8.
	if got := healthBody(t, &Config{ScanInterval: 7600 * time.Millisecond})["scan_interval"]; got != float64(8) {
		t.Errorf("scan_interval for 7.6s = %#v", got)
	}
	// Integers in the JSON text itself, not 3600.0.
	rec := httptest.NewRecorder()
	handleHealth(NewDriveCache(&Config{ScanInterval: time.Hour}))(rec, httptest.NewRequest("GET", "/api/health", nil))
	for _, want := range []string{`"api_version":1,`, `"scan_interval":3600,`} {
		if !strings.Contains(rec.Body.String(), want) {
			t.Errorf("body %s lacks %s", rec.Body.String(), want)
		}
	}
}
