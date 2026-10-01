//go:build !windows

package main

import (
	"bytes"
	"encoding/json"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// stubZpool writes a shell script standing in for zpool. body runs with $DIR
// set to the fixture directory and $CALLS to a file every invocation appends
// its arguments and locale/time zone to. Returns the script path and CALLS.
func stubZpool(t *testing.T, body string) (string, string) {
	t.Helper()
	dir := t.TempDir()
	fixtures, err := filepath.Abs(filepath.Join("testdata", "zpool"))
	if err != nil {
		t.Fatal(err)
	}
	calls := filepath.Join(dir, "calls")
	script := "#!/bin/sh\n" +
		"DIR='" + fixtures + "'\n" +
		"CALLS='" + calls + "'\n" +
		`echo "$* LC_ALL=$LC_ALL TZ=$TZ" >> "$CALLS"` + "\n" +
		body + "\n"
	path := filepath.Join(dir, "zpool")
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return path, calls
}

// A zpool from OpenZFS 2.3 or later: -j works.
const stubModern = `case " $* " in
  *" -j "*) cat "$DIR/degraded-faulted.json" ;;
  *) cat "$DIR/degraded-faulted.txt" ;;
esac`

// A zpool from before 2.3: -j is an unknown option, as in zpool_do_status()
// at zfs-2.2.7, which prints usage and exits 2.
const stubOld = `case " $* " in
  *" -j "*) echo "invalid option 'j'" >&2; echo "usage:" >&2; exit 2 ;;
  *) cat "$DIR/gh50-three-pools.txt" ;;
esac`

// A zpool present without its kernel module.
const stubNoModule = `echo "The ZFS modules are not loaded." >&2
echo "Try running 'modprobe zfs' as root to load them." >&2
exit 1`

func countLines(path string) int {
	b, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	return strings.Count(string(b), "\n")
}

// captureLog redirects the standard logger for the test.
func captureLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prevOut, prevFlags := log.Writer(), log.Flags()
	log.SetOutput(&buf)
	log.SetFlags(0)
	t.Cleanup(func() {
		log.SetOutput(prevOut)
		log.SetFlags(prevFlags)
	})
	return &buf
}

func waitRefresh(t *testing.T, pc *PoolCache) {
	t.Helper()
	select {
	case <-pc.Refresh():
	case <-time.After(10 * time.Second):
		t.Fatal("refresh did not finish")
	}
}

func TestPoolCache_JSONWhenSupported(t *testing.T) {
	captureLog(t)
	path, calls := stubZpool(t, stubModern)
	pc := NewPoolCache(path, false)
	waitRefresh(t, pc)

	pools := pc.Pools()
	if len(pools) != 1 || pools[0].Name != "tank" || pools[0].State != "DEGRADED" {
		t.Fatalf("pools = %+v", pools)
	}
	if pc.mode != zpoolModeJSON {
		t.Errorf("mode = %d, want json", pc.mode)
	}
	b, _ := os.ReadFile(calls)
	if got := strings.TrimSpace(string(b)); got != "status -j --json-int LC_ALL=C TZ=UTC" {
		t.Errorf("zpool was run as %q", got)
	}

	// The decision sticks: the next poll runs -j once and nothing else.
	waitRefresh(t, pc)
	if n := countLines(calls); n != 2 {
		t.Errorf("%d zpool runs after two polls, want 2", n)
	}
}

func TestPoolCache_TextWhenJSONUnsupported(t *testing.T) {
	captureLog(t)
	path, calls := stubZpool(t, stubOld)
	pc := NewPoolCache(path, false)
	waitRefresh(t, pc)

	if got := pc.Count(); got != 3 {
		t.Fatalf("%d pools, want 3", got)
	}
	if pc.mode != zpoolModeText {
		t.Errorf("mode = %d, want text", pc.mode)
	}
	// First poll tries -j then -p; later polls go straight to -p.
	waitRefresh(t, pc)
	b, _ := os.ReadFile(calls)
	runs := strings.Split(strings.TrimSpace(string(b)), "\n")
	if len(runs) != 3 || !strings.HasPrefix(runs[0], "status -j") ||
		!strings.HasPrefix(runs[1], "status -p ") || !strings.HasPrefix(runs[2], "status -p ") {
		t.Errorf("runs = %q", runs)
	}
}

func TestPoolCache_FailingZpoolReportsZeroAndLogsOnce(t *testing.T) {
	logs := captureLog(t)
	path, _ := stubZpool(t, stubNoModule)
	pc := NewPoolCache(path, false)
	for i := 0; i < 3; i++ {
		waitRefresh(t, pc)
	}
	if got := pc.Count(); got != 0 {
		t.Errorf("%d pools, want 0", got)
	}
	out := logs.String()
	if n := strings.Count(out, "zfs pool status"); n != 1 {
		t.Errorf("logged %d times over three polls, want once:\n%s", n, out)
	}
	if !strings.Contains(out, "modules are not loaded") {
		t.Errorf("log does not say why:\n%s", out)
	}

	// Served as 503, not as an empty list: Home Assistant reads a pool left
	// out of a list as missing, and a zpool that failed says nothing of that.
	rec := httptest.NewRecorder()
	pc.HandlePools(rec, httptest.NewRequest("GET", "/api/pools", nil))
	if rec.Code != http.StatusServiceUnavailable {
		t.Errorf("/api/pools = %d %s, want 503", rec.Code, rec.Body.String())
	}
}

// Before the first read completes, and after a failed one, /api/pools is 503;
// after a successful read it is the list, [] when no pool is imported.
func TestPoolsEndpointAnswersOnlyAfterASuccessfulRead(t *testing.T) {
	captureLog(t)
	dir := t.TempDir()
	broken := filepath.Join(dir, "broken")
	path, _ := stubZpool(t, `if [ -e '`+broken+`' ]; then exit 1; fi
echo '{"output_version":{"command":"zpool status","vers_major":0,"vers_minor":1},"pools":{}}'
`)
	pc := NewPoolCache(path, false)
	get := func() (int, string) {
		rec := httptest.NewRecorder()
		pc.HandlePools(rec, httptest.NewRequest("GET", "/api/pools", nil))
		return rec.Code, strings.TrimSpace(rec.Body.String())
	}
	if code, _ := get(); code != http.StatusServiceUnavailable {
		t.Errorf("before the first read: %d, want 503", code)
	}
	waitRefresh(t, pc)
	if code, body := get(); code != http.StatusOK || body != "[]" {
		t.Errorf("no pools imported: %d %s, want 200 []", code, body)
	}
	os.WriteFile(broken, nil, 0o644)
	waitRefresh(t, pc)
	if code, _ := get(); code != http.StatusServiceUnavailable {
		t.Errorf("after a failed read: %d, want 503", code)
	}
}

func TestPoolCache_FailureAfterSuccessDropsPoolsAndReprobes(t *testing.T) {
	captureLog(t)
	dir := t.TempDir()
	broken := filepath.Join(dir, "broken")
	path, calls := stubZpool(t, `if [ -e '`+broken+`' ]; then exit 1; fi
`+stubModern)
	pc := NewPoolCache(path, false)
	waitRefresh(t, pc)
	if pc.Count() != 1 {
		t.Fatal("first read failed")
	}
	os.WriteFile(broken, nil, 0o644)
	waitRefresh(t, pc)
	if pc.Count() != 0 || pc.mode != zpoolModeUnknown {
		t.Errorf("after failure: %d pools, mode %d", pc.Count(), pc.mode)
	}
	os.Remove(broken)
	waitRefresh(t, pc)
	if pc.Count() != 1 || pc.mode != zpoolModeJSON {
		t.Errorf("after recovery: %d pools, mode %d", pc.Count(), pc.mode)
	}
	if n := countLines(calls); n != 3 {
		t.Errorf("%d runs, want 3 (json ok, json failed, json ok)", n)
	}
}

func TestPoolCache_Timeout(t *testing.T) {
	logs := captureLog(t)
	prev := zpoolTimeout
	zpoolTimeout = 300 * time.Millisecond
	t.Cleanup(func() { zpoolTimeout = prev })

	path, calls := stubZpool(t, "exec sleep 30")
	pc := NewPoolCache(path, false)
	start := time.Now()
	waitRefresh(t, pc)
	if elapsed := time.Since(start); elapsed > 5*time.Second {
		t.Errorf("refresh took %s", elapsed)
	}
	if pc.Count() != 0 {
		t.Error("pools reported after a timeout")
	}
	if !strings.Contains(logs.String(), "timed out") {
		t.Errorf("timeout not logged:\n%s", logs.String())
	}
	// A hung -j is not followed by a text attempt.
	if n := countLines(calls); n != 1 {
		t.Errorf("%d runs after a timeout, want 1", n)
	}
}

// While a read is still running, another poll starts nothing.
func TestPoolCache_OneReadAtATime(t *testing.T) {
	captureLog(t)
	prev := zpoolTimeout
	zpoolTimeout = 2 * time.Second
	t.Cleanup(func() { zpoolTimeout = prev })

	path, calls := stubZpool(t, "sleep 1\n"+stubModern)
	pc := NewPoolCache(path, false)
	first := pc.Refresh()
	second := pc.Refresh()
	select {
	case <-second:
	case <-time.After(200 * time.Millisecond):
		t.Fatal("a refresh during a running read did not return at once")
	}
	<-first
	if n := countLines(calls); n != 1 {
		t.Errorf("%d runs, want 1", n)
	}
}

func TestNewPoolCacheFromConfig(t *testing.T) {
	stub, _ := stubZpool(t, stubModern)
	off := false

	t.Run("zpool_path to a stub script", func(t *testing.T) {
		captureLog(t)
		pc := NewPoolCacheFromConfig(&Config{ZpoolPath: stub})
		if pc == nil || pc.zpoolPath != stub {
			t.Fatalf("pc = %+v", pc)
		}
		waitRefresh(t, pc)
		if pc.Count() != 1 {
			t.Errorf("%d pools via the stub, want 1", pc.Count())
		}
	})

	t.Run("zfs_pool_status false", func(t *testing.T) {
		logs := captureLog(t)
		if pc := NewPoolCacheFromConfig(&Config{ZFSPoolStatus: &off, ZpoolPath: stub}); pc != nil {
			t.Error("pool status on despite zfs_pool_status: false")
		}
		if logs.Len() != 0 {
			t.Errorf("logged: %s", logs.String())
		}
	})

	t.Run("no zpool anywhere logs nothing", func(t *testing.T) {
		logs := captureLog(t)
		t.Setenv("PATH", t.TempDir())
		prev := zpoolSearchPaths
		zpoolSearchPaths = []string{filepath.Join(t.TempDir(), "zpool")}
		t.Cleanup(func() { zpoolSearchPaths = prev })

		if pc := NewPoolCacheFromConfig(&Config{}); pc != nil {
			t.Error("pool status on with no zpool")
		}
		if logs.Len() != 0 {
			t.Errorf("a machine without ZFS logged: %s", logs.String())
		}
	})

	t.Run("zpool found on PATH", func(t *testing.T) {
		captureLog(t)
		t.Setenv("PATH", filepath.Dir(stub))
		pc := NewPoolCacheFromConfig(&Config{})
		if pc == nil || pc.zpoolPath != stub {
			t.Fatalf("pc = %+v", pc)
		}
	})

	t.Run("a configured path that does not exist warns", func(t *testing.T) {
		logs := captureLog(t)
		if pc := NewPoolCacheFromConfig(&Config{ZpoolPath: "/nonexistent/zpool"}); pc != nil {
			t.Error("pool status on with a missing zpool_path")
		}
		if !strings.Contains(logs.String(), "zpool_path") {
			t.Errorf("no warning: %q", logs.String())
		}
	})

	t.Run("default is on", func(t *testing.T) {
		if !(&Config{}).PoolStatusEnabled() {
			t.Error("pool status off by default")
		}
	})
}

func healthOf(t *testing.T, dc *DriveCache) map[string]json.RawMessage {
	t.Helper()
	rec := httptest.NewRecorder()
	handleHealth(dc)(rec, httptest.NewRequest("GET", "/api/health", nil))
	var out map[string]json.RawMessage
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestHealthAdvertisesPools(t *testing.T) {
	captureLog(t)
	dc := NewDriveCache(&Config{ScanInterval: time.Minute})

	off := healthOf(t, dc)
	if _, ok := off["pools"]; ok {
		t.Error("pools count present with pool status off")
	}
	if strings.Contains(string(off["endpoints"]), "/api/pools") {
		t.Error("/api/pools advertised with pool status off")
	}

	path, _ := stubZpool(t, stubOld)
	dc.poolCache = NewPoolCache(path, false)
	waitRefresh(t, dc.poolCache)
	on := healthOf(t, dc)
	if string(on["pools"]) != "3" {
		t.Errorf("pools = %s, want 3", on["pools"])
	}
	if !strings.Contains(string(on["endpoints"]), `"/api/pools"`) {
		t.Errorf("endpoints = %s", on["endpoints"])
	}
}

// /api/pools sits behind the same bearer token as /api/drives.
func TestPoolsEndpointNeedsTheToken(t *testing.T) {
	captureLog(t)
	path, _ := stubZpool(t, stubModern)
	pc := NewPoolCache(path, false)
	waitRefresh(t, pc)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/pools", pc.HandlePools)
	h := authMiddleware("secret", mux)

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/api/pools", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("no token: %d", rec.Code)
	}

	rec = httptest.NewRecorder()
	req := httptest.NewRequest("GET", "/api/pools", nil)
	req.Header.Set("Authorization", "Bearer secret")
	h.ServeHTTP(rec, req)
	var pools []PoolInfo
	if rec.Code != http.StatusOK || json.Unmarshal(rec.Body.Bytes(), &pools) != nil || len(pools) != 1 {
		t.Errorf("with token: %d %s", rec.Code, rec.Body.String())
	}
}

// The drive cache's cycle starts a pool read and does not wait for it.
func TestDriveRefreshStartsPoolRead(t *testing.T) {
	captureLog(t)
	prev := zpoolTimeout
	zpoolTimeout = 5 * time.Second
	t.Cleanup(func() { zpoolTimeout = prev })

	smartctl, _ := stubZpool(t, `echo '{"devices":[]}'`)
	path, calls := stubZpool(t, "sleep 1\n"+stubModern)
	dc := NewDriveCache(&Config{ScanInterval: time.Minute, SmartctlPath: smartctl, StandbyMode: "never"})
	dc.poolCache = NewPoolCache(path, false)

	start := time.Now()
	dc.Refresh()
	if elapsed := time.Since(start); elapsed > 900*time.Millisecond {
		t.Errorf("drive refresh waited %s for zpool", elapsed)
	}
	deadline := time.Now().Add(5 * time.Second)
	for dc.poolCache.Count() == 0 && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	if dc.poolCache.Count() != 1 || countLines(calls) != 1 {
		t.Errorf("pool read did not complete: %d pools, %d runs", dc.poolCache.Count(), countLines(calls))
	}
}
