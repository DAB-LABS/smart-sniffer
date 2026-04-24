package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"

	"gopkg.in/yaml.v3"
)

// discoverDriveResult holds the outcome of probing a single drive in discover mode.
type discoverDriveResult struct {
	path        string
	scanProto   string // protocol reported by --scan-open
	smartOK     bool   // SMART data readable with scan protocol
	satRetried  bool   // SAT fallback was attempted
	satOK       bool   // SAT fallback succeeded
	model       string
	serial      string
	needsConfig bool // requires a device_override entry
}

// RunDiscover probes all drives, reports protocols and SMART accessibility, and
// optionally writes device_overrides to config.yaml. Called when --discover is set.
func RunDiscover(cfg *Config, noWrite bool) error {
	fmt.Println("SMART Sniffer -- Drive Discovery")
	fmt.Println()
	fmt.Println("Scanning drives...")

	// Always use --scan-open in discover mode for best protocol detection.
	scanOut, err := exec.Command("smartctl", "--json", "--scan-open").CombinedOutput()
	if err != nil {
		// Fall back to --scan if --scan-open is unsupported.
		scanOut, err = exec.Command("smartctl", "--json", "--scan").CombinedOutput()
		if err != nil {
			return fmt.Errorf("smartctl --scan failed: %v", err)
		}
	}

	var scanResult struct {
		Devices []struct {
			Name     string `json:"name"`
			Protocol string `json:"protocol"`
		} `json:"devices"`
	}
	if err := json.Unmarshal(scanOut, &scanResult); err != nil {
		return fmt.Errorf("failed to parse scan output: %v", err)
	}

	var results []discoverDriveResult

	if len(scanResult.Devices) == 0 {
		fmt.Printf("  Standard scan found 0 drives.\n")
	} else {
		for _, dev := range scanResult.Devices {
			r := probeOneDrive(dev.Name, dev.Protocol)
			results = append(results, r)
			printDriveResult(r)
		}
	}

	// Platform detection.
	platform := detectPlatform()

	if platform == "synology" {
		fmt.Println()
		fmt.Println("Detected Synology platform. Probing /dev/sata paths...")
		fmt.Println()

		for i := 1; i <= 8; i++ {
			path := fmt.Sprintf("/dev/sata%d", i)
			// Skip if already found by scan.
			alreadyFound := false
			for _, r := range results {
				if r.path == path {
					alreadyFound = true
					break
				}
			}
			if alreadyFound {
				continue
			}

			// Check if the path exists before probing.
			if _, statErr := os.Stat(path); os.IsNotExist(statErr) {
				fmt.Printf("  %s -- not present\n", path)
				continue
			}

			r := probeOneDrive(path, "sat")
			results = append(results, r)
			printDriveResult(r)
		}
	} else if platform == "qnap" {
		fmt.Println()
		fmt.Println("Detected QNAP platform.")
	}

	// Summary.
	fmt.Println()
	total := len(results)
	readable := 0
	var needsOverride []discoverDriveResult
	for _, r := range results {
		if r.smartOK || r.satOK {
			readable++
		}
		if r.needsConfig {
			needsOverride = append(needsOverride, r)
		}
	}

	if total == 0 {
		fmt.Println("No drives found. Verify smartmontools is installed and the agent has permission to access drives.")
		return nil
	}

	fmt.Printf("Found %d drive(s). %d readable.\n", total, readable)

	if len(needsOverride) == 0 {
		if platform == "qnap" {
			fmt.Println("No config changes needed -- the agent handles protocol detection automatically.")
		} else {
			fmt.Println("No config changes needed.")
		}
		return nil
	}

	// Drives need device_overrides.
	fmt.Printf("%d drive(s) need device_overrides in your config.\n", len(needsOverride))
	fmt.Println()
	fmt.Println("Proposed additions to config.yaml:")
	fmt.Println()
	fmt.Println("  device_overrides:")
	for _, r := range needsOverride {
		proto := "sat"
		if r.satOK {
			proto = "sat"
		} else if r.scanProto != "" {
			proto = strings.ToLower(r.scanProto)
		}
		fmt.Printf("    - device: %s\n", r.path)
		fmt.Printf("      protocol: %s\n", proto)
	}

	if noWrite {
		fmt.Println()
		fmt.Println("(--no-write: config not modified)")
		return nil
	}

	// Find the config file path.
	configPath := resolveConfigPath(cfg)
	if configPath == "" {
		fmt.Println()
		fmt.Println("No config.yaml found. Create one and add the device_overrides above.")
		return nil
	}

	fmt.Println()
	fmt.Printf("Write to %s? [Y/n]: ", configPath)

	scanner := bufio.NewScanner(os.Stdin)
	scanner.Scan()
	answer := strings.TrimSpace(scanner.Text())
	if answer != "" && !strings.EqualFold(answer, "y") {
		fmt.Println("Skipped. Add the device_overrides above to your config manually.")
		return nil
	}

	return writeDeviceOverrides(configPath, needsOverride)
}

// probeOneDrive attempts to read SMART data from a single drive path, trying
// SAT fallback if the initial protocol fails. Returns a discoverDriveResult.
func probeOneDrive(path, protocol string) discoverDriveResult {
	r := discoverDriveResult{path: path, scanProto: protocol}

	args := []string{"--json", "-a"}
	if strings.EqualFold(protocol, "sat") {
		args = append(args, "-d", "sat")
	}
	args = append(args, path)

	out, err := exec.Command("smartctl", args...).CombinedOutput()
	code := 0
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			code = exitErr.ExitCode()
		}
	}

	// Execution failure bits (0-2). Bit 1 = device open failed.
	openFailed := code&0x02 != 0

	if !openFailed {
		r.smartOK = true
		r.model, r.serial = extractSmartModelSerial(out)
		// Synology paths always need a device_override (not found by regular scan).
		if strings.HasPrefix(path, "/dev/sata") {
			r.needsConfig = true
		}
		return r
	}

	// SCSI open failed -- try SAT.
	if strings.EqualFold(protocol, "scsi") || strings.EqualFold(protocol, "sat") {
		r.satRetried = true
		satArgs := []string{"--json", "-a", "-d", "sat", path}
		satOut, satErr := exec.Command("smartctl", satArgs...).CombinedOutput()
		satCode := 0
		if satErr != nil {
			if satExitErr, ok := satErr.(*exec.ExitError); ok {
				satCode = satExitErr.ExitCode()
			}
		}
		if satCode&0x02 == 0 {
			r.satOK = true
			r.model, r.serial = extractSmartModelSerial(satOut)
			// SAT auto-fallback handled at runtime -- only needs config if it won't
			// be reached via scan (i.e. path not found by --scan-open).
			return r
		}
	}

	return r
}

// printDriveResult prints a single drive's discover result to stdout.
func printDriveResult(r discoverDriveResult) {
	fmt.Printf("\n  %s\n", r.path)

	if r.satRetried {
		fmt.Printf("    Scan protocol: %s\n", r.scanProto)
		fmt.Printf("    SMART data:    No\n")
		if r.satOK {
			fmt.Printf("    SAT retry:     Yes -- SMART data available\n")
			if r.model != "" {
				fmt.Printf("    Model:         %s\n", r.model)
			}
			if r.serial != "" {
				fmt.Printf("    Serial:        %s\n", r.serial)
			}
			fmt.Printf("    Result:        OK (agent will auto-detect SAT at runtime)\n")
		} else {
			fmt.Printf("    SAT retry:     No -- drive not readable\n")
			fmt.Printf("    Result:        WARNING: could not read SMART data\n")
		}
	} else if r.smartOK {
		proto := r.scanProto
		if proto == "" {
			proto = "SAT"
		}
		fmt.Printf("    Protocol:   %s\n", proto)
		fmt.Printf("    SMART data: Yes\n")
		if r.model != "" {
			fmt.Printf("    Model:      %s\n", r.model)
		}
		if r.serial != "" {
			fmt.Printf("    Serial:     %s\n", r.serial)
		}
		if r.needsConfig {
			fmt.Printf("    Result:     Needs device_override (not found by standard scan)\n")
		} else {
			fmt.Printf("    Result:     OK\n")
		}
	} else {
		fmt.Printf("    Protocol:   %s\n", r.scanProto)
		fmt.Printf("    SMART data: No\n")
		fmt.Printf("    Result:     WARNING: could not read SMART data\n")
	}
}

// detectPlatform returns "synology", "qnap", or "" for standard Linux/other.
func detectPlatform() string {
	// Synology: synoinfo.conf is the canonical marker.
	if _, err := os.Stat("/etc/synoinfo.conf"); err == nil {
		return "synology"
	}
	// Some Synology units may lack synoinfo.conf but have /dev/sata1.
	if _, err := os.Stat("/dev/sata1"); err == nil {
		return "synology"
	}
	// QNAP.
	if _, err := os.Stat("/etc/config/qpkg.conf"); err == nil {
		return "qnap"
	}
	if _, err := os.Stat("/sbin/get_hd_smartinfo"); err == nil {
		return "qnap"
	}
	return ""
}

// extractSmartModelSerial extracts model_name and serial_number from raw smartctl JSON output.
func extractSmartModelSerial(out []byte) (model, serial string) {
	var parsed map[string]interface{}
	if err := json.Unmarshal(out, &parsed); err != nil {
		return "", ""
	}
	model = extractString(parsed, "model_name")
	serial = extractString(parsed, "serial_number")
	return model, serial
}

// resolveConfigPath returns the path to the config file that would be written.
// Mirrors the auto-detect logic in LoadConfig().
func resolveConfigPath(cfg *Config) string {
	// If we can find the file that was actually loaded, use it.
	for _, p := range []string{"config.yaml", "/etc/smartha-agent/config.yaml"} {
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

// writeDeviceOverrides backs up the config file and appends the device_overrides section.
func writeDeviceOverrides(configPath string, drives []discoverDriveResult) error {
	// Read existing config.
	existing, err := os.ReadFile(configPath)
	if err != nil {
		return fmt.Errorf("reading %s: %w", configPath, err)
	}

	// Back up the original.
	backupPath := configPath + ".bak"
	fmt.Printf("\nBacking up %s to %s... ", configPath, backupPath)
	if err := os.WriteFile(backupPath, existing, 0644); err != nil {
		return fmt.Errorf("writing backup %s: %w", backupPath, err)
	}
	fmt.Println("done")

	// Build the overrides to append.
	type overrideEntry struct {
		Device   string `yaml:"device"`
		Protocol string `yaml:"protocol"`
	}
	type overridesDoc struct {
		DeviceOverrides []overrideEntry `yaml:"device_overrides"`
	}

	var entries []overrideEntry
	for _, r := range drives {
		proto := "sat"
		entries = append(entries, overrideEntry{Device: r.path, Protocol: proto})
	}

	doc := overridesDoc{DeviceOverrides: entries}
	addition, err := yaml.Marshal(doc)
	if err != nil {
		return fmt.Errorf("marshalling device_overrides: %w", err)
	}

	// Append to existing config (preserving all existing fields).
	var newContent []byte
	if len(existing) > 0 && existing[len(existing)-1] != '\n' {
		newContent = append(existing, '\n')
	} else {
		newContent = existing
	}
	newContent = append(newContent, addition...)

	if err := os.WriteFile(configPath, newContent, 0644); err != nil {
		return fmt.Errorf("writing %s: %w", configPath, err)
	}

	fmt.Printf("\nAdded to %s:\n\n", configPath)
	fmt.Print("  " + strings.ReplaceAll(string(addition), "\n", "\n  "))
	fmt.Println()
	fmt.Println("Restart the agent to apply:")
	fmt.Println("  sudo systemctl restart smartha-agent")

	return nil
}
