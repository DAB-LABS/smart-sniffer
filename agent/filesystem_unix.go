//go:build !windows

package main

import (
	"log"
	"syscall"
)

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
