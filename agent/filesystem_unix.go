//go:build !windows

package main

import (
	"errors"
	"fmt"
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

		// statPath is the path the agent actually stats. It equals cfg.Path
		// on a host install, or mountPrefix+cfg.Path when running in a
		// container (e.g. /host/DATA). The reported Mountpoint stays cfg.Path.
		statPath := fc.resolvePath(cfg.Path)

		var stat syscall.Statfs_t
		if err := syscall.Statfs(statPath, &stat); err != nil {
			if msg := fmt.Sprintf("filesystem: statfs %s failed: %v", statPath, err); fc.logs.shouldLog(statPath, msg) {
				log.Print(msg)
			}
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

		// Phase 1A: btrfs statvfs fallback.
		//
		// We trigger fallback only when TotalBytes == 0 on a btrfs mount.
		// We do NOT broaden the trigger to "implausible non-zero" cases
		// (e.g. btrfs single-disk near-full overstating free). That would
		// fork a subprocess on every poll cycle for every btrfs mount,
		// which is wasteful. The CTO's panel point that btrfs CLI is the
		// more reliable source still stands -- this is a deliberate
		// performance/reliability tradeoff. See plan-btrfs-filesystem-
		// reporting.md for the full reasoning.
		if info.TotalBytes == 0 && cfg.FSType == "btrfs" {
			usage, err := tryBtrfsFallback(statPath)
			var msg string
			switch {
			case err == nil:
				info.TotalBytes = usage.Total
				info.UsedBytes = usage.Used
				info.AvailableBytes = usage.Available
				msg = fmt.Sprintf("filesystem: using btrfs-progs for %s (statvfs returned zero)", statPath)
			case errors.Is(err, errBtrfsProgsMissing):
				msg = fmt.Sprintf("filesystem: btrfs-progs not installed, returning statvfs zeros for %s", statPath)
			case errors.Is(err, errBtrfsTimeout):
				msg = fmt.Sprintf("filesystem: btrfs filesystem usage timed out after 5s for %s", statPath)
			default:
				// Wraps errBtrfsParse or an exec error treated as parse-class.
				msg = fmt.Sprintf("filesystem: btrfs filesystem usage parse error for %s: %v", statPath, err)
			}
			if fc.logs.shouldLog(statPath, msg) {
				log.Print(msg)
			}
		}

		// Use df semantics: used / (used + available). AvailableBytes is
		// Bavail, which already excludes reserved blocks (ext4 default 5%),
		// so this matches what `df` reports and what users actually
		// experience. Computing used/total understates usage by the
		// reserved fraction. See issue #39.
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
