// smartctl_resolve.go -- finding the smartctl binary at startup.
//
// Order: the smartctl_path config key when set, then PATH, then a list of
// known install locations for this platform. On Windows the agent runs as
// a service under the SYSTEM account, which sees only the system PATH, so
// a smartctl installed per user (Scoop, winget portable) is invisible to
// the PATH lookup and has to be found by the known-locations search
// (GH #60).
package main

import (
	"errors"
	"fmt"
	"io/fs"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
)

// smartctlSearchPaths lists known installation locations for smartctl on
// Linux, macOS, BSD and NAS platforms. Checked in order when the PATH
// version is missing or too old. Windows uses windowsSmartctlCandidates.
// See docs/internal/research/smartctl-install-paths.md for full research.
var smartctlSearchPaths = []string{
	// NAS platforms (most likely to need fallback)
	"/var/packages/synocli-disk/target/sbin/smartctl",        // SynoCommunity on Synology
	"/opt/sbin/smartctl",                                     // Entware (Synology/QNAP)
	"/opt/bin/smartctl",                                      // Entware alternate
	"/boot/extra/sbin/smartctl",                              // Unraid NerdTools
	"/boot/extra/bin/smartctl",                               // Unraid NerdTools alternate
	"/share/CACHEDEV1_DATA/.qpkg/smartmontools/bin/smartctl", // QNAP QPKG

	// Standard Linux
	"/usr/sbin/smartctl", // Debian, Ubuntu, RHEL, Fedora, Arch, Alpine, Proxmox, OMV

	// BSD / TrueNAS CORE
	"/usr/local/sbin/smartctl", // FreeBSD, OpenBSD

	// macOS
	"/usr/local/bin/smartctl",    // Homebrew (Intel)
	"/opt/homebrew/bin/smartctl", // Homebrew (Apple Silicon)
	"/opt/local/sbin/smartctl",   // MacPorts

	// NixOS
	"/run/current-system/sw/sbin/smartctl", // NixOS system profile
}

// serviceNotice forwards a startup message to the platform's service log.
// It is a no-op except on Windows, where main_windows.go points it at the
// Event Log while running under the SCM: a service's stdout goes nowhere,
// so without this the smartctl lines below would never be seen there.
var serviceNotice = func(warning bool, msg string) {}

// windowsSmartctlCandidates returns the places smartctl.exe is usually
// installed on Windows, in search order. getenv and glob are injected so
// the list can be tested on any OS; production passes os.Getenv and
// filepath.Glob. Paths are built with backslashes whatever the host OS.
//
// The service runs as SYSTEM, whose own profile and environment say
// nothing about the user who installed smartmontools, so per-user
// locations are found by looking in every profile under the Users folder.
func windowsSmartctlCandidates(getenv func(string) string, glob func(string) ([]string, error)) []string {
	envOr := func(key, fallback string) string {
		if v := strings.TrimRight(getenv(key), `\`); v != "" {
			return v
		}
		return fallback
	}
	sysDrive := envOr("SystemDrive", "C:")
	programFiles := envOr("ProgramFiles", sysDrive+`\Program Files`)
	programFilesX86 := envOr("ProgramFiles(x86)", sysDrive+`\Program Files (x86)`)
	programData := envOr("ProgramData", sysDrive+`\ProgramData`)
	usersDir := sysDrive + `\Users`

	var out []string
	seen := make(map[string]bool)
	add := func(p string) {
		key := strings.ToLower(p)
		if !seen[key] {
			seen[key] = true
			out = append(out, p)
		}
	}
	addGlob := func(pattern string) {
		matches, err := glob(pattern)
		if err != nil {
			return
		}
		sort.Strings(matches)
		for _, m := range matches {
			add(m)
		}
	}
	scoopRoot := func(root string) {
		add(root + `\apps\smartmontools\current\bin\smartctl.exe`)
		add(root + `\shims\smartctl.exe`)
	}

	// Official installer. The winget and Chocolatey packages run it too.
	add(programFiles + `\smartmontools\bin\smartctl.exe`)
	add(programFilesX86 + `\smartmontools\bin\smartctl.exe`)

	// Chocolatey shims.
	if choco := strings.TrimRight(getenv("ChocolateyInstall"), `\`); choco != "" {
		add(choco + `\bin\smartctl.exe`)
	}
	add(programData + `\chocolatey\bin\smartctl.exe`)

	// winget portable installs, machine scope.
	add(programFiles + `\WinGet\Links\smartctl.exe`)

	// Scoop, global installs (scoop install -g).
	if g := strings.TrimRight(getenv("SCOOP_GLOBAL"), `\`); g != "" {
		scoopRoot(g)
	}
	scoopRoot(programData + `\scoop`)

	// Scoop, per-user installs: $SCOOP when the agent runs as that user,
	// then the default scoop folder in every profile.
	if u := strings.TrimRight(getenv("SCOOP"), `\`); u != "" {
		scoopRoot(u)
	}
	addGlob(usersDir + `\*\scoop\apps\smartmontools\current\bin\smartctl.exe`)
	addGlob(usersDir + `\*\scoop\shims\smartctl.exe`)

	// winget portable installs, user scope.
	addGlob(usersDir + `\*\AppData\Local\Microsoft\WinGet\Links\smartctl.exe`)

	return out
}

// baseName returns the last element of p, treating both / and \ as
// separators so Windows paths are handled on any host OS.
func baseName(p string) string {
	if i := strings.LastIndexAny(p, `/\`); i >= 0 {
		return p[i+1:]
	}
	return p
}

// isExecutableFor reports whether a regular file counts as runnable on
// goos. Windows has no execute bits; os.Stat reports any file there as
// mode 0666, so the extension decides instead.
func isExecutableFor(goos, path string, info os.FileInfo) bool {
	if goos == "windows" {
		ext := strings.ToLower(filepath.Ext(baseName(path)))
		return ext == ".exe" || ext == ".com"
	}
	return info.Mode()&0o111 != 0
}

// smartctlResolver holds what resolveSmartctlPath needs from the outside
// world, so tests can replace each piece.
type smartctlResolver struct {
	goos       string
	lookPath   func(file string) (string, error)
	stat       func(name string) (os.FileInfo, error)
	version    func(path string) string
	candidates func() []string
	logf       func(format string, args ...interface{})
	notice     func(warning bool, msg string)
}

func newSmartctlResolver() *smartctlResolver {
	return &smartctlResolver{
		goos:     runtime.GOOS,
		lookPath: exec.LookPath,
		stat:     os.Stat,
		version:  getSmartctlVersion,
		candidates: func() []string {
			if runtime.GOOS == "windows" {
				return windowsSmartctlCandidates(os.Getenv, filepath.Glob)
			}
			return smartctlSearchPaths
		},
		logf:   log.Printf,
		notice: func(warning bool, msg string) { serviceNotice(warning, msg) },
	}
}

// resolveSmartctlPath finds the smartctl binary to use. A smartctl_path
// from the config wins when it works; when it does not, the reason is
// logged and the normal search runs: PATH first, then known
// platform-specific paths. Returns the full path and version string, or
// an error if no usable binary is found.
func resolveSmartctlPath(configured, minVersion string) (string, string, error) {
	return newSmartctlResolver().resolve(configured, minVersion)
}

// check reports whether p is a usable smartctl. It returns the path to run
// (on Windows ".exe" is added when p has no extension and only p.exe
// exists), its version, and an empty reason on success; otherwise reason
// says what is wrong in words fit for a log line.
func (r *smartctlResolver) check(p, minVersion string) (path, ver, reason string) {
	info, err := r.stat(p)
	if err != nil && r.goos == "windows" && filepath.Ext(baseName(p)) == "" {
		if exeInfo, exeErr := r.stat(p + ".exe"); exeErr == nil {
			p, info, err = p+".exe", exeInfo, nil
		}
	}
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return p, "", "file not found"
		}
		return p, "", err.Error()
	}
	if info.IsDir() {
		return p, "", "it is a folder, not the smartctl program"
	}
	if !isExecutableFor(r.goos, p, info) {
		if r.goos == "windows" {
			return p, "", "not an .exe file"
		}
		return p, "", "file is not executable"
	}
	ver = r.version(p)
	if ver == "" {
		return p, "", "running it with --version failed or printed no smartctl version"
	}
	if !isSmartctlVersionOK(ver, minVersion) {
		return p, ver, fmt.Sprintf("version %s is older than the required %s", ver, minVersion)
	}
	return p, ver, ""
}

func (r *smartctlResolver) resolve(configured, minVersion string) (string, string, error) {
	// 0. smartctl_path from config.yaml.
	configNote := ""
	configured = strings.TrimSpace(configured)
	if configured != "" {
		p, ver, reason := r.check(configured, minVersion)
		if reason == "" {
			r.logf("using smartctl_path from config: %s", p)
			return p, ver, nil
		}
		// The path is quoted as typed, not with %q: %q would double every
		// backslash in a Windows path.
		// The log line says ERROR: it is a mistake in config.yaml to fix.
		// The Event Log entry (event 103) is already a Warning, so its
		// text carries no prefix.
		msg := fmt.Sprintf(`smartctl_path "%s" in config cannot be used: %s. Searching PATH and known locations instead.`, configured, reason)
		r.logf("ERROR: %s", msg)
		r.notice(true, msg)
		configNote = fmt.Sprintf("\nsmartctl_path \"%s\" in config was not used: %s.\n", configured, reason)
	}

	// 1. Try PATH first (current behavior for most users).
	pathBin, err := r.lookPath("smartctl")
	if err == nil {
		ver := r.version(pathBin)
		if isSmartctlVersionOK(ver, minVersion) {
			return pathBin, ver, nil
		}
		r.logf("smartctl in PATH is %s (requires %s+), searching known paths...", ver, minVersion)
	}

	// 2. Search known platform-specific paths.
	for _, candidate := range r.candidates() {
		p, ver, reason := r.check(candidate, minVersion)
		if reason != "" {
			continue
		}
		r.logf("found smartctl %s at %s", ver, p)
		return p, ver, nil
	}

	// 3. Nothing found.
	if pathBin != "" {
		return "", "", fmt.Errorf(`smartctl found in PATH but version is too old.
The agent requires smartctl %s or newer for JSON output support.

The smartctl in your PATH is at: %s
%s
Install a newer version:
  Linux (Debian/Ubuntu):  sudo apt install smartmontools
  Linux (RHEL/Fedora):    sudo dnf install smartmontools
  macOS (Homebrew):       brew install smartmontools
  Synology:               Install SynoCli Disk Tools from SynoCommunity
  QNAP:                   Install smartmontools via Entware (opkg install smartmontools)

Run 'smartctl --version' to check your current version.`, minVersion, pathBin, configNote)
	}

	return "", "", fmt.Errorf(`smartctl not found in PATH or known locations.
smartmontools is required for SMART Sniffer to function.
%s
Install it for your platform:
  Linux (Debian/Ubuntu):  sudo apt install smartmontools
  Linux (RHEL/Fedora):    sudo dnf install smartmontools
  macOS (Homebrew):       brew install smartmontools
  Windows:                winget install smartmontools (or choco install smartmontools)
  Synology:               Install SynoCli Disk Tools from SynoCommunity
  QNAP:                   Install smartmontools via Entware (opkg install smartmontools)

If smartctl is installed somewhere else, set smartctl_path in config.yaml
to its full path.

More info: https://www.smartmontools.org/wiki/Download
`, configNote)
}
