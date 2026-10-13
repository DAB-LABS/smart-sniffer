//go:build !windows

package main

// windowsInfo is only meaningful on Windows.
func windowsInfo() systemInfo { return systemInfo{} }
