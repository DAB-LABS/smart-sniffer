// Filesystem monitoring — reports disk usage for user-selected mountpoints.
// Configured via the installer's Disk Usage picker. If no filesystems are
// configured in config.yaml, the /api/filesystems endpoint is never registered
// and callers get a 404 (graceful degradation for older agents).
package main

import (
	"crypto/sha1"
	"encoding/json"
	"fmt"
	"net/http"
	"path/filepath"
	"runtime"
	"sync"
)

// FilesystemInfo is the per-mountpoint payload returned by /api/filesystems.
type FilesystemInfo struct {
	ID             string  `json:"id"`
	UUID           string  `json:"uuid"`
	Mountpoint     string  `json:"mountpoint"`
	Device         string  `json:"device"`
	FSType         string  `json:"fstype"`
	TotalBytes     uint64  `json:"total_bytes"`
	UsedBytes      uint64  `json:"used_bytes"`
	AvailableBytes uint64  `json:"available_bytes"`
	UsePercent     float64 `json:"use_percent"`
	Status         string  `json:"status"` // "ok" or "unavailable"

	// macOS only (filesystem_physical.go): the whole physical disks the
	// filesystem lives on, and the /api/drives id of its drive when it is on
	// exactly one drive the agent reports. Omitted elsewhere and when unknown.
	PhysicalDevices []string `json:"physical_devices,omitempty"`
	DriveID         string   `json:"drive_id,omitempty"`
}

// MarshalJSON writes the api_version 1 shape (v1.0): when the filesystem
// could not be read (status "unavailable") its byte counts and use_percent
// are null, not 0, so "unknown" never reads as "empty". Read filesystems
// keep their numbers.
func (f FilesystemInfo) MarshalJSON() ([]byte, error) {
	type plain FilesystemInfo
	if f.Status == "ok" {
		return json.Marshal(plain(f))
	}
	return json.Marshal(struct {
		plain
		TotalBytes     *uint64  `json:"total_bytes"`
		UsedBytes      *uint64  `json:"used_bytes"`
		AvailableBytes *uint64  `json:"available_bytes"`
		UsePercent     *float64 `json:"use_percent"`
	}{plain: plain(f)})
}

// FilesystemCache holds cached filesystem usage data. It is refreshed on
// the same interval as SMART data — no separate timer.
type FilesystemCache struct {
	mu          sync.RWMutex
	filesystems []FilesystemInfo
	configs     []FilesystemConfig
	mountPrefix string       // host mount root when running in a container; "" = none
	logs        *logThrottle // suppresses repeated per-mount log lines (key = stat path)

	// Physical drive linking on macOS (filesystem_physical.go). Touched only
	// by Refresh. run, smartRun and drives are injectable for tests.
	goos         string
	run          cmdRunner
	smartRun     smartctlRunner
	smartctlPath string
	drives       func() []driveRef
	phys         map[string]*physEntry
	serials      map[string]*serialEntry
}

// NewFilesystemCache creates a cache for the given filesystem configurations.
// mountPrefix is the host mount root when the agent runs in a container (e.g.
// "/host"); pass "" when running directly on the host. verbose disables log
// suppression so every poll logs.
func NewFilesystemCache(configs []FilesystemConfig, mountPrefix string, verbose bool) *FilesystemCache {
	return &FilesystemCache{
		configs:     configs,
		mountPrefix: mountPrefix,
		logs:        newLogThrottle(logReminderInterval, verbose),
		goos:        runtime.GOOS,
		run:         runCommand,
		smartRun:    runSmartctlWithTimeout,
	}
}

// resolvePath maps a configured (host) mountpoint to the path the agent must
// stat. When the agent runs in a container with the host filesystem mounted
// under mountPrefix (e.g. "/host"), the real path is prefix+path. The reported
// mountpoint stays the host path. An empty prefix returns the path unchanged.
func (fc *FilesystemCache) resolvePath(p string) string {
	if fc.mountPrefix == "" {
		return p
	}
	return filepath.Join(fc.mountPrefix, p)
}

// HandleFilesystems serves GET /api/filesystems.
func (fc *FilesystemCache) HandleFilesystems(w http.ResponseWriter, r *http.Request) {
	fc.mu.RLock()
	data := fc.filesystems
	fc.mu.RUnlock()

	if data == nil {
		data = []FilesystemInfo{}
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(data)
}

// uniqueFilesystemIDs makes the ids in one list distinct (v1.0). Two
// configured filesystems can share an id: the id is "fs-" plus the first 8
// characters of the UUID, and btrfs subvolumes and bind mounts of one
// filesystem share its UUID. Home Assistant has always followed the first of
// them in the list (one entity per id, which reads the first match), so the
// first keeps the plain id and its entity never changes. Each later one gets
// "<id>-<6 hex of the SHA-1 of its mountpoint>", the same every run.
func uniqueFilesystemIDs(list []FilesystemInfo) {
	seen := make(map[string]bool, len(list))
	for i := range list {
		id := list[i].ID
		if seen[id] {
			sum := sha1.Sum([]byte(list[i].Mountpoint))
			id = fmt.Sprintf("%s-%x", id, sum[:3])
			for n := 2; seen[id]; n++ { // the same mountpoint twice
				id = fmt.Sprintf("%s-%x-%d", list[i].ID, sum[:3], n)
			}
			list[i].ID = id
		}
		seen[id] = true
	}
}

// makeFilesystemID creates a stable, URL-safe identifier for a filesystem.
// Uses the first 8 characters of the UUID (prefixed with "fs-"), falling
// back to a slugified mountpoint if UUID is empty.
func makeFilesystemID(uuid, mountpoint string) string {
	if uuid != "" && len(uuid) >= 8 {
		return fmt.Sprintf("fs-%s", uuid[:8])
	}
	// Fallback: slug from mountpoint.
	slug := makeDriveSlug("", mountpoint)
	return fmt.Sprintf("fs-%s", slug)
}
