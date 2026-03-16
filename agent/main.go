// SMART Sniffer Agent — lightweight REST API wrapping smartctl.
// Exposes SMART disk health data over HTTP for consumption by the
// Home Assistant custom integration (or any other client).
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/grandcat/zeroconf"
)

// version is set at build time via -ldflags "-X main.version=...".
// Falls back to "dev" for untagged builds.
var version = "0.1.0"

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

func main() {
	// Direct log output to stdout so launchd/systemd captures it via
	// StandardOutPath. Preflight errors still go to stderr via fmt.Fprintf.
	log.SetOutput(os.Stdout)

	cfg, err := LoadConfig()
	if err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: failed to load configuration: %v\n", err)
		os.Exit(1)
	}

	// --- Preflight checks (order matters) ---
	if err := preflightSmartctlExists(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	smartctlVer, err := preflightSmartctlVersion()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	drives, err := preflightScanDrives()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	// --- Startup banner ---
	authLabel := "disabled"
	if cfg.Token != "" {
		authLabel = "enabled"
	}
	mdnsLabel := "disabled"
	if cfg.MDNSEnabled() {
		mdnsLabel = "enabled"
	}
	log.Printf("SMART Sniffer Agent v%s", version)
	log.Printf("smartctl version: %s", smartctlVer)
	log.Printf("Drives detected: %d", len(drives))
	log.Printf("Listening on: 0.0.0.0:%d", cfg.Port)
	log.Printf("Auth: %s", authLabel)
	log.Printf("mDNS: %s", mdnsLabel)

	// --- Cache / background scanner ---
	cache := NewDriveCache(cfg.ScanInterval)
	cache.Refresh() // initial population

	// --- HTTP server ---
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", handleHealth)
	mux.HandleFunc("/api/drives", cache.HandleDrives)
	mux.HandleFunc("/api/drives/", cache.HandleDrive) // trailing slash catches /api/drives/{id}

	var handler http.Handler = mux
	if cfg.Token != "" {
		handler = authMiddleware(cfg.Token, mux)
	}

	srv := &http.Server{
		Addr:    fmt.Sprintf(":%d", cfg.Port),
		Handler: handler,
	}

	// Graceful shutdown on SIGINT / SIGTERM.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("HTTP server error: %v", err)
		}
	}()

	go cache.RunBackground(ctx)

	// --- mDNS / Zeroconf service advertisement ---
	var mdnsServer *zeroconf.Server
	if cfg.MDNSEnabled() {
		hostname, _ := os.Hostname()
		// Strip domain suffix — dots in mDNS instance names break DNS label parsing.
		if idx := strings.IndexByte(hostname, '.'); idx != -1 {
			hostname = hostname[:idx]
		}
		authFlag := "0"
		if cfg.Token != "" {
			authFlag = "1"
		}
		txt := []string{
			"txtvers=1",
			"version=" + version,
			"hostname=" + hostname,
			"os=" + detectOS(),
			"auth=" + authFlag,
			"drives=" + strconv.Itoa(len(drives)),
		}
		instance := "smartha-" + hostname
		mdnsServer, err = zeroconf.Register(instance, "_smartha._tcp", "local.", cfg.Port, txt, nil)
		if err != nil {
			log.Printf("WARNING: mDNS registration failed: %v", err)
		} else {
			log.Printf("mDNS: advertising %s._smartha._tcp.local. on port %d", instance, cfg.Port)
		}
	}

	<-ctx.Done()
	log.Println("Shutting down…")

	// Deregister mDNS before stopping HTTP.
	if mdnsServer != nil {
		mdnsServer.Shutdown()
		log.Println("mDNS: deregistered")
	}

	shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutCtx); err != nil {
		log.Printf("HTTP shutdown error: %v", err)
	}
}

// detectOS returns the runtime OS as a short string for mDNS TXT records.
func detectOS() string {
	out, err := exec.Command("uname", "-s").Output()
	if err != nil {
		return "unknown"
	}
	return strings.ToLower(strings.TrimSpace(string(out)))
}

// ---------------------------------------------------------------------------
// Preflight checks
// ---------------------------------------------------------------------------

func preflightSmartctlExists() error {
	_, err := exec.LookPath("smartctl")
	if err != nil {
		return fmt.Errorf(`ERROR: smartctl not found in PATH.
smartmontools is required for SMART Sniffer to function.

Install it for your platform:
  Linux (Debian/Ubuntu):  sudo apt install smartmontools
  Linux (RHEL/Fedora):    sudo dnf install smartmontools
  macOS (Homebrew):       brew install smartmontools
  Windows (Chocolatey):   choco install smartmontools

More info: https://www.smartmontools.org/wiki/Download
`)
	}
	return nil
}

// preflightSmartctlVersion runs "smartctl --version", extracts the version
// string and returns it. Exits on unexpected failure.
func preflightSmartctlVersion() (string, error) {
	out, err := exec.Command("smartctl", "--version").CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("ERROR: failed to run smartctl --version: %v\nOutput: %s", err, string(out))
	}
	ver := parseSmartctlVersion(string(out))
	if ver == "" {
		ver = "unknown"
	}
	log.Printf("smartctl %s detected", ver)
	return ver, nil
}

// parseSmartctlVersion extracts a version like "7.4" from the --version output.
var versionRe = regexp.MustCompile(`smartctl\s+(\d+\.\d+)`)

func parseSmartctlVersion(output string) string {
	m := versionRe.FindStringSubmatch(output)
	if len(m) >= 2 {
		return m[1]
	}
	return ""
}

// preflightScanDrives runs "smartctl --scan" and checks for permission errors
// or zero drives.
func preflightScanDrives() ([]string, error) {
	out, err := exec.Command("smartctl", "--scan").CombinedOutput()
	outStr := string(out)

	// Permission errors surface in different ways depending on OS.
	// Check the output text first (smartctl may exit 0 but still warn),
	// then fall back to the generic exec error.
	if containsPermissionError(outStr) {
		return nil, fmt.Errorf(`ERROR: smartctl requires elevated privileges to read drive data.

Run the agent with sufficient permissions:
  Linux/macOS:  sudo ./smartha-agent
  Windows:      Run as Administrator`)
	}
	if err != nil {
		return nil, fmt.Errorf("ERROR: smartctl --scan failed: %v\nOutput: %s", err, outStr)
	}

	drives := parseScanOutput(outStr)
	if len(drives) == 0 {
		log.Println("WARNING: smartctl detected no drives. The agent will start but no data will be available.")
		log.Println("Check that your drives support SMART and are visible to the OS.")
	}

	return drives, nil
}

// containsPermissionError is a best-effort heuristic for permission problems.
func containsPermissionError(output string) bool {
	lower := strings.ToLower(output)
	return strings.Contains(lower, "permission denied") ||
		strings.Contains(lower, "operation not permitted") ||
		strings.Contains(lower, "requires root") ||
		strings.Contains(lower, "access is denied")
}

// parseScanOutput extracts device paths from "smartctl --scan" output.
// Each line typically looks like: /dev/sda -d sat # /dev/sda, ATA device
func parseScanOutput(output string) []string {
	var drives []string
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.Fields(line)
		if len(parts) > 0 {
			drives = append(drives, parts[0])
		}
	}
	return drives
}

// ---------------------------------------------------------------------------
// Auth middleware
// ---------------------------------------------------------------------------

func authMiddleware(token string, next http.Handler) http.Handler {
	expected := "Bearer " + token
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != expected {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnauthorized)
			w.Write([]byte(`{"error":"unauthorized"}`))
			return
		}
		next.ServeHTTP(w, r)
	})
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"status":"ok"}`))
}

// ---------------------------------------------------------------------------
// Drive cache — periodically refreshes SMART data in the background
// ---------------------------------------------------------------------------

// DriveCache holds cached SMART data for all discovered drives.
type DriveCache struct {
	mu           sync.RWMutex
	interval     time.Duration
	drives       map[string]DriveInfo // keyed by slug id
	driveOrder   []string             // preserve discovery order
}

// DriveInfo is the per-drive cached payload.
type DriveInfo struct {
	ID         string          `json:"id"`
	DevicePath string          `json:"device_path"`
	Model      string          `json:"model"`
	Serial     string          `json:"serial"`
	Protocol   string          `json:"protocol"` // ATA, NVMe, SCSI, …
	RawJSON    json.RawMessage `json:"smart_data"`
}

// DriveSummary is the abbreviated representation returned by GET /api/drives.
type DriveSummary struct {
	ID         string `json:"id"`
	DevicePath string `json:"device_path"`
	Model      string `json:"model"`
	Serial     string `json:"serial"`
	Protocol   string `json:"protocol"`
}

func NewDriveCache(interval time.Duration) *DriveCache {
	return &DriveCache{
		interval: interval,
		drives:   make(map[string]DriveInfo),
	}
}

// Refresh re-scans drives and pulls full SMART data for each.
func (dc *DriveCache) Refresh() {
	// Discover drives via JSON scan.
	scanOut, err := exec.Command("smartctl", "--json", "--scan").CombinedOutput()
	if err != nil {
		log.Printf("drive scan error: %v", err)
		return
	}

	var scanResult struct {
		Devices []struct {
			Name     string `json:"name"`
			InfoName string `json:"info_name"`
			Type     string `json:"type"`
			Protocol string `json:"protocol"`
		} `json:"devices"`
	}
	if err := json.Unmarshal(scanOut, &scanResult); err != nil {
		log.Printf("failed to parse scan JSON: %v", err)
		return
	}

	newDrives := make(map[string]DriveInfo, len(scanResult.Devices))
	var order []string

	for _, dev := range scanResult.Devices {
		info := dc.fetchDriveInfo(dev.Name, dev.Protocol)
		newDrives[info.ID] = info
		order = append(order, info.ID)
	}

	dc.mu.Lock()
	dc.drives = newDrives
	dc.driveOrder = order
	dc.mu.Unlock()

	log.Printf("cache refreshed: %d drive(s)", len(newDrives))
}

// fetchDriveInfo calls smartctl -a --json on a single device and parses the
// key fields we care about.
func (dc *DriveCache) fetchDriveInfo(devicePath, protocol string) DriveInfo {
	out, err := exec.Command("smartctl", "--json", "-a", devicePath).CombinedOutput()
	if err != nil {
		// smartctl exits non-zero when SMART status is failing — that's expected.
		// We still want to parse the JSON if possible.
		log.Printf("smartctl -a %s exited with error (may be normal): %v", devicePath, err)
	}

	info := DriveInfo{
		DevicePath: devicePath,
		Protocol:   protocol,
		RawJSON:    json.RawMessage(out),
	}

	// Best-effort extraction of model/serial from the JSON blob.
	// The structure differs between ATA and NVMe — handle both.
	var parsed map[string]interface{}
	if err := json.Unmarshal(out, &parsed); err == nil {
		info.Model = extractString(parsed, "model_name")
		info.Serial = extractString(parsed, "serial_number")

		// Prefer the protocol from the SMART data itself — more accurate than
		// the scan output. SATA drives accessed via SAT (SCSI/ATA Translation)
		// report as "SCSI" during --scan but correctly self-identify as "ATA"
		// in their full SMART data. Using the scan protocol would cause the HA
		// integration to skip ATA-specific sensors on perfectly valid SATA drives.
		if devMap, ok := parsed["device"].(map[string]interface{}); ok {
			if proto := extractString(devMap, "protocol"); proto != "" {
				info.Protocol = proto
			}
		}

		// TODO: NVMe devices may nest these under "nvme_smart_health_information_log".
		// TODO: SAS/SCSI devices have yet another layout — add support as needed.
	}

	// Build a URL-safe slug from serial (preferred) or device path.
	info.ID = makeDriveSlug(info.Serial, devicePath)

	return info
}

// extractString does a shallow lookup in a JSON object for a string value.
func extractString(m map[string]interface{}, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

// slugRe matches runs of non-alphanumeric characters for slug generation.
var slugRe = regexp.MustCompile(`[^a-z0-9]+`)

// makeDriveSlug creates a URL-safe identifier for a drive.
func makeDriveSlug(serial, devicePath string) string {
	base := serial
	if base == "" {
		base = devicePath
	}
	// Simple slug: lowercase, replace non-alphanumeric with hyphens, collapse.
	slug := strings.ToLower(base)
	slug = slugRe.ReplaceAllString(slug, "-")
	slug = strings.Trim(slug, "-")
	if slug == "" {
		slug = "unknown"
	}
	return slug
}

// RunBackground starts the periodic refresh loop.
func (dc *DriveCache) RunBackground(ctx context.Context) {
	ticker := time.NewTicker(dc.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			dc.Refresh()
		}
	}
}

// HandleDrives serves GET /api/drives — returns summary list.
func (dc *DriveCache) HandleDrives(w http.ResponseWriter, r *http.Request) {
	dc.mu.RLock()
	defer dc.mu.RUnlock()

	summaries := make([]DriveSummary, 0, len(dc.driveOrder))
	for _, id := range dc.driveOrder {
		d := dc.drives[id]
		summaries = append(summaries, DriveSummary{
			ID:         d.ID,
			DevicePath: d.DevicePath,
			Model:      d.Model,
			Serial:     d.Serial,
			Protocol:   d.Protocol,
		})
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(summaries)
}

// HandleDrive serves GET /api/drives/{id} — returns full SMART data.
func (dc *DriveCache) HandleDrive(w http.ResponseWriter, r *http.Request) {
	// Extract the drive ID from the URL path.
	// Path is /api/drives/{id} — strip the prefix.
	id := strings.TrimPrefix(r.URL.Path, "/api/drives/")
	id = strings.TrimSuffix(id, "/")

	if id == "" {
		// Bare /api/drives/ with trailing slash — treat as list.
		dc.HandleDrives(w, r)
		return
	}

	dc.mu.RLock()
	drive, ok := dc.drives[id]
	dc.mu.RUnlock()

	if !ok {
		http.Error(w, `{"error":"drive not found"}`, http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(drive)
}
