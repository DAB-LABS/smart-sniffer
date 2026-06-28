//go:build !windows

// Advanced filesystem fallback framework.
//
// Some filesystems return misleading numbers from statfs: btrfs multi-device
// reports a zero total, ZFS reports dataset-scoped usage instead of the pool,
// and so on. Each such filesystem registers an fsFallback that shells out to
// the native CLI and returns a corrected byte triple. The dispatcher runs at
// most one fallback per mount, after statfs, and the existing percent block
// (used/(used+available), issue #39) computes UsePercent afterward.
//
// This file is the unix-side registry: the fsFallbacks slice names resolver
// functions that only exist on non-Windows builds, so it carries the same
// build constraint. Windows has no Phase-1 fallbacks.
package main

import (
	"errors"
	"fmt"
	"log"
	"strings"
	"time"
)

// fsFallbackTimeout bounds any single fallback subprocess. A wedged CLI tool
// must not stall the agent's poll loop. Shared by every resolver.
const fsFallbackTimeout = 5 * time.Second

// Shared sentinels for the three failure modes every fallback can hit. Each
// resolver returns one of these (directly, or translated from its own
// filesystem-specific error) so the dispatcher can log a consistent message.
var (
	errFSToolMissing = errors.New("filesystem CLI tool not installed")
	errFSTimeout     = errors.New("filesystem usage query timed out")
	errFSParse       = errors.New("filesystem usage parse error")
)

// fsUsage is the corrected byte triple a fallback returns.
type fsUsage struct {
	Total     uint64
	Used      uint64
	Available uint64
}

// fsFallback corrects statfs misreporting for one filesystem type.
type fsFallback struct {
	fstype  string                             // matched against cfg.FSType (case-insensitive)
	trigger func(info FilesystemInfo) bool     // when to invoke
	resolve func(path string) (fsUsage, error) // run native CLI, return corrected usage
}

// fsFallbacks is the ordered registry. The first entry whose fstype matches
// and whose trigger fires wins; one fallback runs per mount.
var fsFallbacks = []fsFallback{
	{fstype: "btrfs", trigger: triggerTotalZero, resolve: btrfsResolve},
	{fstype: "zfs", trigger: triggerAlways, resolve: zfsResolve},
}

// triggerTotalZero fires only when statfs clearly failed (total == 0). Used by
// btrfs so a healthy btrfs mount does not fork a subprocess every cycle.
func triggerTotalZero(i FilesystemInfo) bool { return i.TotalBytes == 0 }

// triggerAlways fires every cycle. Used by ZFS, whose statfs numbers are
// non-zero but wrong in a way that cannot be detected numerically.
func triggerAlways(i FilesystemInfo) bool { return true }

// applyFilesystemFallback runs the first matching fallback for fstype and, on
// success, overwrites the byte fields of info in place. It logs through the
// cache's throttle so recurring corrections (ZFS fires every poll) do not spam
// the log. UsePercent is intentionally not touched here; the caller's percent
// block recomputes it from the corrected bytes.
func (fc *FilesystemCache) applyFilesystemFallback(info *FilesystemInfo, fstype, statPath string) {
	for _, fb := range fsFallbacks {
		if !strings.EqualFold(fb.fstype, fstype) || !fb.trigger(*info) {
			continue
		}

		usage, err := fb.resolve(statPath)
		var msg string
		switch {
		case err == nil:
			info.TotalBytes, info.UsedBytes, info.AvailableBytes = usage.Total, usage.Used, usage.Available
			msg = fmt.Sprintf("filesystem: corrected %s usage for %s via fallback", fb.fstype, statPath)
		case errors.Is(err, errFSToolMissing):
			msg = fmt.Sprintf("filesystem: %s tools not installed for %s; reporting statfs values", fb.fstype, statPath)
		case errors.Is(err, errFSTimeout):
			msg = fmt.Sprintf("filesystem: %s usage query timed out for %s", fb.fstype, statPath)
		default:
			msg = fmt.Sprintf("filesystem: %s usage parse error for %s: %v", fb.fstype, statPath, err)
		}
		if fc.logs.shouldLog(statPath, msg) {
			log.Print(msg)
		}
		break // one fallback per mount
	}
}
