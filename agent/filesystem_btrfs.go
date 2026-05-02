//go:build !windows

// Phase 1A: btrfs statvfs fallback.
//
// Some btrfs configurations (multi-device, certain kernel versions,
// near-full single-disk) cause syscall.Statfs to return zero values
// where df-style tools would report real numbers. When that happens,
// we fall back to parsing `btrfs filesystem usage --raw <path>`.
//
// This is a fallback, not the primary source. statvfs is microseconds;
// a subprocess is milliseconds and forks a child. We only invoke btrfs
// when statvfs has clearly failed (total==0 on a btrfs mount).
//
// Three failure modes, each with a distinct log line so users can
// diagnose without reading source:
//   - btrfs-progs not installed
//   - subprocess timed out (5s)
//   - output didn't parse
//
// All three fall through to the original statvfs values (zero); the
// /api/filesystems endpoint reports zeros and continues working.
package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"regexp"
	"strconv"
	"time"
)

// btrfsFallbackTimeout is the maximum wall time we'll allow the
// `btrfs filesystem usage --raw` subprocess. A hung btrfs binary
// must not block the agent's poll cycle.
const btrfsFallbackTimeout = 5 * time.Second

// Sentinel errors for the three documented failure modes. Callers
// distinguish via errors.Is to emit the right log message.
var (
	errBtrfsProgsMissing = errors.New("btrfs-progs not installed")
	errBtrfsTimeout      = errors.New("btrfs filesystem usage timed out")
	errBtrfsParse        = errors.New("btrfs filesystem usage parse error")
)

// btrfsUsage holds the three values we extract from --raw output.
type btrfsUsage struct {
	Total     uint64
	Used      uint64
	Available uint64
}

// Anchor patterns for the bare lines in the Overall: block of
// `btrfs filesystem usage --raw` output. Each must end after the
// digits to avoid colliding with the per-block-group lines such as
// "Data,single: Size:N, Used:N (62.40%)" -- those have "Used:" mid-line.
var (
	reBtrfsDeviceSize = regexp.MustCompile(`(?m)^\s*Device size:\s+(\d+)\s*$`)
	reBtrfsUsed       = regexp.MustCompile(`(?m)^\s*Used:\s+(\d+)\s*$`)
	// "Free (estimated):" has an optional trailing "(min: N)" parenthetical.
	// We only want the first integer; the "min:" value is conservative
	// scheduling info we don't expose.
	reBtrfsFreeEst = regexp.MustCompile(`(?m)^\s*Free \(estimated\):\s+(\d+)`)
)

// tryBtrfsFallback runs `btrfs filesystem usage --raw <path>` and
// parses the result. Returns the typed error sentinels documented
// above so the caller can log the three distinct messages.
func tryBtrfsFallback(path string) (btrfsUsage, error) {
	// Cheap pre-check: if the binary isn't even on PATH, fail fast
	// with the specific sentinel. exec.LookPath is microseconds.
	if _, err := exec.LookPath("btrfs"); err != nil {
		return btrfsUsage{}, errBtrfsProgsMissing
	}

	ctx, cancel := context.WithTimeout(context.Background(), btrfsFallbackTimeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, "btrfs", "filesystem", "usage", "--raw", path)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		// Distinguish timeout from other failures. A context-cancelled
		// CommandContext returns ctx.Err() via the Go stdlib.
		if ctx.Err() == context.DeadlineExceeded {
			return btrfsUsage{}, errBtrfsTimeout
		}
		// Any other run error (non-zero exit, permission denied,
		// disappeared mountpoint) is treated as a parse-class failure
		// from the caller's perspective. Wrap so the caller sees the
		// underlying cause if they choose to inspect it.
		return btrfsUsage{}, fmt.Errorf("%w: %v", errBtrfsParse, err)
	}

	return parseBtrfsUsageRaw(stdout.Bytes())
}

// parseBtrfsUsageRaw extracts Device size, Used, and Free (estimated)
// from the --raw output. Exposed (unexported but package-visible) for
// unit tests so we don't need a real btrfs binary to test parsing.
func parseBtrfsUsageRaw(out []byte) (btrfsUsage, error) {
	totalMatch := reBtrfsDeviceSize.FindSubmatch(out)
	usedMatch := reBtrfsUsed.FindSubmatch(out)
	freeMatch := reBtrfsFreeEst.FindSubmatch(out)

	if totalMatch == nil || usedMatch == nil {
		return btrfsUsage{}, fmt.Errorf("%w: missing Device size or Used line", errBtrfsParse)
	}

	total, err := strconv.ParseUint(string(totalMatch[1]), 10, 64)
	if err != nil {
		return btrfsUsage{}, fmt.Errorf("%w: Device size not numeric: %v", errBtrfsParse, err)
	}
	used, err := strconv.ParseUint(string(usedMatch[1]), 10, 64)
	if err != nil {
		return btrfsUsage{}, fmt.Errorf("%w: Used not numeric: %v", errBtrfsParse, err)
	}

	// Available is best-effort. If "Free (estimated):" is missing or
	// non-numeric, derive it from total-used. The endpoint contract
	// requires a value; an off-by-some on btrfs is acceptable given
	// btrfs's own Free estimation is itself an estimate.
	var available uint64
	if freeMatch != nil {
		if v, perr := strconv.ParseUint(string(freeMatch[1]), 10, 64); perr == nil {
			available = v
		}
	}
	if available == 0 && total > used {
		available = total - used
	}

	return btrfsUsage{
		Total:     total,
		Used:      used,
		Available: available,
	}, nil
}
