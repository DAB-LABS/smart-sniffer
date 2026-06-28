//go:build windows

package main

import (
	"fmt"
	"log"

	"golang.org/x/sys/windows"
)

// Refresh polls each configured path via GetDiskFreeSpaceEx and updates
// the cache.  This is the Windows equivalent of the Unix statfs-based
// implementation in filesystem_unix.go.
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

		pathPtr, err := windows.UTF16PtrFromString(cfg.Path)
		if err != nil {
			if msg := fmt.Sprintf("filesystem: invalid path %s: %v", cfg.Path, err); fc.logs.shouldLog(cfg.Path, msg) {
				log.Print(msg)
			}
			info.Status = "unavailable"
			results = append(results, info)
			continue
		}

		var freeBytesAvailable, totalBytes, totalFreeBytes uint64
		err = windows.GetDiskFreeSpaceEx(pathPtr, &freeBytesAvailable, &totalBytes, &totalFreeBytes)
		if err != nil {
			if msg := fmt.Sprintf("filesystem: GetDiskFreeSpaceEx %s failed: %v", cfg.Path, err); fc.logs.shouldLog(cfg.Path, msg) {
				log.Print(msg)
			}
			info.Status = "unavailable"
			results = append(results, info)
			continue
		}

		info.TotalBytes = totalBytes
		info.UsedBytes = totalBytes - totalFreeBytes
		info.AvailableBytes = freeBytesAvailable
		// Use df semantics for parity with the unix path: used /
		// (used + available). Windows has no ext4-style reserved blocks, so
		// this is normally equal to used/total, but keeping one formula
		// across platforms avoids divergence. See issue #39.
		if denom := info.UsedBytes + info.AvailableBytes; denom > 0 {
			info.UsePercent = float64(info.UsedBytes) / float64(denom) * 100.0
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
