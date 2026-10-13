//go:build !darwin && !windows

package main

import "syscall"

// mountSource is only read on macOS; elsewhere the configured device is used.
func mountSource(*syscall.Statfs_t) string { return "" }
