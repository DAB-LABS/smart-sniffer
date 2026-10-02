//go:build !windows

package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// smartctlRig is a fake smartctl that answers the -a call, the -d sat call
// and the devstat call separately, per device, from files in its directory,
// and logs every argument list:
//
//	<dev>.<call>.body   printed on stdout (absent: nothing)
//	<dev>.<call>.code   exit code (absent: 0)
//	<dev>.<call>.hang   present: sleep past any test timeout
//	scan.json           printed for --scan and --scan-open
//	scan.hang, scan-open.hang
//
// <call> is "devstat" for a call with "-l devstat", else "sat" for a call
// with "-d sat", else "a". <dev> is the base name of the last argument.
type smartctlRig struct {
	t    *testing.T
	dir  string
	path string
}

func newSmartctlRig(t *testing.T) *smartctlRig {
	t.Helper()
	dir := t.TempDir()
	script := "#!/bin/sh\n" +
		"D='" + dir + "'\n" +
		`echo "$*" >> "$D/calls.log"` + "\n" +
		`w=""` + "\n" +
		`case " $* " in *" --scan-open "*) w=scan-open;; *" --scan "*) w=scan;; esac` + "\n" +
		`if [ -n "$w" ]; then` + "\n" +
		`  if [ -f "$D/$w.hang" ]; then exec sleep 5; fi` + "\n" +
		`  cat "$D/scan.json" 2>/dev/null; exit 0` + "\n" +
		`fi` + "\n" +
		`for a in "$@"; do last="$a"; done` + "\n" +
		`dev=$(basename "$last")` + "\n" +
		`call=a` + "\n" +
		`case " $* " in *" -l devstat "*) call=devstat;; *" -d sat "*) call=sat;; esac` + "\n" +
		`f="$D/$dev.$call"` + "\n" +
		`if [ -f "$f.hang" ]; then exec sleep 5; fi` + "\n" +
		`if [ -f "$f.body" ]; then cat "$f.body"; fi` + "\n" +
		`code=0; if [ -f "$f.code" ]; then code=$(cat "$f.code"); fi` + "\n" +
		`exit $code` + "\n"
	path := filepath.Join(dir, "smartctl")
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return &smartctlRig{t: t, dir: dir, path: path}
}

func (r *smartctlRig) write(name string, data []byte) {
	r.t.Helper()
	if err := os.WriteFile(filepath.Join(r.dir, name), data, 0o644); err != nil {
		r.t.Fatal(err)
	}
}

func (r *smartctlRig) remove(name string) {
	r.t.Helper()
	if err := os.Remove(filepath.Join(r.dir, name)); err != nil && !os.IsNotExist(err) {
		r.t.Fatal(err)
	}
}

// set makes <dev>'s <call> print body and exit code; it also clears a hang.
func (r *smartctlRig) set(dev, call string, body []byte, code int) {
	r.t.Helper()
	r.remove(dev + "." + call + ".hang")
	if body == nil {
		r.remove(dev + "." + call + ".body")
	} else {
		r.write(dev+"."+call+".body", body)
	}
	r.write(dev+"."+call+".code", []byte(itoa(code)))
}

func (r *smartctlRig) hang(name string) { r.write(name+".hang", nil) }

func (r *smartctlRig) scan(devs ...scanDevice) {
	r.t.Helper()
	b, err := json.Marshal(map[string]any{"devices": devs})
	if err != nil {
		r.t.Fatal(err)
	}
	r.write("scan.json", b)
}

// calls returns every logged argument list and clears the log.
func (r *smartctlRig) calls() []string {
	r.t.Helper()
	b, err := os.ReadFile(filepath.Join(r.dir, "calls.log"))
	if err != nil && !os.IsNotExist(err) {
		r.t.Fatal(err)
	}
	r.remove("calls.log")
	s := strings.TrimSpace(string(b))
	if s == "" {
		return nil
	}
	return strings.Split(s, "\n")
}

func itoa(n int) string {
	b, _ := json.Marshal(n)
	return string(b)
}

// split separates logged calls into scans, main (-a, including SAT) calls
// and devstat calls.
func splitCalls(calls []string) (scans, mains, devstats []string) {
	for _, c := range calls {
		switch {
		case strings.Contains(c, "--scan"):
			scans = append(scans, c)
		case strings.Contains(c, "-l devstat"):
			devstats = append(devstats, c)
		default:
			mains = append(mains, c)
		}
	}
	return
}

// newRigCache builds a drive cache on the rig.
func newRigCache(r *smartctlRig, standbyMode string, devstatOn bool) *DriveCache {
	cfg := &Config{ScanInterval: time.Minute, SmartctlPath: r.path, StandbyMode: standbyMode}
	if !devstatOn {
		f := false
		cfg.DeviceStatistics = &f
	}
	dc := NewDriveCache(cfg)
	dc.goos = "linux"
	return dc
}

func shortenDevstatTimeout(t *testing.T) {
	t.Helper()
	saved := devstatTimeout
	devstatTimeout = 200 * time.Millisecond
	t.Cleanup(func() { devstatTimeout = saved })
}

// onlyDrive returns the single drive in the cache.
func onlyDrive(t *testing.T, dc *DriveCache) DriveInfo {
	t.Helper()
	dc.mu.RLock()
	defer dc.mu.RUnlock()
	if len(dc.drives) != 1 {
		t.Fatalf("%d drives in cache, want 1", len(dc.drives))
	}
	for _, d := range dc.drives {
		return d
	}
	return DriveInfo{}
}
