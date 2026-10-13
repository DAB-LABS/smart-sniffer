//go:build windows

package main

import (
	"golang.org/x/sys/windows/registry"
)

// windowsInfo reads the product name, display version (e.g. "24H2") and
// build number from the registry. Empty on any error.
func windowsInfo() systemInfo {
	k, err := registry.OpenKey(registry.LOCAL_MACHINE,
		`SOFTWARE\Microsoft\Windows NT\CurrentVersion`, registry.QUERY_VALUE)
	if err != nil {
		return systemInfo{}
	}
	defer k.Close()
	get := func(name string) string {
		v, _, err := k.GetStringValue(name)
		if err != nil {
			return ""
		}
		return v
	}
	display := get("DisplayVersion")
	if display == "" {
		display = get("ReleaseId") // before Windows 10 20H2
	}
	return windowsProductName(get("ProductName"), display, get("CurrentBuild"))
}
