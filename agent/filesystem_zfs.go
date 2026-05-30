//go:build !windows

// Phase 1B: ZFS statvfs fallback.
//
// On some ZFS configurations (containers, certain pool layouts, legacy
// mount setups) syscall.Statfs returns zero values. When that happens,
// we fall back to parsing `zfs list -H -p -o used,avail,mountpoint`.
//
// This is a fallback, not the primary source. statvfs is microseconds;
// a subprocess is milliseconds. We only invoke zfs when statvfs has
// clearly failed (total==0 on a zfs mount).
//
// Three failure modes, each with a distinct log line:
//   - zfs binary not installed / not on PATH
//   - subprocess timed out (5s)
//   - output didn't parse (mountpoint not found, non-numeric field)
//
// All three fall through to the original statvfs values (zero).
package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

const zfsFallbackTimeout = 5 * time.Second

var (
	errZFSMissing = errors.New("zfs not installed")
	errZFSTimeout = errors.New("zfs list timed out")
	errZFSParse   = errors.New("zfs list parse error")
)

// zfsUsage holds the three values extracted from zfs list output.
type zfsUsage struct {
	Total     uint64
	Used      uint64
	Available uint64
}

// tryZFSFallback runs `zfs list -H -p -o used,avail,mountpoint -t filesystem`
// and finds the dataset whose mountpoint matches path. Returns the typed
// error sentinels so the caller can log distinct messages.
func tryZFSFallback(path string) (zfsUsage, error) {
	if _, err := exec.LookPath("zfs"); err != nil {
		return zfsUsage{}, errZFSMissing
	}

	ctx, cancel := context.WithTimeout(context.Background(), zfsFallbackTimeout)
	defer cancel()

	// -H: no header, tab-separated; -p: parseable byte values;
	// -t filesystem: excludes volumes and snapshots.
	cmd := exec.CommandContext(ctx, "zfs", "list", "-H", "-p",
		"-o", "used,avail,mountpoint", "-t", "filesystem")
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return zfsUsage{}, errZFSTimeout
		}
		return zfsUsage{}, fmt.Errorf("%w: %v", errZFSParse, err)
	}

	return parseZFSList(stdout.Bytes(), path)
}

// parseZFSList finds the tab-separated row whose mountpoint column matches
// the target mountpoint and returns used/avail values. Exposed for unit tests.
func parseZFSList(out []byte, mountpoint string) (zfsUsage, error) {
	for _, line := range strings.Split(string(bytes.TrimSpace(out)), "\n") {
		if line == "" {
			continue
		}
		fields := strings.Split(line, "\t")
		if len(fields) != 3 {
			continue
		}
		mp := strings.TrimSpace(fields[2])
		// Skip datasets with no real mountpoint (legacy, none, -).
		if mp != mountpoint {
			continue
		}

		used, err := strconv.ParseUint(strings.TrimSpace(fields[0]), 10, 64)
		if err != nil {
			return zfsUsage{}, fmt.Errorf("%w: used not numeric: %v", errZFSParse, err)
		}
		avail, err := strconv.ParseUint(strings.TrimSpace(fields[1]), 10, 64)
		if err != nil {
			return zfsUsage{}, fmt.Errorf("%w: avail not numeric: %v", errZFSParse, err)
		}

		return zfsUsage{
			Total:     used + avail,
			Used:      used,
			Available: avail,
		}, nil
	}

	return zfsUsage{}, fmt.Errorf("%w: mountpoint %s not found in zfs list output", errZFSParse, mountpoint)
}
