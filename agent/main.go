// SMART Sniffer Agent — lightweight REST API wrapping smartctl.
// Exposes SMART disk health data over HTTP for consumption by the
// Home Assistant custom integration (or any other client).
package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/grandcat/zeroconf"
)

// version is set at build time via -ldflags "-X main.version=...".
// Falls back to "dev" for untagged builds.
var version = "0.1.0"

// startTime records when the agent process started, used for the uptime
// field in /api/health.
var startTime = time.Now()

// ---------------------------------------------------------------------------
// RunAgent — shared entry point for all platforms
// ---------------------------------------------------------------------------
//
// RunAgent loads config, runs preflight checks, starts the HTTP server and
// mDNS advertisement, and blocks until ctx is canceled. Platform-specific
// main() wrappers in main_unix.go and main_windows.go are responsible for
// building the ctx (signal-driven on Unix, SCM-driven on Windows) and
// calling RunAgent.
//
// If ready is non-nil, it is closed at the moment the HTTP listener has
// successfully bound its port. Callers that need to know when the agent is
// accepting connections (e.g. Windows Service handler reporting svc.Running
// to the SCM) should pass a channel; callers that don't can pass nil.
//
// See docs/internal/plans/plan-v0.5.1-consolidated-changes.md §Change 3.
func RunAgent(ctx context.Context, ready chan<- struct{}) error {
	// Direct log output to stdout so launchd/systemd captures it via
	// StandardOutPath. Preflight errors still go to stderr via fmt.Fprintf.
	log.SetOutput(os.Stdout)

	cfg, err := LoadConfig()
	if err != nil {
		return fmt.Errorf("failed to load configuration: %w", err)
	}

	// --discover flag: probe drives, detect protocols, optionally write config.
	// Must resolve smartctl path before running (discover needs to call smartctl).
	if cfg.Discover {
		smartctlPath, _, resolveErr := resolveSmartctlPath("7.0")
		if resolveErr != nil {
			return resolveErr
		}
		cfg.SmartctlPath = smartctlPath
		return RunDiscover(cfg, cfg.NoWrite)
	}

	// --- Preflight: resolve smartctl binary ---
	const minSmartctlVersion = "7.0"
	smartctlPath, smartctlVer, err := resolveSmartctlPath(minSmartctlVersion)
	if err != nil {
		return err
	}
	cfg.SmartctlPath = smartctlPath
	log.Printf("using smartctl: %s (version %s)", smartctlPath, smartctlVer)

	drives, err := preflightScanDrives(cfg.SmartctlPath)
	if err != nil {
		return err
	}

	// --- Startup banner ---
	authLabel := "disabled"
	if cfg.Token != "" {
		authLabel = "enabled"
	}
	mdnsLabel := "disabled"
	if cfg.MDNSEnabled() {
		mdnsLabel = "enabled"
		if cfg.AdvertiseInterface != "" {
			mdnsLabel += " (interface: " + cfg.AdvertiseInterface + ")"
		}
	}
	log.Printf("SMART Sniffer Agent v%s", version)
	log.Printf("smartctl version: %s", smartctlVer)
	log.Printf("Drives detected: %d", len(drives))
	log.Printf("Listening on: 0.0.0.0:%d", cfg.Port)
	log.Printf("Auth: %s", authLabel)
	log.Printf("mDNS: %s", mdnsLabel)
	if cfg.StandbyMode != "never" {
		log.Printf("Standby mode: %s", cfg.StandbyMode)
	}

	// --- Cache / background scanner ---
	cache := NewDriveCache(cfg)
	cache.Refresh() // initial population

	// --- Filesystem cache (if configured) ---
	var fsCache *FilesystemCache
	if len(cfg.Filesystems) > 0 {
		fsCache = NewFilesystemCache(cfg.Filesystems)
		fsCache.Refresh() // initial population
		cache.fsCache = fsCache
		log.Printf("Filesystem monitoring: %d mount(s)", len(cfg.Filesystems))
	}

	// --- HTTP server ---
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", handleHealth(cache))
	mux.HandleFunc("/api/drives", cache.HandleDrives)
	mux.HandleFunc("/api/drives/", cache.HandleDrive) // trailing slash catches /api/drives/{id}
	if fsCache != nil {
		mux.HandleFunc("/api/filesystems", fsCache.HandleFilesystems)
	}

	var handler http.Handler = mux
	if cfg.Token != "" {
		handler = authMiddleware(cfg.Token, mux)
	}

	srv := &http.Server{
		Addr:    fmt.Sprintf(":%d", cfg.Port),
		Handler: handler,
	}

	// --- Bind the listener explicitly so we can signal ready at the exact
	// moment the kernel has accepted our bind. srv.Serve(ln) then blocks
	// in a goroutine. This eliminates both failure modes of sleep-based
	// ready signaling: over-reporting on fast boots (Running before bind)
	// and under-reporting on slow boots (SCM thinks we hung).
	ln, err := net.Listen("tcp", srv.Addr)
	if err != nil {
		return fmt.Errorf("failed to bind listener on %s: %w", srv.Addr, err)
	}
	if ready != nil {
		close(ready)
	}

	go func() {
		if err := srv.Serve(ln); err != nil && err != http.ErrServerClosed {
			log.Printf("HTTP server error: %v", err)
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

		// Resolve which interfaces to advertise on.
		ifaces, ifaceDesc := cfg.ResolveAdvertiseInterfaces()
		log.Printf("mDNS: interfaces: %s", ifaceDesc)

		// Determine preferred IP for TXT record so the HA integration
		// doesn't have to guess which IP is the real LAN address.
		preferredIP := PreferredIP(ifaces)

		// Build the mDNS instance name. Use --mdns-name / mdns_name if
		// provided (e.g. by the HA add-on to avoid container hostname
		// collisions), otherwise default to smartha-<hostname>.
		instance := "smartha-" + hostname
		if cfg.MDNSName != "" {
			instance = cfg.MDNSName
			log.Printf("mDNS: using custom instance name: %s", instance)
		}
		// DNS labels are limited to 63 bytes — truncate to be safe.
		if len(instance) > 63 {
			instance = instance[:63]
		}

		txt := []string{
			"txtvers=1",
			"version=" + version,
			"hostname=" + hostname,
			"os=" + detectOS(),
			"auth=" + authFlag,
			"drives=" + strconv.Itoa(len(drives)),
		}
		if preferredIP != "" {
			txt = append(txt, "ip="+preferredIP)
			log.Printf("mDNS: preferred IP: %s", preferredIP)
		}

		mdnsServer, err = zeroconf.Register(instance, "_smartha._tcp", "local.", cfg.Port, txt, ifaces)
		if err != nil {
			log.Printf("WARNING: mDNS registration failed: %v", err)
		} else {
			log.Printf("mDNS: advertising %s._smartha._tcp.local. on port %d", instance, cfg.Port)
		}
	}

	<-ctx.Done()
	log.Println("Shutting down…")

	// --- Bounded graceful shutdown (Change 9) -----------------------------
	//
	// shutdownBudget caps the total time the agent spends in shutdown. This
	// is the standard production pattern for services running under a
	// supervisor (HA Supervisor, systemd, Kubernetes, Windows SCM): the
	// supervisor has its own hard-kill timer and we want our own budget to
	// fire slightly before it, so that WE choose what to drop rather than
	// the OS SIGKILLing us mid-cleanup.
	//
	// Tuned to HA Supervisor's default 10s add-on stop_timeout (2s margin).
	// Also well within Windows SCM's default 20s service stop timeout.
	// If the add-on config.yaml ever sets a custom stop_timeout, revisit
	// this constant. See docs/internal/plans/plan-v0.5.1-consolidated-changes.md
	// §Change 9 for rationale.
	const shutdownBudget = 8 * time.Second
	shutdownCtx, cancelShutdown := context.WithTimeout(context.Background(), shutdownBudget)
	defer cancelShutdown()

	shutdownStart := time.Now()
	phase := struct {
		mdnsDone        bool
		httpDrainDone   bool
		coordinatorDone bool
		current         string
	}{current: "mdns"}

	done := make(chan struct{})
	go func() {
		defer close(done)

		// Phase 1: mDNS goodbye packet so clients stop discovering us.
		phase.current = "mdns"
		if mdnsServer != nil {
			mdnsServer.Shutdown()
			log.Println("mDNS: deregistered")
		}
		phase.mdnsDone = true

		// Phase 2: HTTP graceful drain, bounded by the shared shutdownCtx.
		phase.current = "http_drain"
		if err := srv.Shutdown(shutdownCtx); err != nil {
			log.Printf("HTTP shutdown error: %v", err)
		}
		phase.httpDrainDone = true

		// Phase 3: coordinator close (currently a no-op; placeholder for
		// future cache flush, filesystem sync, etc. without requiring
		// another shutdown-path refactor).
		phase.current = "coordinator"
		phase.coordinatorDone = true
	}()

	select {
	case <-done:
		log.Printf("shutdown complete elapsed=%s", time.Since(shutdownStart))
	case <-shutdownCtx.Done():
		log.Printf("WARN shutdown budget exceeded phase=%s elapsed=%s "+
			"mdns_done=%t drain_done=%t coordinator_done=%t",
			phase.current, time.Since(shutdownStart),
			phase.mdnsDone, phase.httpDrainDone, phase.coordinatorDone)
	}

	return nil
}

// detectOS returns the runtime OS as a short string for mDNS TXT records
// and the /api/health response. Uses runtime.GOOS instead of shelling out
// to uname, which doesn't exist on Windows.
func detectOS() string {
	return runtime.GOOS // "linux", "darwin", "windows", etc.
}

// ---------------------------------------------------------------------------
// Preflight checks
// ---------------------------------------------------------------------------

// preflightSmartctlExists and preflightSmartctlVersion have been replaced
// by resolveSmartctlPath() which handles both existence and version checking
// with fallback to known platform-specific paths.

// parseSmartctlVersion extracts a version like "7.4" from the --version output.
var versionRe = regexp.MustCompile(`smartctl\s+(\d+\.\d+)`)

func parseSmartctlVersion(output string) string {
	m := versionRe.FindStringSubmatch(output)
	if len(m) >= 2 {
		return m[1]
	}
	return ""
}

// isSmartctlVersionOK returns true if ver >= minVer (both as "X.Y" strings).
// Returns true for "unknown" versions to avoid blocking on parse failures --
// the agent will fail later on --json calls anyway.
func isSmartctlVersionOK(ver, minVer string) bool {
	if ver == "unknown" || ver == "" {
		return true
	}
	parse := func(s string) (int, int) {
		parts := strings.SplitN(s, ".", 2)
		major, _ := strconv.Atoi(parts[0])
		minor := 0
		if len(parts) > 1 {
			minor, _ = strconv.Atoi(parts[1])
		}
		return major, minor
	}
	vMaj, vMin := parse(ver)
	mMaj, mMin := parse(minVer)
	return vMaj > mMaj || (vMaj == mMaj && vMin >= mMin)
}

// smartctlSearchPaths lists known installation locations for smartctl across
// platforms. Checked in order when the PATH version is missing or too old.
// See docs/internal/research/smartctl-install-paths.md for full research.
var smartctlSearchPaths = []string{
	// NAS platforms (most likely to need fallback)
	"/var/packages/synocli-disk/target/sbin/smartctl", // SynoCommunity on Synology
	"/opt/sbin/smartctl",                               // Entware (Synology/QNAP)
	"/opt/bin/smartctl",                                // Entware alternate
	"/boot/extra/sbin/smartctl",                        // Unraid NerdTools
	"/boot/extra/bin/smartctl",                         // Unraid NerdTools alternate
	"/share/CACHEDEV1_DATA/.qpkg/smartmontools/bin/smartctl", // QNAP QPKG

	// Standard Linux
	"/usr/sbin/smartctl", // Debian, Ubuntu, RHEL, Fedora, Arch, Alpine, Proxmox, OMV

	// BSD / TrueNAS CORE
	"/usr/local/sbin/smartctl", // FreeBSD, OpenBSD

	// macOS
	"/usr/local/bin/smartctl",  // Homebrew (Intel)
	"/opt/homebrew/bin/smartctl", // Homebrew (Apple Silicon)
	"/opt/local/sbin/smartctl", // MacPorts

	// NixOS
	"/run/current-system/sw/sbin/smartctl", // NixOS system profile
}

// resolveSmartctlPath finds the best smartctl binary available. It checks
// PATH first, then falls back to known platform-specific paths. Returns the
// full path and version string, or an error if no usable binary is found.
func resolveSmartctlPath(minVersion string) (string, string, error) {
	// 1. Try PATH first (current behavior for most users).
	pathBin, err := exec.LookPath("smartctl")
	if err == nil {
		ver := getSmartctlVersion(pathBin)
		if isSmartctlVersionOK(ver, minVersion) {
			return pathBin, ver, nil
		}
		log.Printf("smartctl in PATH is %s (requires %s+), searching known paths...", ver, minVersion)
	}

	// 2. Search known platform-specific paths.
	for _, candidate := range smartctlSearchPaths {
		info, statErr := os.Stat(candidate)
		if statErr != nil || info.IsDir() {
			continue
		}
		if info.Mode()&0111 == 0 {
			continue // not executable
		}
		ver := getSmartctlVersion(candidate)
		if ver == "" {
			continue
		}
		if isSmartctlVersionOK(ver, minVersion) {
			log.Printf("found smartctl %s at %s", ver, candidate)
			return candidate, ver, nil
		}
	}

	// 3. Nothing found.
	if pathBin != "" {
		return "", "", fmt.Errorf(`ERROR: smartctl found in PATH but version is too old.
The agent requires smartctl %s or newer for JSON output support.

The smartctl in your PATH is at: %s

Install a newer version:
  Linux (Debian/Ubuntu):  sudo apt install smartmontools
  Linux (RHEL/Fedora):    sudo dnf install smartmontools
  macOS (Homebrew):       brew install smartmontools
  Synology:               Install SynoCli Disk Tools from SynoCommunity
  QNAP:                   Install smartmontools via Entware (opkg install smartmontools)

Run 'smartctl --version' to check your current version.`, minVersion, pathBin)
	}

	return "", "", fmt.Errorf(`ERROR: smartctl not found in PATH or known locations.
smartmontools is required for SMART Sniffer to function.

Install it for your platform:
  Linux (Debian/Ubuntu):  sudo apt install smartmontools
  Linux (RHEL/Fedora):    sudo dnf install smartmontools
  macOS (Homebrew):       brew install smartmontools
  Windows (Chocolatey):   choco install smartmontools
  Synology:               Install SynoCli Disk Tools from SynoCommunity
  QNAP:                   Install smartmontools via Entware (opkg install smartmontools)

More info: https://www.smartmontools.org/wiki/Download
`)
}

// getSmartctlVersion runs "<path> --version" and returns the version string,
// or "" if it can't be determined.
func getSmartctlVersion(path string) string {
	out, err := exec.Command(path, "--version").CombinedOutput()
	if err != nil {
		return ""
	}
	return parseSmartctlVersion(string(out))
}

// preflightScanDrives runs "smartctl --scan" and checks for permission errors
// or zero drives.
func preflightScanDrives(smartctlPath string) ([]string, error) {
	out, err := exec.Command(smartctlPath, "--scan").CombinedOutput()
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
	expected := []byte("Bearer " + token)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := []byte(r.Header.Get("Authorization"))
		if subtle.ConstantTimeCompare(auth, expected) != 1 {
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

// healthResponse is the JSON payload for GET /api/health.
type healthResponse struct {
	Status      string   `json:"status"`
	Version     string   `json:"version"`
	OS          string   `json:"os"`
	Uptime      int      `json:"uptime_seconds"`
	Endpoints   []string `json:"endpoints"`
	Drives      int      `json:"drives"`
	Filesystems int      `json:"filesystems"`
}

// handleHealth serves GET /api/health — includes available endpoints and counts.
func handleHealth(cache *DriveCache) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		endpoints := []string{"/api/health", "/api/drives", "/api/drives/{id}"}

		var fsCount int
		if cache.fsCache != nil {
			endpoints = append(endpoints, "/api/filesystems")
			fsCount = len(cache.fsCache.configs)
		}

		cache.mu.RLock()
		driveCount := len(cache.drives)
		cache.mu.RUnlock()

		resp := healthResponse{
			Status:      "ok",
			Version:     version,
			OS:          detectOS(),
			Uptime:      int(time.Since(startTime).Seconds()),
			Endpoints:   endpoints,
			Drives:      driveCount,
			Filesystems: fsCount,
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	}
}

// ---------------------------------------------------------------------------
// Drive cache — periodically refreshes SMART data in the background
// ---------------------------------------------------------------------------

// DriveCache holds cached SMART data for all discovered drives.
type DriveCache struct {
	mu             sync.RWMutex
	interval       time.Duration
	drives         map[string]DriveInfo // keyed by slug id
	driveOrder     []string             // preserve discovery order
	fsCache        *FilesystemCache     // refreshed alongside drive data (nil = disabled)
	lastHealthBits map[string]int       // per-device last-seen health bits (suppress repeat logs)
	standbyMode    string               // never, standby, sleep, idle
	firstPoll      bool                 // true until first Refresh() completes; uses --scan-open on first poll
	protocolCache  map[string]string    // per-device-path detected or overridden protocol
	cfg            *Config              // full agent config (for device_overrides access)
}

// DriveInfo is the per-drive cached payload.
type DriveInfo struct {
	ID          string          `json:"id"`
	DevicePath  string          `json:"device_path"`
	Model       string          `json:"model"`
	Serial      string          `json:"serial"`
	Protocol    string          `json:"protocol"`                      // ATA, NVMe, SCSI, ...
	InStandby   bool            `json:"in_standby,omitempty"`          // true when drive was skipped due to standby
	LastUpdated string          `json:"last_updated,omitempty"`        // ISO 8601 timestamp of last successful SMART fetch
	RawJSON     json.RawMessage `json:"smart_data"`
}

// DriveSummary is the abbreviated representation returned by GET /api/drives.
type DriveSummary struct {
	ID         string `json:"id"`
	DevicePath string `json:"device_path"`
	Model      string `json:"model"`
	Serial     string `json:"serial"`
	Protocol   string `json:"protocol"`
}

func NewDriveCache(cfg *Config) *DriveCache {
	return &DriveCache{
		interval:       cfg.ScanInterval,
		drives:         make(map[string]DriveInfo),
		lastHealthBits: make(map[string]int),
		standbyMode:    cfg.StandbyMode,
		firstPoll:      true,
		protocolCache:  make(map[string]string),
		cfg:            cfg,
	}
}

// scanDevice is the per-entry shape returned by smartctl --scan / --scan-open.
type scanDevice struct {
	Name     string `json:"name"`
	InfoName string `json:"info_name"`
	Type     string `json:"type"`
	Protocol string `json:"protocol"`
}

// Refresh re-scans drives and pulls full SMART data for each.
func (dc *DriveCache) Refresh() {
	// On the first poll, use --scan-open so smartctl opens device handles for
	// accurate protocol detection. Subsequent polls use --scan to avoid waking
	// sleeping drives. Fall back to --scan if --scan-open is unsupported.
	scanCmd := "--scan"
	dc.mu.Lock()
	isFirstPoll := dc.firstPoll
	if dc.firstPoll {
		scanCmd = "--scan-open"
		dc.firstPoll = false
		log.Println("first poll: using --scan-open for protocol detection, waking drives for SMART baseline")
	}
	dc.mu.Unlock()

	scanOut, err := exec.Command(dc.cfg.SmartctlPath, "--json", scanCmd).CombinedOutput()
	if err != nil && scanCmd == "--scan-open" {
		log.Println("--scan-open failed, falling back to --scan")
		scanOut, err = exec.Command(dc.cfg.SmartctlPath, "--json", "--scan").CombinedOutput()
	}
	if err != nil {
		log.Printf("drive scan error: %v", err)
		return
	}

	var scanResult struct {
		Devices []scanDevice `json:"devices"`
	}
	if err := json.Unmarshal(scanOut, &scanResult); err != nil {
		log.Printf("failed to parse scan JSON: %v", err)
		return
	}

	// Cache the detected protocol for each device found by scan.
	dc.mu.Lock()
	for _, dev := range scanResult.Devices {
		if dev.Protocol != "" {
			dc.protocolCache[dev.Name] = dev.Protocol
		}
	}
	dc.mu.Unlock()

	// Merge device_overrides: cache their protocols and inject any devices that
	// weren't found by scan (e.g. Synology /dev/sata* paths).
	if dc.cfg != nil {
		for _, ov := range dc.cfg.DeviceOverrides {
			dc.mu.Lock()
			dc.protocolCache[ov.Device] = ov.Protocol
			dc.mu.Unlock()

			found := false
			for _, dev := range scanResult.Devices {
				if dev.Name == ov.Device {
					found = true
					break
				}
			}
			if !found {
				scanResult.Devices = append(scanResult.Devices, scanDevice{
					Name:     ov.Device,
					Protocol: ov.Protocol,
				})
				log.Printf("added override device: %s (protocol: %s)", ov.Device, ov.Protocol)
			}
		}
	}

	newDrives := make(map[string]DriveInfo, len(scanResult.Devices))
	var order []string

	for _, dev := range scanResult.Devices {
		info, inStandby := dc.fetchDriveInfo(dev.Name, dev.Protocol, isFirstPoll)
		if inStandby {
			// Drive is sleeping -- serve last known data with standby flag.
			slug := makeDriveSlug("", dev.Name) // fallback slug from path
			dc.mu.RLock()
			if existing, ok := dc.drives[slug]; ok {
				existing.InStandby = true
				newDrives[existing.ID] = existing
				order = append(order, existing.ID)
				slug = existing.ID // use the real ID for logging
			} else {
				// Check all cached drives by device path (serial-based slug won't match path-based slug).
				for id, d := range dc.drives {
					if d.DevicePath == dev.Name {
						d.InStandby = true
						newDrives[id] = d
						order = append(order, id)
						slug = id
						break
					}
				}
			}
			dc.mu.RUnlock()
			// Log standby transition once.
			if prev, ok := dc.drives[slug]; !ok || !prev.InStandby {
				log.Printf("drive %s is in standby, serving cached data", dev.Name)
			}
			continue
		}
		info.InStandby = false
		newDrives[info.ID] = info
		order = append(order, info.ID)
	}

	dc.mu.Lock()
	dc.drives = newDrives
	dc.driveOrder = order
	dc.mu.Unlock()

	log.Printf("cache refreshed: %d drive(s)", len(newDrives))

	// Refresh filesystem data on the same cycle.
	if dc.fsCache != nil {
		dc.fsCache.Refresh()
	}
}

// decodeExecBits returns human-readable labels for smartctl execution
// failure bits (0-2). These indicate the command itself failed.
func decodeExecBits(code int) string {
	bits := code & 0x07
	var reasons []string
	if bits&1 != 0 {
		reasons = append(reasons, "command line parse error")
	}
	if bits&2 != 0 {
		reasons = append(reasons, "device open failed")
	}
	if bits&4 != 0 {
		reasons = append(reasons, "command failed or checksum error")
	}
	return strings.Join(reasons, ", ")
}

// decodeHealthBits returns human-readable labels for smartctl drive
// health flag bits (3-7). These indicate drive status, not command failure.
func decodeHealthBits(code int) string {
	bits := code & 0xF8
	var flags []string
	if bits&8 != 0 {
		flags = append(flags, "DISK FAILING")
	}
	if bits&16 != 0 {
		flags = append(flags, "prefail attributes <= threshold")
	}
	if bits&32 != 0 {
		flags = append(flags, "usage attributes <= threshold")
	}
	if bits&64 != 0 {
		flags = append(flags, "error log has records")
	}
	if bits&128 != 0 {
		flags = append(flags, "self-test log has errors")
	}
	return strings.Join(flags, ", ")
}

// runSmartctl runs smartctl with the given args and returns (output, exitCode, error).
// error is non-nil only for non-ExitError failures (missing binary, permissions, etc.).
func runSmartctl(smartctlPath string, args []string) ([]byte, int, error) {
	out, err := exec.Command(smartctlPath, args...).CombinedOutput()
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			return out, exitErr.ExitCode(), nil
		}
		return out, -1, err
	}
	return out, 0, nil
}

// fetchDriveInfo calls smartctl -a --json on a single device and parses the
// key fields we care about. Returns (info, inStandby). When inStandby is true,
// the drive was sleeping and no SMART data was collected.
func (dc *DriveCache) fetchDriveInfo(devicePath, protocol string, skipStandby bool) (DriveInfo, bool) {
	// Check the protocol cache: if we have a confirmed working protocol for this
	// device (e.g. "sat" from a previous SAT fallback, or a device_override),
	// use it upfront instead of relying on the scan-reported protocol.
	dc.mu.RLock()
	if cached, ok := dc.protocolCache[devicePath]; ok {
		protocol = cached
	}
	dc.mu.RUnlock()

	args := []string{"--json", "-a"}
	if dc.standbyMode != "never" && !skipStandby {
		args = append(args, "-n", dc.standbyMode)
	}
	if strings.EqualFold(protocol, "sat") {
		args = append(args, "-d", "sat")
	}
	args = append(args, devicePath)

	out, code, execErr := runSmartctl(dc.cfg.SmartctlPath, args)
	if execErr != nil {
		// Non-ExitError (binary missing, permissions, etc.)
		log.Printf("WARNING: smartctl -a %s: %v", devicePath, execErr)
	} else if code != 0 {
		// Bit 1 (value 2) with standby mode = drive is sleeping.
		if dc.standbyMode != "never" && code == 2 {
			return DriveInfo{DevicePath: devicePath, Protocol: protocol}, true
		}

		// SAT fallback: if any execution failure bits (0-2) are set and protocol
		// is SCSI, retry with -d sat. Bit 1 = device open failed (Synology),
		// bit 2 = command failed/checksum error (QNAP). Both indicate a protocol
		// mismatch on NAS HBAs where SATA drives present as SCSI.
		if code&0x07 != 0 && strings.EqualFold(protocol, "scsi") {
			satArgs := []string{"--json", "-a", "-d", "sat"}
			if dc.standbyMode != "never" && !skipStandby {
				satArgs = append(satArgs, "-n", dc.standbyMode)
			}
			satArgs = append(satArgs, devicePath)

			satOut, satCode, satExecErr := runSmartctl(dc.cfg.SmartctlPath, satArgs)
			if satExecErr == nil && satCode&0x07 == 0 {
				log.Printf("INFO: %s reports as SCSI but SAT succeeded -- using SAT for this drive", devicePath)
				dc.mu.Lock()
				dc.protocolCache[devicePath] = "sat"
				dc.mu.Unlock()
				out = satOut
				code = satCode
				protocol = "sat"
				// Fall through to normal parsing below with the SAT output.
			}
		}

		if code != 0 {
			execBits := code & 0x07
			healthBits := code & 0xF8

			// Bits 0-2: execution failures -- always log as WARNING.
			if execBits != 0 {
				log.Printf("WARNING: smartctl -a %s failed (exit code %d: %s)",
					devicePath, code, decodeExecBits(code))
			}

			// Bits 3-7: drive health flags -- log once, suppress repeats.
			if healthBits != 0 {
				dc.mu.RLock()
				prev := dc.lastHealthBits[devicePath]
				dc.mu.RUnlock()
				if healthBits != prev {
					log.Printf("smartctl -a %s: drive health flags (exit code %d: %s)",
						devicePath, code, decodeHealthBits(code))
					dc.mu.Lock()
					dc.lastHealthBits[devicePath] = healthBits
					dc.mu.Unlock()
				}
			}
		}
	}

	info := DriveInfo{
		DevicePath:  devicePath,
		Protocol:    protocol,
		LastUpdated: time.Now().UTC().Format(time.RFC3339),
		RawJSON:     json.RawMessage(out),
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

	return info, false
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
