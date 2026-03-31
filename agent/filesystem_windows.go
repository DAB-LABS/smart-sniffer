//go:build windows

package main

import "log"

// Refresh on Windows is a stub — syscall.Statfs is not available.
// Filesystem monitoring is effectively unsupported on Windows for now.
// All configured mountpoints are reported as "unavailable".
func (fc *FilesystemCache) Refresh() {
	results := make([]FilesystemInfo, 0, len(fc.configs))

	for _, cfg := range fc.configs {
		log.Printf("filesystem: statfs not supported on Windows for %s", cfg.Path)
		results = append(results, FilesystemInfo{
			ID:         makeFilesystemID(cfg.UUID, cfg.Path),
			UUID:       cfg.UUID,
			Mountpoint: cfg.Path,
			Device:     cfg.Device,
			FSType:     cfg.FSType,
			Status:     "unavailable",
		})
	}

	fc.mu.Lock()
	fc.filesystems = results
	fc.mu.Unlock()
}
