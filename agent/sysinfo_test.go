package main

import (
	"encoding/json"
	"errors"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestParseOSRelease(t *testing.T) {
	cases := []struct {
		name, in      string
		wantName, ver string
	}{
		{"pretty name", "NAME=\"Ubuntu\"\nVERSION_ID=\"24.04\"\nPRETTY_NAME=\"Ubuntu 24.04.1 LTS\"\n", "Ubuntu 24.04.1 LTS", "24.04"},
		{"single quotes and comments", "# comment\nPRETTY_NAME='Debian GNU/Linux 12 (bookworm)'\nVERSION_ID='12'\n", "Debian GNU/Linux 12 (bookworm)", "12"},
		{"no pretty name", "NAME=Fake OS\nVERSION=\"3 (Test)\"\nVERSION_ID=3\n", "Fake OS 3 (Test)", "3"},
		{"empty", "", "", ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := parseOSRelease(c.in)
			if got.Name != c.wantName || got.Version != c.ver {
				t.Errorf("got %+v, want %q / %q", got, c.wantName, c.ver)
			}
		})
	}
}

func TestOSReleaseInfoPrefersMountPrefix(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "etc"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "etc/os-release"),
		[]byte("PRETTY_NAME=\"Host Linux 9\"\nVERSION_ID=9\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := osReleaseInfo(root); got.Name != "Host Linux 9" || got.Version != "9" {
		t.Errorf("got %+v, want the host's os-release under the prefix", got)
	}
	// A prefix with no os-release falls back to the agent's own (whatever
	// the test machine has); it must not panic or return the prefix's.
	_ = osReleaseInfo(t.TempDir())
}

func TestMacOSInfo(t *testing.T) {
	run := func(name string, args []string, _ time.Duration) ([]byte, error) {
		if name != "sw_vers" {
			t.Fatalf("unexpected command %s", name)
		}
		switch args[0] {
		case "-productName":
			return []byte("macOS\n"), nil
		case "-productVersion":
			return []byte("15.1\n"), nil
		}
		return nil, errors.New("bad flag")
	}
	if got := macOSInfo(run); got.Name != "macOS 15.1" || got.Version != "15.1" {
		t.Errorf("got %+v", got)
	}
	fail := func(string, []string, time.Duration) ([]byte, error) { return nil, errors.New("no sw_vers") }
	if got := macOSInfo(fail); got != (systemInfo{}) {
		t.Errorf("failing sw_vers gave %+v, want empty", got)
	}
}

func TestWindowsProductName(t *testing.T) {
	cases := []struct {
		product, display, build string
		want                    systemInfo
	}{
		{"Windows 10 Pro", "24H2", "26100", systemInfo{"Windows 11 Pro 24H2", "24H2"}},
		{"Windows 10 Home", "22H2", "19045", systemInfo{"Windows 10 Home 22H2", "22H2"}},
		{"Windows Server 2022 Standard", "", "20348", systemInfo{"Windows Server 2022 Standard build 20348", "build 20348"}},
		{"", "24H2", "26100", systemInfo{}},
	}
	for _, c := range cases {
		if got := windowsProductName(c.product, c.display, c.build); got != c.want {
			t.Errorf("%q/%q/%q: got %+v, want %+v", c.product, c.display, c.build, got, c.want)
		}
	}
}

func TestHealthReportsHostAndOS(t *testing.T) {
	saved := agentSystem
	t.Cleanup(func() { agentSystem = saved })
	dc := NewDriveCache(&Config{ScanInterval: time.Minute})

	agentSystem = systemInfo{Name: "Fake OS 1.2", Version: "1.2"}
	out := healthJSON(t, dc)
	var name, ver, osField, host string
	_ = json.Unmarshal(out["os_name"], &name)
	_ = json.Unmarshal(out["os_version"], &ver)
	_ = json.Unmarshal(out["os"], &osField)
	_ = json.Unmarshal(out["hostname"], &host)
	if name != "Fake OS 1.2" || ver != "1.2" {
		t.Errorf("os_name/os_version = %q/%q", name, ver)
	}
	if osField != detectOS() {
		t.Errorf("os = %q, want unchanged %q", osField, detectOS())
	}
	if want := shortHostname(); host != want {
		t.Errorf("hostname = %q, want %q", host, want)
	}

	agentSystem = systemInfo{}
	out = healthJSON(t, dc)
	for _, k := range []string{"os_name", "os_version"} {
		if _, ok := out[k]; ok {
			t.Errorf("%s present when unknown", k)
		}
	}

	rec := httptest.NewRecorder()
	handleHealth(dc)(rec, httptest.NewRequest("GET", "/api/health", nil))
	if rec.Code != 200 {
		t.Errorf("status %d", rec.Code)
	}
}

// healthJSON decodes one /api/health response. Kept here, not shared with the
// unix-only pool tests, so this file builds on every platform.
func healthJSON(t *testing.T, dc *DriveCache) map[string]json.RawMessage {
	t.Helper()
	rec := httptest.NewRecorder()
	handleHealth(dc)(rec, httptest.NewRequest("GET", "/api/health", nil))
	var out map[string]json.RawMessage
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	return out
}
