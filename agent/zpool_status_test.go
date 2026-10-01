package main

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// Fixtures in testdata/zpool, one pool set per stem, each as `zpool status -p`
// text (.txt) and `zpool status -j --json-int` (.json).
//
// Provenance. Only one file is a real capture; everything else is constructed
// and must not be presented as captured output.
//
//	gh50-three-pools.txt   REAL. The reporter's `zpool status` from GH #50
//	                       (Proxmox, pools raid10, rpool, storage), copied from
//	                       the issue body. Captured without -p, serials
//	                       redacted by the reporter, and tabs turned into
//	                       spaces by the paste. Times are the reporter's local
//	                       time; the agent runs zpool with TZ=UTC, so they are
//	                       read as UTC here.
//	gh50-three-pools.json  CONSTRUCTED. The same three pools in the JSON form,
//	                       derived from status_callback_json() and its helpers
//	                       in cmd/zpool/zpool_main.c at zfs-2.3.4. Disk names
//	                       that the redaction made identical were made unique,
//	                       since real vdev names are; GUIDs and sizes invented.
//	degraded-faulted.*     CONSTRUCTED. raidz2, one disk FAULTED with read and
//	                       write errors, another with checksum errors; the pool
//	                       row reads zero, as it does on real pools.
//	data-errors.*          CONSTRUCTED. Mirror with checksum errors on both
//	                       sides, two permanent data errors, a log device.
//	scrub-in-progress.*    CONSTRUCTED. raidz1 mid-scrub, with a cache device.
//	never-scrubbed.*       CONSTRUCTED. Single disk, no scan record.
//	resilver.*             CONSTRUCTED. A FAULTED disk being replaced; the scan
//	                       record is the resilver, so the last scrub is unknown.
//
// The constructed text follows status_callback(), print_status_config() and
// print_scan_scrub_resilver_status() at zfs-2.3.4 (the same at 2.2.7 and
// 2.4.4 for every line used here).

func readFixture(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", "zpool", name))
	if err != nil {
		t.Fatalf("fixture %s: %v", name, err)
	}
	return b
}

func sp(s string) *string { return &s }
func up(v uint64) *uint64 { return &v }

const (
	featStatus = "Some supported and requested features are not enabled on the pool. " +
		"The pool can still be used, but some features are unavailable."
	featAction = "Enable all features using 'zpool upgrade'. Once this is done, the pool may no " +
		"longer be accessible by software that does not support the features. " +
		"See zpool-features(7) for details."
)

func scrubbed(end string, repaired, errs uint64) func(*PoolInfo) {
	return func(p *PoolInfo) {
		p.ScanFunction, p.ScanState = sp("SCRUB"), sp("FINISHED")
		p.LastScrubEnd, p.LastScrubRepaired, p.LastScrubErrors = sp(end), up(repaired), up(errs)
	}
}

func pool(name, state string, opts ...func(*PoolInfo)) PoolInfo {
	p := PoolInfo{Name: name, State: state, DataErrors: up(0)}
	for _, o := range opts {
		o(&p)
	}
	return p
}

func withStatus(status, action string) func(*PoolInfo) {
	return func(p *PoolInfo) { p.Status, p.Action = sp(status), sp(action) }
}

func withErrors(r, w, c uint64) func(*PoolInfo) {
	return func(p *PoolInfo) { p.ReadErrors, p.WriteErrors, p.ChecksumErrors = r, w, c }
}

// expected is what both readers must produce for each fixture.
var expected = map[string][]PoolInfo{
	"gh50-three-pools": {
		pool("raid10", "ONLINE", withStatus(featStatus, featAction), scrubbed("2026-09-01T03:05:34Z", 0, 0)),
		pool("rpool", "ONLINE", withStatus(featStatus, featAction), scrubbed("2026-09-01T01:00:33Z", 0, 0)),
		pool("storage", "ONLINE", withStatus(featStatus, featAction), scrubbed("2026-09-01T09:39:32Z", 0, 0)),
	},
	"degraded-faulted": {
		pool("tank", "DEGRADED",
			withStatus(
				"One or more devices are faulted in response to persistent errors. Sufficient replicas "+
					"exist for the pool to continue functioning in a degraded state.",
				"Replace the faulted device, or use 'zpool clear' to mark the device repaired."),
			withErrors(18, 3, 2),
			scrubbed("2026-09-27T05:36:09Z", 1572864, 0)),
	},
	"data-errors": {
		pool("backup", "ONLINE",
			withStatus(
				"One or more devices has experienced an error resulting in data corruption. "+
					"Applications may be affected.",
				"Restore the file in question if possible. Otherwise restore the entire pool from backup."),
			withErrors(0, 0, 16),
			scrubbed("2026-09-13T00:24:02Z", 0, 2),
			func(p *PoolInfo) { p.DataErrors = up(2) }),
	},
	"scrub-in-progress": {
		pool("archive", "ONLINE", func(p *PoolInfo) {
			p.ScanFunction, p.ScanState, p.ScrubInProgress = sp("SCRUB"), sp("SCANNING"), true
		}),
	},
	"never-scrubbed": {
		pool("scratch", "ONLINE"),
	},
	"resilver": {
		pool("media", "DEGRADED",
			withStatus(
				"One or more devices is currently being resilvered. The pool will continue to "+
					"function, possibly in a degraded state.",
				"Wait for the resilver to complete."),
			withErrors(0, 37, 0),
			func(p *PoolInfo) { p.ScanFunction, p.ScanState = sp("RESILVER"), sp("SCANNING") }),
	},
}

func jsonOf(t *testing.T, v any) string {
	t.Helper()
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestZpoolParsers_EveryFixtureBothForms(t *testing.T) {
	for stem, want := range expected {
		t.Run(stem, func(t *testing.T) {
			fromJSON, err := parseZpoolStatusJSON(readFixture(t, stem+".json"))
			if err != nil {
				t.Fatalf("json: %v", err)
			}
			fromText, err := parseZpoolStatusText(readFixture(t, stem+".txt"))
			if err != nil {
				t.Fatalf("text: %v", err)
			}
			if !reflect.DeepEqual(fromJSON, want) {
				t.Errorf("json reader:\n%s\nwant:\n%s", jsonOf(t, fromJSON), jsonOf(t, want))
			}
			if !reflect.DeepEqual(fromText, want) {
				t.Errorf("text reader:\n%s\nwant:\n%s", jsonOf(t, fromText), jsonOf(t, want))
			}
			if !reflect.DeepEqual(fromJSON, fromText) {
				t.Errorf("the two readers disagree on %s", stem)
			}
		})
	}
}

// Every fixture in testdata/zpool must be covered above, in both forms.
func TestZpoolFixturesAllCovered(t *testing.T) {
	entries, err := os.ReadDir(filepath.Join("testdata", "zpool"))
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		stem := strings.TrimSuffix(strings.TrimSuffix(e.Name(), ".json"), ".txt")
		if _, ok := expected[stem]; !ok {
			t.Errorf("fixture %s has no expectation", e.Name())
		}
	}
}

// The status line on all three of the reporter's pools is informational. It
// must arrive as text and nothing else: state ONLINE, every counter zero, no
// data errors. Whether a pool is unhealthy is decided from those fields alone.
func TestZpoolFeaturesNotEnabledIsHealthy(t *testing.T) {
	for _, name := range []string{"gh50-three-pools.txt", "gh50-three-pools.json"} {
		var pools []PoolInfo
		var err error
		if strings.HasSuffix(name, ".json") {
			pools, err = parseZpoolStatusJSON(readFixture(t, name))
		} else {
			pools, err = parseZpoolStatusText(readFixture(t, name))
		}
		if err != nil {
			t.Fatal(err)
		}
		if len(pools) != 3 {
			t.Fatalf("%s: %d pools, want 3", name, len(pools))
		}
		for _, p := range pools {
			if p.State != "ONLINE" || p.ReadErrors+p.WriteErrors+p.ChecksumErrors != 0 ||
				p.DataErrors == nil || *p.DataErrors != 0 {
				t.Errorf("%s: pool %s does not read as healthy: %+v", name, p.Name, p)
			}
			if p.Status == nil || !strings.HasPrefix(*p.Status, "Some supported and requested features") {
				t.Errorf("%s: pool %s status not passed through: %v", name, p.Name, p.Status)
			}
		}
	}
}

// The pool row of degraded-faulted reads 0 0 0. Its totals must come from the
// disks, or the pool would look error-free.
func TestZpoolTotalsComeFromEveryVdev(t *testing.T) {
	pools, err := parseZpoolStatusText(readFixture(t, "degraded-faulted.txt"))
	if err != nil {
		t.Fatal(err)
	}
	if got := pools[0]; got.ReadErrors != 18 || got.WriteErrors != 3 || got.ChecksumErrors != 2 {
		t.Errorf("totals %d/%d/%d, want 18/3/2", got.ReadErrors, got.WriteErrors, got.ChecksumErrors)
	}
}

func TestZpoolNoPools(t *testing.T) {
	pools, err := parseZpoolStatusText(nil)
	if err != nil || pools == nil || len(pools) != 0 {
		t.Errorf("empty text: pools=%v err=%v, want empty list", pools, err)
	}
	pools, err = parseZpoolStatusJSON([]byte(`{"output_version":{"command":"zpool status","vers_major":0,"vers_minor":1},"pools":{}}`))
	if err != nil || pools == nil || len(pools) != 0 {
		t.Errorf("empty json: pools=%v err=%v, want empty list", pools, err)
	}
	// Served as [] rather than null.
	if got := jsonOf(t, pools); got != "[]" {
		t.Errorf("empty list marshals as %s", got)
	}
}

func TestZpoolParseErrors(t *testing.T) {
	for name, in := range map[string]string{
		"not json":           `zpool: invalid option 'j'`,
		"no pools key":       `{"output_version":{}}`,
		"pool with no state": `{"pools":{"tank":{"name":"tank","vdevs":{"tank":{}}}}}`,
		"counter as nicenum": `{"pools":{"tank":{"name":"tank","state":"ONLINE","vdevs":{"tank":{"read_errors":"1.2K"}}}}}`,
	} {
		if _, err := parseZpoolStatusJSON([]byte(in)); !errors.Is(err, errZpoolParse) {
			t.Errorf("json %s: err = %v, want errZpoolParse", name, err)
		}
	}
	for name, in := range map[string]string{
		"pool with no state": "  pool: tank\nconfig:\n",
		"wrong first row":    "  pool: tank\n state: ONLINE\nconfig:\n\n\tNAME STATE READ WRITE CKSUM\n\tother ONLINE 0 0 0\n",
		"bad scrub date":     "  pool: tank\n state: ONLINE\n  scan: scrub repaired 0B in 00:00:01 with 0 errors on yesterday\n",
	} {
		if _, err := parseZpoolStatusText([]byte(in)); !errors.Is(err, errZpoolParse) {
			t.Errorf("text %s: err = %v, want errZpoolParse", name, err)
		}
	}
}

// Scan lines the fixtures do not carry, from print_scan_scrub_resilver_status
// and print_rebuild_status_impl.
func TestZpoolScanLines(t *testing.T) {
	cases := []struct {
		line           string
		function       string // "" = null
		state          string
		inProgress     bool
		end            string
		repaired, errs uint64
	}{
		{line: "none requested"},
		{line: "scrub repaired 0B in 00:00:32 with 0 errors on Tue Sep  1 01:00:33 2026",
			function: "SCRUB", state: "FINISHED", end: "2026-09-01T01:00:33Z"},
		{line: "scrub repaired 12K in 1 days 02:03:04 with 3 errors on Sat Oct 10 10:10:10 2026",
			function: "SCRUB", state: "FINISHED", end: "2026-10-10T10:10:10Z", repaired: 12288, errs: 3},
		{line: "scrub canceled on Tue Sep  1 01:00:33 2026", function: "SCRUB", state: "CANCELED"},
		{line: "scrub paused since Tue Sep  1 01:00:33 2026", function: "SCRUB", state: "SCANNING", inProgress: true},
		{line: "resilvered 1.21T in 05:00:00 with 0 errors on Tue Sep  1 01:00:33 2026", function: "RESILVER", state: "FINISHED"},
		{line: "resilvered (mirror-0) 1.21T in 05:00:00 with 0 errors on Tue Sep  1 01:00:33 2026", function: "RESILVER", state: "FINISHED"},
		{line: "resilver (draid1:4d:6c:1s-0) in progress since Tue Sep  1 01:00:33 2026", function: "RESILVER", state: "SCANNING"},
		{line: "resilver canceled on Tue Sep  1 01:00:33 2026", function: "RESILVER", state: "CANCELED"},
		{line: "something a later release prints"},
	}
	for _, c := range cases {
		p := PoolInfo{Name: "tank"}
		if err := applyScanText(&p, []string{c.line}); err != nil {
			t.Errorf("%q: %v", c.line, err)
			continue
		}
		want := PoolInfo{Name: "tank", ScrubInProgress: c.inProgress}
		if c.function != "" {
			want.ScanFunction, want.ScanState = sp(c.function), sp(c.state)
		}
		if c.end != "" {
			want.LastScrubEnd, want.LastScrubRepaired, want.LastScrubErrors = sp(c.end), up(c.repaired), up(c.errs)
		}
		if !reflect.DeepEqual(p, want) {
			t.Errorf("%q:\n got %s\nwant %s", c.line, jsonOf(t, p), jsonOf(t, want))
		}
	}
}

func TestZpoolJSONRebuildReadsAsResilver(t *testing.T) {
	in := `{"pools":{"d":{"name":"d","state":"DEGRADED","vdevs":{"d":{}},"error_count":0,
		"scan_stats":{"function":"NONE","state":"NONE","rebuild_stats":{"draid1-0":{"state":"ACTIVE"}}}}}}`
	pools, err := parseZpoolStatusJSON([]byte(in))
	if err != nil {
		t.Fatal(err)
	}
	if f, s := pools[0].ScanFunction, pools[0].ScanState; f == nil || *f != "RESILVER" || s == nil || *s != "SCANNING" {
		t.Errorf("rebuild read as %v/%v", f, s)
	}
}

func TestParseNiceNum(t *testing.T) {
	for in, want := range map[string]uint64{
		"0B": 0, "512B": 512, "1K": 1024, "1.50M": 1572864, "12.3G": 13207024435, "0": 0, "7": 7, "1.2K": 1229,
	} {
		var got uint64
		var err error
		if strings.HasSuffix(in, "B") {
			got, err = parseNiceBytes(in)
		} else {
			got, err = parseZFSCount(in)
		}
		if err != nil || got != want {
			t.Errorf("%s = %d, %v; want %d", in, got, err, want)
		}
	}
	for _, bad := range []string{"", "B", "-1", "1.2X", "lots"} {
		if _, err := parseZFSCount(bad); err == nil {
			t.Errorf("%q parsed", bad)
		}
	}
}

// The integration's tests read tests/fixtures/pools, which must be exactly what
// this agent serves at /api/pools for the same zpool fixtures.
func TestZpoolIntegrationFixturesMatchAgentOutput(t *testing.T) {
	for stem := range expected {
		pools, err := parseZpoolStatusJSON(readFixture(t, stem+".json"))
		if err != nil {
			t.Fatal(err)
		}
		served, err := json.Marshal(pools)
		if err != nil {
			t.Fatal(err)
		}
		raw, err := os.ReadFile(filepath.Join("..", "tests", "fixtures", "pools", stem+".json"))
		if err != nil {
			t.Fatalf("integration fixture for %s: %v", stem, err)
		}
		var want, got any
		if err := json.Unmarshal(raw, &want); err != nil {
			t.Fatal(err)
		}
		json.Unmarshal(served, &got)
		if !reflect.DeepEqual(got, want) {
			t.Errorf("tests/fixtures/pools/%s.json differs from what the agent serves", stem)
		}
	}
}
