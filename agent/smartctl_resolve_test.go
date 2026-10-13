package main

import (
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

// fakeFile is an os.FileInfo for the resolver's injected stat.
type fakeFile struct {
	name string
	mode fs.FileMode
}

func (f fakeFile) Name() string       { return f.name }
func (f fakeFile) Size() int64        { return 1 }
func (f fakeFile) Mode() fs.FileMode  { return f.mode }
func (f fakeFile) ModTime() time.Time { return time.Time{} }
func (f fakeFile) IsDir() bool        { return f.mode.IsDir() }
func (f fakeFile) Sys() interface{}   { return nil }

// fakeResolver builds a resolver over an in-memory file table. files maps
// a path to its mode; versions maps a path to what --version reports.
type fakeResolver struct {
	*smartctlResolver
	logs    []string
	notices []string
}

func newFakeResolver(goos string, files map[string]fs.FileMode, versions map[string]string, onPath string, candidates []string) *fakeResolver {
	fr := &fakeResolver{}
	fr.smartctlResolver = &smartctlResolver{
		goos: goos,
		lookPath: func(string) (string, error) {
			if onPath == "" {
				return "", errors.New("executable file not found in $PATH")
			}
			return onPath, nil
		},
		stat: func(name string) (os.FileInfo, error) {
			mode, ok := files[name]
			if !ok {
				return nil, &fs.PathError{Op: "stat", Path: name, Err: fs.ErrNotExist}
			}
			return fakeFile{name: baseName(name), mode: mode}, nil
		},
		version:    func(p string) string { return versions[p] },
		candidates: func() []string { return candidates },
		logf: func(format string, args ...interface{}) {
			fr.logs = append(fr.logs, fmt.Sprintf(format, args...))
		},
		notice: func(warning bool, msg string) {
			if warning {
				fr.notices = append(fr.notices, msg)
			}
		},
	}
	return fr
}

func TestLoadConfigSmartctlPath(t *testing.T) {
	load := func(t *testing.T, yaml string) *Config {
		t.Helper()
		path := filepath.Join(t.TempDir(), "config.yaml")
		if err := os.WriteFile(path, []byte(yaml), 0o644); err != nil {
			t.Fatal(err)
		}
		savedArgs, savedFlags := os.Args, flag.CommandLine
		t.Cleanup(func() { os.Args, flag.CommandLine = savedArgs, savedFlags })
		flag.CommandLine = flag.NewFlagSet("smartha-agent", flag.ContinueOnError)
		os.Args = []string{"smartha-agent", "--config", path}
		cfg, err := LoadConfig()
		if err != nil {
			t.Fatal(err)
		}
		return cfg
	}

	if got := load(t, "port: 9099\n").SmartctlPathConfig; got != "" {
		t.Errorf("unset smartctl_path = %q, want empty", got)
	}
	cfg := load(t, "smartctl_path: /usr/local/bin/smartctl\n")
	if cfg.SmartctlPathConfig != "/usr/local/bin/smartctl" {
		t.Errorf("smartctl_path = %q", cfg.SmartctlPathConfig)
	}
	if cfg.SmartctlPath != "" {
		t.Errorf("SmartctlPath is the resolved path and must not be read from the file, got %q", cfg.SmartctlPath)
	}
	// The form from GH #60: a double-quoted Windows path with escaped backslashes.
	win := load(t, `smartctl_path: "C:\\Users\\me\\scoop\\apps\\smartmontools\\current\\bin\\smartctl.exe"`+"\n")
	if want := `C:\Users\me\scoop\apps\smartmontools\current\bin\smartctl.exe`; win.SmartctlPathConfig != want {
		t.Errorf("windows smartctl_path = %q, want %q", win.SmartctlPathConfig, want)
	}
	// The form install.ps1 writes: single-quoted, backslashes literal, quote doubled.
	inst := load(t, `smartctl_path: 'C:\Users\o''brien\scoop\apps\smartmontools\current\bin\smartctl.exe'`+"\n")
	if want := `C:\Users\o'brien\scoop\apps\smartmontools\current\bin\smartctl.exe`; inst.SmartctlPathConfig != want {
		t.Errorf("installer smartctl_path = %q, want %q", inst.SmartctlPathConfig, want)
	}

	b, err := os.ReadFile("config.yaml.example")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), "\n# smartctl_path: ") {
		t.Error("config.yaml.example lacks the commented smartctl_path line")
	}
}

func TestResolveSmartctlConfiguredValid(t *testing.T) {
	r := newFakeResolver("linux",
		map[string]fs.FileMode{"/opt/custom/smartctl": 0o755},
		map[string]string{"/opt/custom/smartctl": "7.4", "/usr/sbin/smartctl": "7.3"},
		"/usr/sbin/smartctl", nil)
	p, ver, err := r.resolve("/opt/custom/smartctl", "7.0")
	if err != nil || p != "/opt/custom/smartctl" || ver != "7.4" {
		t.Fatalf("got %q %q %v", p, ver, err)
	}
	if len(r.logs) != 1 || r.logs[0] != "using smartctl_path from config: /opt/custom/smartctl" {
		t.Errorf("logs = %q", r.logs)
	}
	if len(r.notices) != 0 {
		t.Errorf("unexpected warnings %q", r.notices)
	}
}

func TestResolveSmartctlConfiguredInvalidFallsBack(t *testing.T) {
	cases := []struct {
		name   string
		files  map[string]fs.FileMode
		vers   map[string]string
		reason string
	}{
		{"missing", nil, nil, "file not found"},
		{"folder", map[string]fs.FileMode{"/opt/custom/smartctl": fs.ModeDir | 0o755}, nil, "it is a folder"},
		{"not executable", map[string]fs.FileMode{"/opt/custom/smartctl": 0o644}, nil, "file is not executable"},
		{"no version", map[string]fs.FileMode{"/opt/custom/smartctl": 0o755}, nil, "--version failed"},
		{"too old", map[string]fs.FileMode{"/opt/custom/smartctl": 0o755},
			map[string]string{"/opt/custom/smartctl": "6.5"}, "version 6.5 is older than the required 7.0"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			vers := map[string]string{"/usr/sbin/smartctl": "7.4"}
			for k, v := range tc.vers {
				vers[k] = v
			}
			r := newFakeResolver("linux", tc.files, vers, "/usr/sbin/smartctl", nil)
			p, ver, err := r.resolve("/opt/custom/smartctl", "7.0")
			if err != nil || p != "/usr/sbin/smartctl" || ver != "7.4" {
				t.Fatalf("fallback: got %q %q %v", p, ver, err)
			}
			if len(r.logs) != 1 {
				t.Fatalf("want exactly one log line, got %q", r.logs)
			}
			line := r.logs[0]
			for _, want := range []string{"ERROR: smartctl_path", `"/opt/custom/smartctl"`, tc.reason, "Searching PATH and known locations instead."} {
				if !strings.Contains(line, want) {
					t.Errorf("log %q lacks %q", line, want)
				}
			}
			if len(r.notices) != 1 || r.notices[0] != line {
				t.Errorf("service warning = %q, want the log line", r.notices)
			}
		})
	}
}

func TestResolveSmartctlConfiguredInvalidNothingElse(t *testing.T) {
	r := newFakeResolver("linux", nil, nil, "", []string{"/usr/sbin/smartctl"})
	_, _, err := r.resolve("/nope/smartctl", "7.0")
	if err == nil {
		t.Fatal("want an error when nothing is found")
	}
	msg := err.Error()
	for _, want := range []string{"smartctl not found in PATH or known locations", `smartctl_path in config was not used: "/nope/smartctl": file not found.`, "set smartctl_path in config.yaml"} {
		if !strings.Contains(msg, want) {
			t.Errorf("error lacks %q:\n%s", want, msg)
		}
	}
}

func TestResolveSmartctlUnsetUnchanged(t *testing.T) {
	// No smartctl_path: PATH wins with no extra log line, as before.
	r := newFakeResolver("linux", nil, map[string]string{"/usr/bin/smartctl": "7.4"}, "/usr/bin/smartctl", nil)
	p, _, err := r.resolve("", "7.0")
	if err != nil || p != "/usr/bin/smartctl" || len(r.logs) != 0 {
		t.Fatalf("got %q %v logs %q", p, err, r.logs)
	}
	// Known paths: the first executable one with a good version, as before.
	r = newFakeResolver("linux",
		map[string]fs.FileMode{"/opt/sbin/smartctl": 0o644, "/usr/sbin/smartctl": 0o755},
		map[string]string{"/opt/sbin/smartctl": "7.4", "/usr/sbin/smartctl": "7.4"},
		"", []string{"/opt/sbin/smartctl", "/usr/sbin/smartctl"})
	p, _, err = r.resolve("", "7.0")
	if err != nil || p != "/usr/sbin/smartctl" {
		t.Fatalf("got %q %v", p, err)
	}
	if len(r.logs) != 1 || r.logs[0] != "found smartctl 7.4 at /usr/sbin/smartctl" {
		t.Errorf("logs = %q", r.logs)
	}
}

func TestResolveSmartctlWindows(t *testing.T) {
	scoop := `C:\Users\me\scoop\apps\smartmontools\current\bin\smartctl.exe`
	// Windows files have no execute bits: os.Stat reports 0666.
	files := map[string]fs.FileMode{scoop: 0o666, `C:\tools\smartctl.exe`: 0o666, `C:\tools\smartctl.txt`: 0o666}
	vers := map[string]string{scoop: "7.5", `C:\tools\smartctl.exe`: "7.4", `C:\tools\smartctl.txt`: "7.4"}

	// Found by the search when PATH has nothing (the service under SYSTEM).
	r := newFakeResolver("windows", files, vers, "", []string{`C:\Program Files\smartmontools\bin\smartctl.exe`, scoop})
	p, ver, err := r.resolve("", "7.0")
	if err != nil || p != scoop || ver != "7.5" {
		t.Fatalf("got %q %q %v", p, ver, err)
	}
	if len(r.logs) != 1 || r.logs[0] != "found smartctl 7.5 at "+scoop {
		t.Errorf("logs = %q", r.logs)
	}

	// smartctl_path without ".exe" finds the .exe.
	r = newFakeResolver("windows", files, vers, "", nil)
	if p, _, err := r.resolve(`C:\tools\smartctl`, "7.0"); err != nil || p != `C:\tools\smartctl.exe` {
		t.Fatalf("got %q %v", p, err)
	}

	// A file that is not an .exe is refused with a reason.
	r = newFakeResolver("windows", files, vers, "", []string{scoop})
	if p, _, err := r.resolve(`C:\tools\smartctl.txt`, "7.0"); err != nil || p != scoop {
		t.Fatalf("got %q %v", p, err)
	}
	if len(r.logs) == 0 || !strings.Contains(r.logs[0], "not an .exe file") {
		t.Errorf("logs = %q", r.logs)
	}
}

func TestIsExecutableFor(t *testing.T) {
	cases := []struct {
		goos, path string
		mode       fs.FileMode
		want       bool
	}{
		{"windows", `C:\x\smartctl.exe`, 0o666, true},
		{"windows", `C:\x\SMARTCTL.EXE`, 0o666, true},
		{"windows", `C:\x.y\smartctl`, 0o666, false},
		{"windows", `C:\x\smartctl.txt`, 0o777, false},
		{"linux", "/usr/sbin/smartctl", 0o755, true},
		{"linux", "/usr/sbin/smartctl", 0o644, false},
		{"darwin", "/opt/homebrew/bin/smartctl", 0o555, true},
		{"linux", "/x/smartctl.exe", 0o644, false},
	}
	for _, c := range cases {
		if got := isExecutableFor(c.goos, c.path, fakeFile{mode: c.mode}); got != c.want {
			t.Errorf("isExecutableFor(%s, %s, %v) = %v, want %v", c.goos, c.path, c.mode, got, c.want)
		}
	}
}

func TestWindowsSmartctlCandidates(t *testing.T) {
	var globbed []string
	glob := func(pattern string) ([]string, error) {
		globbed = append(globbed, pattern)
		switch pattern {
		case `C:\Users\*\scoop\apps\smartmontools\current\bin\smartctl.exe`:
			return []string{
				`C:\Users\zed\scoop\apps\smartmontools\current\bin\smartctl.exe`,
				`C:\Users\amy\scoop\apps\smartmontools\current\bin\smartctl.exe`,
			}, nil
		case `C:\Users\*\scoop\shims\smartctl.exe`:
			return []string{`C:\Users\amy\scoop\shims\smartctl.exe`}, nil
		}
		return nil, nil
	}
	env := map[string]string{
		"SystemDrive":       "C:",
		"ProgramFiles":      `C:\Program Files`,
		"ProgramFiles(x86)": `C:\Program Files (x86)`,
		"ProgramData":       `C:\ProgramData`,
	}
	got := windowsSmartctlCandidates(func(k string) string { return env[k] }, glob)
	want := []string{
		`C:\Program Files\smartmontools\bin\smartctl.exe`,
		`C:\Program Files (x86)\smartmontools\bin\smartctl.exe`,
		`C:\ProgramData\chocolatey\bin\smartctl.exe`,
		`C:\Program Files\WinGet\Links\smartctl.exe`,
		`C:\ProgramData\scoop\apps\smartmontools\current\bin\smartctl.exe`,
		`C:\ProgramData\scoop\shims\smartctl.exe`,
		`C:\Users\amy\scoop\apps\smartmontools\current\bin\smartctl.exe`,
		`C:\Users\zed\scoop\apps\smartmontools\current\bin\smartctl.exe`,
		`C:\Users\amy\scoop\shims\smartctl.exe`,
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("candidates:\n got %q\nwant %q", got, want)
	}
	wantGlobs := []string{
		`C:\Users\*\scoop\apps\smartmontools\current\bin\smartctl.exe`,
		`C:\Users\*\scoop\shims\smartctl.exe`,
		`C:\Users\*\AppData\Local\Microsoft\WinGet\Links\smartctl.exe`,
	}
	if !reflect.DeepEqual(globbed, wantGlobs) {
		t.Errorf("globs:\n got %q\nwant %q", globbed, wantGlobs)
	}

	// Install-specific variables add their roots; duplicates collapse
	// case-insensitively; an empty environment falls back to C:.
	env = map[string]string{
		"SystemDrive":       `D:`,
		"ProgramFiles":      `D:\Program Files\`,
		"ChocolateyInstall": `D:\ProgramData\Chocolatey`,
		"SCOOP_GLOBAL":      `D:\scoop-global`,
		"SCOOP":             `D:\Users\amy\scoop`,
	}
	got = windowsSmartctlCandidates(func(k string) string { return env[k] }, func(string) ([]string, error) {
		return []string{`D:\Users\AMY\scoop\shims\smartctl.exe`}, nil
	})
	want = []string{
		`D:\Program Files\smartmontools\bin\smartctl.exe`,
		`D:\Program Files (x86)\smartmontools\bin\smartctl.exe`,
		`D:\ProgramData\Chocolatey\bin\smartctl.exe`,
		`D:\Program Files\WinGet\Links\smartctl.exe`,
		`D:\scoop-global\apps\smartmontools\current\bin\smartctl.exe`,
		`D:\scoop-global\shims\smartctl.exe`,
		`D:\ProgramData\scoop\apps\smartmontools\current\bin\smartctl.exe`,
		`D:\ProgramData\scoop\shims\smartctl.exe`,
		`D:\Users\amy\scoop\apps\smartmontools\current\bin\smartctl.exe`,
		`D:\Users\amy\scoop\shims\smartctl.exe`,
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("candidates with env:\n got %q\nwant %q", got, want)
	}

	got = windowsSmartctlCandidates(func(string) string { return "" }, func(string) ([]string, error) { return nil, nil })
	if len(got) == 0 || got[0] != `C:\Program Files\smartmontools\bin\smartctl.exe` {
		t.Errorf("empty env: got %q", got)
	}
}
