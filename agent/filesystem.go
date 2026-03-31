// Filesystem monitoring — reports disk usage for user-selected mountpoints.
// Configured via the installer's Disk Usage picker. If no filesystems are
// configured in config.yaml, the /api/filesystems endpoint is never registered
// and callers get a 404 (graceful degradation for older agents).
package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"sync"
	"syscall"
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
}

// FilesystemCache holds cached filesystem usage data. It is refreshed on
// the same interval as SMART data — no separate timer.
type FilesystemCache struct {
	mu          sync.RWMutex
	filesystems []FilesystemInfo
	configs     []FilesystemConfig
}

// NewFilesystemCache creates a cache for the given filesystem configurations.
func NewFilesystemCache(configs []FilesystemConfig) *FilesystemCache {
	return &FilesystemCache{
		configs: configs,
	}
}

// Refresh polls each configured mountpoint via statfs and updates the cache.
func (fc *FilesystemCache) Refresh() {
	results := make([]FilesystemInfo, 0, len(fc.configs))

	for _, cfg := range fc.configs {
		info := FilesystemInfo{
			ID:         makeFilesystemID(cfg.UUID, cfg.Path),
			UUID:       cfg.UUID,
			Mountpoint: cfg.Path,
			Device:     cfg.Device,
			FSType:     cfg.FSType,
		}

		var stat syscall.Statfs_t
		if err := syscall.Statfs(cfg.Path, &stat); err != nil {
			log.Printf("filesystem: statfs %s failed: %v", cfg.Path, err)
			info.Status = "unavailable"
			results = append(results, info)
			continue
		}

		// Total and available are straightforward. Used = total - free.
		// We use stat.Bfree (total free blocks including reserved) for
		// calculating used, and stat.Bavail (available to unprivileged
		// users) for the available_bytes field — matching df behavior.
		info.TotalBytes = stat.Blocks * uint64(stat.Bsize)
		freeBytes := stat.Bfree * uint64(stat.Bsize)
		info.UsedBytes = info.TotalBytes - freeBytes
		info.AvailableBytes = stat.Bavail * uint64(stat.Bsize)

		if info.TotalBytes > 0 {
			info.UsePercent = float64(info.UsedBytes) / float64(info.TotalBytes) * 100.0
			// Round to one decimal place.
			info.UsePercent = float64(int(info.UsePercent*10+0.5)) / 10.0
		}
		info.Status = "ok"

		results = append(results, info)
	}

	fc.mu.Lock()
	fc.filesystems = results
	fc.mu.Unlock()
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
