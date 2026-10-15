package main

import (
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"
)

type logSink struct{ lines []string }

func (l *logSink) logf(format string, args ...any) {
	l.lines = append(l.lines, fmt.Sprintf(format, args...))
}

func (l *logSink) warnings() int {
	n := 0
	for _, s := range l.lines {
		if strings.HasPrefix(s, "WARNING") {
			n++
		}
	}
	return n
}

var hex32 = regexp.MustCompile(`^[0-9a-f]{32}$`)

// unwritableDir returns a path that cannot be created or written, even as
// root: it sits under a regular file.
func unwritableDir(t *testing.T) string {
	t.Helper()
	f := filepath.Join(t.TempDir(), "not-a-dir")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	return filepath.Join(f, "sub")
}

func readIDFile(t *testing.T, dir string) agentIDRecord {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, agentIDFileName))
	if err != nil {
		t.Fatal(err)
	}
	var rec agentIDRecord
	if err := json.Unmarshal(data, &rec); err != nil {
		t.Fatal(err)
	}
	return rec
}

func TestNewAgentIDFormat(t *testing.T) {
	a, b := newAgentID(), newAgentID()
	if !hex32.MatchString(a) || !hex32.MatchString(b) {
		t.Fatalf("bad format: %q %q", a, b)
	}
	if a == b {
		t.Fatal("two IDs are equal")
	}
	if _, ok := normAgentID(a); !ok {
		t.Fatal("generated ID fails validation")
	}
}

func TestNormAgentID(t *testing.T) {
	good := map[string]string{
		"3F9C0E5A7B1D4C2E8A6F0B9D1E2C3A4B": "3f9c0e5a7b1d4c2e8a6f0b9d1e2c3a4b",
		"  my-nas-01  ":                    "my-nas-01",
		"abcdefgh":                         "abcdefgh",
	}
	for in, want := range good {
		got, ok := normAgentID(in)
		if !ok || got != want {
			t.Errorf("%q: got %q %v, want %q", in, got, ok, want)
		}
	}
	for _, in := range []string{"", "short", "has space", "-leading", "under_score", strings.Repeat("a", 65)} {
		if _, ok := normAgentID(in); ok {
			t.Errorf("%q: should be rejected", in)
		}
	}
}

func TestResolveAgentIDCreateThenReload(t *testing.T) {
	dir := t.TempDir()
	var log1 logSink
	id := resolveAgentID("", []string{dir}, log1.logf)
	if !hex32.MatchString(id) {
		t.Fatalf("id = %q", id)
	}
	if rec := readIDFile(t, dir); rec.AgentID != id || rec.Created == "" {
		t.Fatalf("file = %+v", rec)
	}
	path := filepath.Join(dir, agentIDFileName)
	if want := []string{fmt.Sprintf("Agent ID: %s (new, saved in %s)", id, path)}; !reflect.DeepEqual(log1.lines, want) {
		t.Errorf("log = %q", log1.lines)
	}
	if st, _ := os.Stat(path); st.Mode().Perm() != 0o644 {
		t.Errorf("mode = %v", st.Mode().Perm())
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 1 {
		t.Errorf("temp file left behind: %v", entries)
	}

	var log2 logSink
	if again := resolveAgentID("", []string{dir}, log2.logf); again != id {
		t.Fatalf("reload gave %q, want %q", again, id)
	}
	if want := []string{fmt.Sprintf("Agent ID: %s (saved in %s)", id, path)}; !reflect.DeepEqual(log2.lines, want) {
		t.Errorf("log = %q", log2.lines)
	}
}

func TestResolveAgentIDPinWritesNothing(t *testing.T) {
	dir := t.TempDir()
	var l logSink
	if id := resolveAgentID("my-nas-01", []string{dir}, l.logf); id != "my-nas-01" {
		t.Fatalf("id = %q", id)
	}
	if entries, _ := os.ReadDir(dir); len(entries) != 0 {
		t.Errorf("pin wrote files: %v", entries)
	}
	if want := []string{"Agent ID: my-nas-01 (from config)"}; !reflect.DeepEqual(l.lines, want) {
		t.Errorf("log = %q", l.lines)
	}
}

func TestResolveAgentIDFallsBackWhenConfigDirUnwritable(t *testing.T) {
	bad, good := unwritableDir(t), t.TempDir()
	var l logSink
	id := resolveAgentID("", []string{bad, good}, l.logf)
	if !hex32.MatchString(id) || readIDFile(t, good).AgentID != id {
		t.Fatalf("id = %q", id)
	}
	if l.warnings() != 0 {
		t.Errorf("unexpected warning: %q", l.lines)
	}
	// Stable across restarts while the first directory stays unwritable.
	if again := resolveAgentID("", []string{bad, good}, (&logSink{}).logf); again != id {
		t.Errorf("reload gave %q, want %q", again, id)
	}
}

func TestResolveAgentIDExistingFileWins(t *testing.T) {
	// A valid file is used even when a later candidate could be written.
	first, second := t.TempDir(), t.TempDir()
	if err := writeAgentIDFile(first, "0123456789abcdef0123456789abcdef"); err != nil {
		t.Fatal(err)
	}
	if id := resolveAgentID("", []string{first, second}, (&logSink{}).logf); id != "0123456789abcdef0123456789abcdef" {
		t.Fatalf("id = %q", id)
	}
	if entries, _ := os.ReadDir(second); len(entries) != 0 {
		t.Errorf("second dir written: %v", entries)
	}
}

func TestResolveAgentIDNothingWritable(t *testing.T) {
	var l logSink
	id := resolveAgentID("", []string{unwritableDir(t), unwritableDir(t)}, l.logf)
	if id != "" {
		t.Fatalf("id = %q, want none", id)
	}
	if len(l.lines) != 1 || l.warnings() != 1 {
		t.Fatalf("want exactly one warning, got %q", l.lines)
	}
	if !strings.Contains(l.lines[0], "not reported") || !strings.Contains(l.lines[0], "agent_id in config.yaml") {
		t.Errorf("warning = %q", l.lines[0])
	}

	var l2 logSink
	if id := resolveAgentID("", nil, l2.logf); id != "" || l2.warnings() != 1 {
		t.Errorf("no dirs: id %q, log %q", id, l2.lines)
	}
}

func TestResolveAgentIDMalformedFileIsReplaced(t *testing.T) {
	for name, content := range map[string]string{
		"not json":   "garbage{",
		"bad id":     `{"agent_id":"no"}`,
		"missing id": `{"created":"2026-10-15T00:00:00Z"}`,
	} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			if err := os.WriteFile(filepath.Join(dir, agentIDFileName), []byte(content), 0o644); err != nil {
				t.Fatal(err)
			}
			var l logSink
			id := resolveAgentID("", []string{dir}, l.logf)
			if !hex32.MatchString(id) || readIDFile(t, dir).AgentID != id {
				t.Fatalf("id = %q", id)
			}
			if l.warnings() != 1 || !strings.Contains(l.lines[0], "not a valid agent ID file") {
				t.Errorf("log = %q", l.lines)
			}
		})
	}
}

func TestAgentIDCandidateDirs(t *testing.T) {
	env := func(m map[string]string) func(string) string { return func(k string) string { return m[k] } }
	isDir := func(dirs ...string) func(string) bool {
		return func(p string) bool {
			for _, d := range dirs {
				if d == p {
					return true
				}
			}
			return false
		}
	}
	cases := []struct {
		name, config, goos string
		env                map[string]string
		dirs               []string
		want               []string
	}{
		{"linux service", "/etc/smartha-agent/config.yaml", "linux", nil, nil,
			[]string{"/etc/smartha-agent", "/var/lib/smartha-agent"}},
		{"linux no config", "", "linux", nil, nil,
			[]string{"/var/lib/smartha-agent"}},
		{"HA app", "", "linux", map[string]string{"SUPERVISOR_TOKEN": "x"}, []string{"/data"},
			[]string{"/data", "/var/lib/smartha-agent"}},
		{"HA token without /data", "", "linux", map[string]string{"SUPERVISOR_TOKEN": "x"}, nil,
			[]string{"/var/lib/smartha-agent"}},
		{"/data without token", "", "linux", nil, []string{"/data"},
			[]string{"/var/lib/smartha-agent"}},
		{"macOS", "/etc/smartha-agent/config.yaml", "darwin", nil, nil,
			[]string{"/etc/smartha-agent", "/Library/Application Support/SmartHA-Agent"}},
		{"Windows", `C:\Program Files\SmartHA-Agent\config.yaml`, "windows", map[string]string{"ProgramData": `D:\ProgramData`}, nil,
			[]string{`C:\Program Files\SmartHA-Agent`, `D:\ProgramData\SmartHA-Agent`}},
		{"Windows no ProgramData", "", "windows", nil, nil,
			[]string{`C:\ProgramData\SmartHA-Agent`}},
		{"config already in state dir", "/var/lib/smartha-agent/config.yaml", "linux", nil, nil,
			[]string{"/var/lib/smartha-agent"}},
	}
	for _, tc := range cases {
		got := agentIDCandidateDirsFor(tc.config, tc.goos, env(tc.env), isDir(tc.dirs...))
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s: got %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestBuildTXTAgentID(t *testing.T) {
	txt := buildTXT("nas", "0", 3, "192.168.1.10", "0123456789abcdef0123456789abcdef")
	want := []string{"txtvers=1", "version=" + version, "hostname=nas", "os=" + detectOS(), "auth=0", "drives=3",
		"ip=192.168.1.10", "agent_id=0123456789abcdef0123456789abcdef"}
	if !reflect.DeepEqual(txt, want) {
		t.Errorf("got %q", txt)
	}
	for _, s := range buildTXT("nas", "0", 3, "", "") {
		if strings.HasPrefix(s, "ip=") || strings.HasPrefix(s, "agent_id=") {
			t.Errorf("empty field present: %q", s)
		}
	}
}

func TestHealthReportsAgentID(t *testing.T) {
	saved := agentID
	t.Cleanup(func() { agentID = saved })
	dc := NewDriveCache(&Config{})

	for _, tc := range []struct {
		id      string
		present bool
	}{{"0123456789abcdef0123456789abcdef", true}, {"", false}} {
		agentID = tc.id
		rec := httptest.NewRecorder()
		handleHealth(dc)(rec, httptest.NewRequest("GET", "/api/health", nil))
		var body map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatal(err)
		}
		v, ok := body["agent_id"]
		if ok != tc.present || (ok && v != tc.id) {
			t.Errorf("id %q: agent_id = %v (present %v)", tc.id, v, ok)
		}
	}
}
