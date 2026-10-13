// Host name and operating system name for /api/health.
//
// The OS name is read once at startup and cached: on macOS it costs two
// sw_vers runs and on Windows a registry read, and neither changes while the
// agent runs. The host name is read on every request so a rename shows up
// without a restart. Nothing here can fail /api/health: a value that cannot
// be read is left empty and the field is omitted.
package main

import (
	"bufio"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// systemInfo is the cached operating system description.
type systemInfo struct {
	Name    string // friendly name with version, e.g. "macOS 15.1" or "Ubuntu 24.04.1 LTS"
	Version string // version alone, e.g. "15.1" or "24.04"
}

// agentSystem is set once in RunAgent before the HTTP server starts and only
// read afterwards.
var agentSystem systemInfo

// cmdRunner runs a command with a timeout and returns its standard output.
// Injectable so tests never run sw_vers or diskutil.
type cmdRunner func(name string, args []string, timeout time.Duration) ([]byte, error)

// runCommand is the real cmdRunner.
func runCommand(name string, args []string, timeout time.Duration) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.WaitDelay = 2 * time.Second
	return cmd.Output()
}

// sysInfoTimeout bounds each command run to learn the OS name.
const sysInfoTimeout = 5 * time.Second

// shortHostname returns the host name without any domain suffix, the same
// value the mDNS TXT record carries. It is the machine's own name (inside a
// container, the container's), never mdns_name. Empty on error.
func shortHostname() string {
	h, err := os.Hostname()
	if err != nil {
		return ""
	}
	if i := strings.IndexByte(h, '.'); i != -1 {
		h = h[:i]
	}
	return h
}

// loadSystemInfo works out the OS name for the platform the agent runs on.
// mountPrefix is the host root when the agent runs in a container; on Linux
// the host's os-release under it is preferred over the container's own.
func loadSystemInfo(mountPrefix string) systemInfo {
	switch runtime.GOOS {
	case "darwin":
		return macOSInfo(runCommand)
	case "windows":
		return windowsInfo()
	default:
		return osReleaseInfo(mountPrefix)
	}
}

// osReleaseInfo reads os-release, trying the host's copy under mountPrefix
// first when one is set, then the agent's own.
func osReleaseInfo(mountPrefix string) systemInfo {
	var paths []string
	if mountPrefix != "" {
		paths = append(paths,
			filepath.Join(mountPrefix, "etc/os-release"),
			filepath.Join(mountPrefix, "usr/lib/os-release"))
	}
	paths = append(paths, "/etc/os-release", "/usr/lib/os-release")
	for _, p := range paths {
		data, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		if info := parseOSRelease(string(data)); info.Name != "" {
			return info
		}
	}
	return systemInfo{}
}

// parseOSRelease takes PRETTY_NAME as the name (or NAME plus VERSION when
// PRETTY_NAME is missing) and VERSION_ID as the version.
func parseOSRelease(content string) systemInfo {
	vals := map[string]string{}
	sc := bufio.NewScanner(strings.NewReader(content))
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		v = strings.TrimSpace(v)
		if len(v) >= 2 && (v[0] == '"' || v[0] == '\'') && v[len(v)-1] == v[0] {
			v = v[1 : len(v)-1]
		}
		vals[strings.TrimSpace(k)] = v
	}
	info := systemInfo{Name: vals["PRETTY_NAME"], Version: vals["VERSION_ID"]}
	if info.Name == "" {
		info.Name = strings.TrimSpace(vals["NAME"] + " " + vals["VERSION"])
	}
	return info
}

// macOSInfo asks sw_vers for the product name and version, e.g. "macOS" and
// "15.1". Either part missing leaves what could be read.
func macOSInfo(run cmdRunner) systemInfo {
	read := func(flag string) string {
		out, err := run("sw_vers", []string{flag}, sysInfoTimeout)
		if err != nil {
			return ""
		}
		return strings.TrimSpace(string(out))
	}
	name, ver := read("-productName"), read("-productVersion")
	return systemInfo{Name: strings.TrimSpace(name + " " + ver), Version: ver}
}

// windowsProductName builds the friendly Windows name from the registry
// values under HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion. Windows 11
// still says "Windows 10" in ProductName; build 22000 and later is Windows 11.
func windowsProductName(product, displayVersion, build string) systemInfo {
	product = strings.TrimSpace(product)
	if n := atoiPrefix(build); n >= 22000 && strings.HasPrefix(product, "Windows 10") {
		product = "Windows 11" + strings.TrimPrefix(product, "Windows 10")
	}
	ver := strings.TrimSpace(displayVersion)
	if ver == "" && build != "" {
		ver = "build " + build
	}
	if product == "" {
		return systemInfo{}
	}
	return systemInfo{Name: strings.TrimSpace(product + " " + ver), Version: ver}
}

// atoiPrefix parses the leading digits of s, 0 if there are none.
func atoiPrefix(s string) int {
	n := 0
	for _, c := range strings.TrimSpace(s) {
		if c < '0' || c > '9' {
			break
		}
		n = n*10 + int(c-'0')
	}
	return n
}
