//go:build !windows

// ZFS filesystem fallback.
//
// statfs on a ZFS dataset mountpoint reports values scoped to that dataset,
// not the pool. A parent dataset such as /rpool holds almost no data directly
// (all the real data lives in child datasets and zvols), so statfs reports
// near-zero usage while the pool is actually full. See issue #31.
//
// We correct this with `zfs list -Hp -o used,available <path>`. ZFS "used" is
// recursive (it includes child datasets and snapshots), so on a pool root it
// reflects real consumption, and "available" is usable free space (post-parity
// on RAID-Z). Total = used + available, which matches what users see in
// `pvesm status` / `df` for the dataset and excludes raw parity overhead.
//
// This is the same shape as the btrfs fallback: a LookPath pre-check, a
// timeout-bounded subprocess, a parser unit-tested against captured output,
// and the shared errFS* sentinels.
package main

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
)

// zfsResolve returns pool-level usage for the dataset that owns path by
// querying `zfs list`. Returns the shared sentinels on tool-missing, timeout,
// or parse failure.
func zfsResolve(path string) (fsUsage, error) {
	// zfs/zpool are always present on a real ZFS system, so a missing binary
	// means this mount is not actually ZFS (or the tools are unavailable).
	if _, err := exec.LookPath("zfs"); err != nil {
		return fsUsage{}, errFSToolMissing
	}

	ctx, cancel := context.WithTimeout(context.Background(), fsFallbackTimeout)
	defer cancel()

	// -H: no header, -p: exact byte values. ZFS accepts a path operand that
	// resolves to the owning dataset, so no explicit pool-name extraction is
	// needed.
	cmd := exec.CommandContext(ctx, "zfs", "list", "-Hp", "-o", "used,available", path)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if ctx.Err() == context.DeadlineExceeded {
			return fsUsage{}, errFSTimeout
		}
		// Non-zero exit, permission denied, dataset gone, etc. Treat as
		// parse-class so the dispatcher keeps the statfs values.
		return fsUsage{}, fmt.Errorf("%w: %v", errFSParse, err)
	}

	return parseZFSList(stdout.Bytes())
}

// parseZFSList parses one line of `zfs list -Hp -o used,available` output:
// two tab-separated exact-byte integers, "used\tavailable". Total is derived
// as used + available. Exposed (unexported but package-visible) so the parser
// can be unit-tested without a real ZFS system.
func parseZFSList(out []byte) (fsUsage, error) {
	line := strings.TrimSpace(string(out))
	if line == "" {
		return fsUsage{}, fmt.Errorf("%w: empty zfs list output", errFSParse)
	}
	// If a path resolves to multiple datasets (should not happen with a single
	// path operand), take the first data line.
	if idx := strings.IndexByte(line, '\n'); idx >= 0 {
		line = strings.TrimSpace(line[:idx])
	}

	fields := strings.Split(line, "\t")
	if len(fields) < 2 {
		return fsUsage{}, fmt.Errorf("%w: expected used and available, got %q", errFSParse, line)
	}

	used, err := strconv.ParseUint(fields[0], 10, 64)
	if err != nil {
		return fsUsage{}, fmt.Errorf("%w: used not numeric: %v", errFSParse, err)
	}
	available, err := strconv.ParseUint(fields[1], 10, 64)
	if err != nil {
		return fsUsage{}, fmt.Errorf("%w: available not numeric: %v", errFSParse, err)
	}

	return fsUsage{
		Total:     used + available,
		Used:      used,
		Available: available,
	}, nil
}
