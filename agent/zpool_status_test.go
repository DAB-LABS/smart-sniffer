package main

import (
	"encoding/json"
	"errors"
	"fmt"
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
//	spares.*               CONSTRUCTED. Two pools. vault: a FAULTED disk under a
//	                       DEGRADED raidz1, stood in for by a hot spare that has
//	                       finished resilvering (spare-2), a second disk with
//	                       only checksum errors, a log mirror, a cache device, a
//	                       spare INUSE and one AVAIL. fast: ONLINE with no
//	                       errors, its only spare UNAVAIL (libzfs_status.c does
//	                       not check spares, so zpool prints no status line).
//	                       Spare rows follow print_spares() and the isspare
//	                       branch of print_status_config(): a name and a state,
//	                       no counters.
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
	p := PoolInfo{Name: name, State: state, DataErrors: up(0), ProblemVdevs: []ProblemVdev{}}
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

func withProblems(vdevs ...ProblemVdev) func(*PoolInfo) {
	return func(p *PoolInfo) { p.ProblemVdevs = vdevs }
}

func pv(name, vtype, state string, r, w, c uint64) ProblemVdev {
	return ProblemVdev{Name: name, Type: vtype, State: state, ReadErrors: r, WriteErrors: w, ChecksumErrors: c}
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
			scrubbed("2026-09-27T05:36:09Z", 1572864, 0),
			withProblems(
				pv("raidz2-0", "raidz", "DEGRADED", 0, 0, 0),
				pv("ata-WDC_WD80EFAX-68KNBN0_VAGX0003", "disk", "FAULTED", 18, 3, 0),
				pv("ata-WDC_WD80EFAX-68KNBN0_VAGX0005", "disk", "ONLINE", 0, 0, 2),
			)),
	},
	"data-errors": {
		pool("backup", "ONLINE",
			withStatus(
				"One or more devices has experienced an error resulting in data corruption. "+
					"Applications may be affected.",
				"Restore the file in question if possible. Otherwise restore the entire pool from backup."),
			withErrors(0, 0, 16),
			scrubbed("2026-09-13T00:24:02Z", 0, 2),
			func(p *PoolInfo) { p.DataErrors = up(2) },
			withProblems(
				pv("mirror-0", "mirror", "ONLINE", 0, 0, 4),
				pv("ata-ST2000DM008-2FR102_ZFL0001", "disk", "ONLINE", 0, 0, 4),
				pv("ata-ST2000DM008-2FR102_ZFL0002", "disk", "ONLINE", 0, 0, 4),
			)),
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
			func(p *PoolInfo) { p.ScanFunction, p.ScanState = sp("RESILVER"), sp("SCANNING") },
			withProblems(
				pv("mirror-0", "mirror", "DEGRADED", 0, 0, 0),
				pv("replacing-1", "replacing", "DEGRADED", 0, 0, 0),
				pv("ata-ST4000VN008-2DR166_ZDH0002", "disk", "FAULTED", 0, 37, 0),
			)),
	},
	"spares": {
		pool("fast", "ONLINE",
			scrubbed("2026-09-13T00:32:52Z", 0, 0),
			withProblems(pv("sdh", "disk", "UNAVAIL", 0, 0, 0))),
		pool("vault", "DEGRADED",
			withStatus(
				"One or more devices are faulted in response to persistent errors. Sufficient replicas "+
					"exist for the pool to continue functioning in a degraded state.",
				"Replace the faulted device, or use 'zpool clear' to mark the device repaired."),
			withErrors(12, 0, 5),
			func(p *PoolInfo) { p.ScanFunction, p.ScanState = sp("RESILVER"), sp("FINISHED") },
			withProblems(
				pv("raidz1-0", "raidz", "DEGRADED", 0, 0, 0),
				pv("sdb", "disk", "ONLINE", 0, 0, 5),
				pv("spare-2", "spare", "DEGRADED", 0, 0, 0),
				pv("sdc", "disk", "FAULTED", 12, 0, 0),
			)),
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
			// Health fields only; the layout has its own tests below.
			fromJSON, fromText = healthOnly(fromJSON), healthOnly(fromText)
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

// Healthy pools list no problem vdevs, as [] and never null.
func TestZpoolHealthyPoolsListNoProblemVdevs(t *testing.T) {
	for _, stem := range []string{"gh50-three-pools", "scrub-in-progress", "never-scrubbed"} {
		for _, form := range []string{".json", ".txt"} {
			var pools []PoolInfo
			var err error
			if form == ".json" {
				pools, err = parseZpoolStatusJSON(readFixture(t, stem+form))
			} else {
				pools, err = parseZpoolStatusText(readFixture(t, stem+form))
			}
			if err != nil {
				t.Fatal(err)
			}
			for _, p := range pools {
				if p.ProblemVdevs == nil || len(p.ProblemVdevs) != 0 {
					t.Errorf("%s%s: %s problem_vdevs = %#v, want []", stem, form, p.Name, p.ProblemVdevs)
				}
				b, _ := json.Marshal(p)
				if !strings.Contains(string(b), `"problem_vdevs":[]`) {
					t.Errorf("%s%s: %s serves %s", stem, form, p.Name, b)
				}
			}
		}
	}
}

// A spare standing in for a disk (INUSE) and one waiting (AVAIL) are not
// problems; the disk the spare replaced is, and so is a spare that cannot be
// opened. sde is in the vault pool twice: in the tree under spare-2, ONLINE,
// and in the spares list, INUSE. Neither is listed.
func TestZpoolSparesInUseAndAvailableAreNotProblems(t *testing.T) {
	for _, form := range []string{".json", ".txt"} {
		var pools []PoolInfo
		var err error
		if form == ".json" {
			pools, err = parseZpoolStatusJSON(readFixture(t, "spares"+form))
		} else {
			pools, err = parseZpoolStatusText(readFixture(t, "spares"+form))
		}
		if err != nil {
			t.Fatal(err)
		}
		for _, p := range pools {
			for _, v := range p.ProblemVdevs {
				if v.Name == "sde" || v.Name == "sdf" || v.State == "AVAIL" || v.State == "INUSE" {
					t.Errorf("spares%s: %s lists %+v", form, p.Name, v)
				}
			}
		}
	}
}

// Problem vdevs come out in the order zpool wrote them, not sorted by name.
func TestZpoolJSONProblemVdevsKeepTreeOrder(t *testing.T) {
	in := `{"pools":{"t":{"name":"t","state":"DEGRADED","error_count":0,"vdevs":{"t":{"name":"t","vdev_type":"root","state":"DEGRADED",
		"read_errors":0,"write_errors":0,"checksum_errors":0,"vdevs":{
		"mirror-1":{"name":"mirror-1","vdev_type":"mirror","state":"DEGRADED","read_errors":0,"write_errors":0,"checksum_errors":0,"vdevs":{
			"sdz":{"name":"sdz","vdev_type":"disk","state":"REMOVED","read_errors":0,"write_errors":0,"checksum_errors":0},
			"sda":{"name":"sda","vdev_type":"disk","state":"ONLINE","read_errors":1,"write_errors":0,"checksum_errors":0}}},
		"mirror-0":{"name":"mirror-0","vdev_type":"mirror","state":"ONLINE","read_errors":0,"write_errors":0,"checksum_errors":3}}}},
		"logs":{"sdq":{"name":"sdq","vdev_type":"disk","state":"OFFLINE","read_errors":0,"write_errors":0,"checksum_errors":0}},
		"spares":{"sdy":{"name":"sdy","vdev_type":"disk","state":"AVAIL"},"sdx":{"name":"sdx","vdev_type":"disk","state":"FAULTED"}}}}}`
	pools, err := parseZpoolStatusJSON([]byte(in))
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, v := range pools[0].ProblemVdevs {
		names = append(names, v.Name)
	}
	if got, want := strings.Join(names, " "), "mirror-1 sdz sda mirror-0 sdq sdx"; got != want {
		t.Errorf("order %q, want %q", got, want)
	}
}

// The text form has no vdev_type; it is derived from the name zpool prints.
func TestVdevTypeFromName(t *testing.T) {
	for name, want := range map[string]string{
		"mirror-0": "mirror", "raidz1-0": "raidz", "raidz3-12": "raidz", "draid2:4d:12c:1s-0": "draid",
		"draid2-0-1": "dspare", "replacing-1": "replacing", "spare-2": "spare", "missing-3": "missing",
		"sdb": "disk", "ata-WDC_WD80EFAX-68KNBN0_VAGX0001-part1": "disk", "nvme0n1": "disk",
		"dm-0": "disk", "16009123456789012345": "disk", "/dev/sdb1": "disk", "/tank/file0": "file",
		"mirror": "disk",
	} {
		if got := vdevTypeFromName(name); got != want {
			t.Errorf("%s: %s, want %s", name, got, want)
		}
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

// ---------------------------------------------------------------------------
// Layout (v0.8.3): the whole vdev tree, served beside problem_vdevs.
// ---------------------------------------------------------------------------

// healthOnly returns pools with the layout fields cleared, for comparisons
// that predate the layout.
func healthOnly(pools []PoolInfo) []PoolInfo {
	out := make([]PoolInfo, len(pools))
	for i, p := range pools {
		p.Vdevs, p.Logs, p.Cache, p.Spares, p.Special, p.Dedup = nil, nil, nil, nil, nil, nil
		p.LayoutTruncated = false
		out[i] = p
	}
	return out
}

// withoutPaths returns the layout with every path cleared, for comparing the
// JSON reader with the text reader, which has no paths.
func withoutPaths(vs []PoolVdev) []PoolVdev {
	if vs == nil {
		return nil
	}
	out := make([]PoolVdev, len(vs))
	for i, v := range vs {
		v.Path = ""
		v.Children = withoutPaths(v.Children)
		out[i] = v
	}
	return out
}

func layoutOf(p PoolInfo) map[string][]PoolVdev {
	return map[string][]PoolVdev{
		"vdevs": p.Vdevs, "logs": p.Logs, "cache": p.Cache,
		"spares": p.Spares, "special": p.Special, "dedup": p.Dedup,
	}
}

func lv(name, vtype, state string, r, w, c uint64, path string, children ...PoolVdev) PoolVdev {
	return PoolVdev{Name: name, Type: vtype, State: state, ReadErrors: r, WriteErrors: w, ChecksumErrors: c,
		Path: path, Children: children}
}

func poolNamed(t *testing.T, pools []PoolInfo, name string) PoolInfo {
	t.Helper()
	for _, p := range pools {
		if p.Name == name {
			return p
		}
	}
	t.Fatalf("pool %s not found", name)
	return PoolInfo{}
}

// The spares fixture has every group but special and dedup: a raidz1 with a
// hot spare standing in, a log mirror, a cache device and two spares.
func TestZpoolLayoutJSONSpares(t *testing.T) {
	pools, err := parseZpoolStatusJSON(readFixture(t, "spares.json"))
	if err != nil {
		t.Fatal(err)
	}
	vault := poolNamed(t, pools, "vault")
	logDisk := func(n string) PoolVdev {
		return lv("nvme-Samsung_SSD_970_EVO_Plus_250GB_S4EUNX000"+n+"-part1", "disk", "ONLINE", 0, 0, 0,
			"/dev/disk/by-id/nvme-Samsung_SSD_970_EVO_Plus_250GB_S4EUNX000"+n+"-part1-part1")
	}
	want := map[string][]PoolVdev{
		"vdevs": {lv("raidz1-0", "raidz", "DEGRADED", 0, 0, 0, "",
			lv("sda", "disk", "ONLINE", 0, 0, 0, "/dev/sda1"),
			lv("sdb", "disk", "ONLINE", 0, 0, 5, "/dev/sdb1"),
			lv("spare-2", "spare", "DEGRADED", 0, 0, 0, "",
				lv("sdc", "disk", "FAULTED", 12, 0, 0, "/dev/sdc1"),
				lv("sde", "disk", "ONLINE", 0, 0, 0, "/dev/sde1")),
			lv("sdd", "disk", "ONLINE", 0, 0, 0, "/dev/sdd1"),
		)},
		"logs":    {lv("mirror-1", "mirror", "ONLINE", 0, 0, 0, "", logDisk("2"), logDisk("3"))},
		"cache":   {lv("nvme-WD_Red_SN700_500GB_22002", "disk", "ONLINE", 0, 0, 0, "/dev/disk/by-id/nvme-WD_Red_SN700_500GB_22002-part1")},
		"spares":  {lv("sde", "disk", "INUSE", 0, 0, 0, "/dev/sde1"), lv("sdf", "disk", "AVAIL", 0, 0, 0, "/dev/sdf1")},
		"special": nil,
		"dedup":   nil,
	}
	if got := layoutOf(vault); !reflect.DeepEqual(got, want) {
		t.Errorf("vault layout:\n%s\nwant:\n%s", jsonOf(t, got), jsonOf(t, want))
	}
	if vault.LayoutTruncated {
		t.Error("vault layout marked truncated")
	}
	// problem_vdevs is unchanged by the layout.
	if len(vault.ProblemVdevs) != 4 {
		t.Errorf("problem_vdevs changed: %+v", vault.ProblemVdevs)
	}
}

// Both readers build the same tree; only the JSON reader has paths.
func TestZpoolLayoutTextMatchesJSON(t *testing.T) {
	for stem := range expected {
		if stem == "gh50-three-pools" {
			continue // the reporter's redaction made disk names repeat in the text; see below
		}
		t.Run(stem, func(t *testing.T) {
			fromJSON, err := parseZpoolStatusJSON(readFixture(t, stem+".json"))
			if err != nil {
				t.Fatal(err)
			}
			fromText, err := parseZpoolStatusText(readFixture(t, stem+".txt"))
			if err != nil {
				t.Fatal(err)
			}
			for i := range fromJSON {
				j, x := layoutOf(fromJSON[i]), layoutOf(fromText[i])
				for group := range j {
					j[group] = withoutPaths(j[group])
				}
				if !reflect.DeepEqual(j, x) {
					t.Errorf("pool %s: text layout:\n%s\njson layout without paths:\n%s",
						fromJSON[i].Name, jsonOf(t, x), jsonOf(t, j))
				}
				if len(fromJSON[i].Vdevs) == 0 {
					t.Errorf("pool %s: no data vdevs", fromJSON[i].Name)
				}
			}
		})
	}
}

// The real GH #50 paste (spaces for tabs, redacted names): the text reader
// still finds the tree's shape.
func TestZpoolLayoutTextFromPastedOutput(t *testing.T) {
	pools, err := parseZpoolStatusText(readFixture(t, "gh50-three-pools.txt"))
	if err != nil {
		t.Fatal(err)
	}
	raid10 := poolNamed(t, pools, "raid10")
	if len(raid10.Vdevs) != 6 {
		t.Fatalf("raid10: %d top-level vdevs, want 6 mirrors", len(raid10.Vdevs))
	}
	for i, m := range raid10.Vdevs {
		if m.Name != "mirror-"+string(rune('0'+i)) || m.Type != "mirror" || len(m.Children) != 2 {
			t.Errorf("raid10 vdev %d: %+v", i, m)
		}
		for _, d := range m.Children {
			if d.Type != "disk" || d.State != "ONLINE" || len(d.Children) != 0 || d.Path != "" {
				t.Errorf("raid10 %s disk: %+v", m.Name, d)
			}
		}
	}
	fromJSON, err := parseZpoolStatusJSON(readFixture(t, "gh50-three-pools.json"))
	if err != nil {
		t.Fatal(err)
	}
	shape := func(vs []PoolVdev) string {
		var b strings.Builder
		var walk func([]PoolVdev, int)
		walk = func(vs []PoolVdev, d int) {
			for _, v := range vs {
				b.WriteString(strings.Repeat(" ", d) + v.Type + " " + v.State + "\n")
				walk(v.Children, d+1)
			}
		}
		walk(vs, 0)
		return b.String()
	}
	for i := range pools {
		if a, b := shape(pools[i].Vdevs), shape(fromJSON[i].Vdevs); a != b {
			t.Errorf("pool %s shape differs:\ntext:\n%s\njson:\n%s", pools[i].Name, a, b)
		}
	}
}

// Served JSON: empty groups are left out, interior vdevs have no path, and
// problem_vdevs is still [] on a healthy pool.
func TestZpoolLayoutJSONShape(t *testing.T) {
	pools, err := parseZpoolStatusJSON(readFixture(t, "never-scrubbed.json"))
	if err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(pools[0])
	var m map[string]any
	json.Unmarshal(b, &m)
	for _, absent := range []string{"logs", "cache", "spares", "special", "dedup", "layout_truncated"} {
		if _, ok := m[absent]; ok {
			t.Errorf("%s present on a pool without it: %s", absent, b)
		}
	}
	if pv, ok := m["problem_vdevs"].([]any); !ok || len(pv) != 0 {
		t.Errorf("problem_vdevs = %v, want []", m["problem_vdevs"])
	}
	vdevs, ok := m["vdevs"].([]any)
	if !ok || len(vdevs) == 0 {
		t.Fatalf("vdevs missing: %s", b)
	}

	pools, err = parseZpoolStatusJSON(readFixture(t, "data-errors.json"))
	if err != nil {
		t.Fatal(err)
	}
	b, _ = json.Marshal(pools[0].Vdevs[0])
	if strings.Contains(string(b[:strings.Index(string(b), "children")]), `"path"`) {
		t.Errorf("interior vdev carries a path: %s", b)
	}
	if !strings.Contains(string(b), `"path":"/dev/`) {
		t.Errorf("leaf disks carry no path: %s", b)
	}
}

// Special and dedup vdevs are their own groups.
func TestZpoolLayoutSpecialAndDedup(t *testing.T) {
	in := `{"pools":{"t":{"name":"t","state":"ONLINE","error_count":0,
		"vdevs":{"t":{"name":"t","vdev_type":"root","state":"ONLINE","vdevs":{
			"sda":{"name":"sda","vdev_type":"disk","path":"/dev/sda1","state":"ONLINE"}}}},
		"special":{"mirror-1":{"name":"mirror-1","vdev_type":"mirror","state":"ONLINE","vdevs":{
			"nvme0n1":{"name":"nvme0n1","vdev_type":"disk","path":"/dev/nvme0n1p1","state":"ONLINE"},
			"nvme1n1":{"name":"nvme1n1","vdev_type":"disk","path":"/dev/nvme1n1p1","state":"ONLINE","checksum_errors":2}}}},
		"dedup":{"sdq":{"name":"sdq","vdev_type":"disk","path":"/dev/sdq1","state":"ONLINE"}}}}}`
	pools, err := parseZpoolStatusJSON([]byte(in))
	if err != nil {
		t.Fatal(err)
	}
	p := pools[0]
	if len(p.Special) != 1 || len(p.Special[0].Children) != 2 || p.Special[0].Children[1].ChecksumErrors != 2 {
		t.Errorf("special: %+v", p.Special)
	}
	if len(p.Dedup) != 1 || p.Dedup[0].Path != "/dev/sdq1" {
		t.Errorf("dedup: %+v", p.Dedup)
	}

	text := "  pool: t\n state: ONLINE\nconfig:\n\n" +
		"\tNAME         STATE     READ WRITE CKSUM\n" +
		"\tt            ONLINE       0     0     0\n" +
		"\t  sda        ONLINE       0     0     0\n" +
		"\tdedup\n" +
		"\t  sdq        ONLINE       0     0     0\n" +
		"\tspecial\n" +
		"\t  mirror-1   ONLINE       0     0     0\n" +
		"\t    nvme0n1  ONLINE       0     0     0\n" +
		"\t    nvme1n1  ONLINE       0     0     2\n" +
		"\nerrors: No known data errors\n"
	tp, err := parseZpoolStatusText([]byte(text))
	if err != nil {
		t.Fatal(err)
	}
	if got, want := layoutOf(tp[0]), layoutOf(p); !reflect.DeepEqual(got, map[string][]PoolVdev{
		"vdevs": withoutPaths(want["vdevs"]), "logs": nil, "cache": nil, "spares": nil,
		"special": withoutPaths(want["special"]), "dedup": withoutPaths(want["dedup"]),
	}) {
		t.Errorf("text layout:\n%s\njson:\n%s", jsonOf(t, got), jsonOf(t, want))
	}
}

// A pool with more vdevs than the bound is cut short and says so; the health
// fields still count every vdev.
func TestZpoolLayoutBounded(t *testing.T) {
	var disks, rows []string
	n := maxLayoutVdevs + 10
	for i := 0; i < n; i++ {
		disks = append(disks, fmt.Sprintf(`"d%d":{"name":"d%d","vdev_type":"disk","state":"ONLINE","checksum_errors":1}`, i, i))
		rows = append(rows, fmt.Sprintf("\t  d%-6d ONLINE       0     0     1", i))
	}
	in := `{"pools":{"big":{"name":"big","state":"ONLINE","error_count":0,"vdevs":{"big":{"name":"big","vdev_type":"root","state":"ONLINE","vdevs":{` +
		strings.Join(disks, ",") + `}}}}}}`
	text := "  pool: big\n state: ONLINE\nconfig:\n\n\tNAME     STATE     READ WRITE CKSUM\n\tbig      ONLINE       0     0     0\n" +
		strings.Join(rows, "\n") + "\n\nerrors: No known data errors\n"
	fromJSON, err := parseZpoolStatusJSON([]byte(in))
	if err != nil {
		t.Fatal(err)
	}
	fromText, err := parseZpoolStatusText([]byte(text))
	if err != nil {
		t.Fatal(err)
	}
	for form, p := range map[string]PoolInfo{"json": fromJSON[0], "text": fromText[0]} {
		if len(p.Vdevs) != maxLayoutVdevs || !p.LayoutTruncated {
			t.Errorf("%s: %d vdevs, truncated %v; want %d, true", form, len(p.Vdevs), p.LayoutTruncated, maxLayoutVdevs)
		}
		if p.ChecksumErrors != uint64(n) || len(p.ProblemVdevs) != n {
			t.Errorf("%s: health fields cut short: %d errors, %d problem vdevs", form, p.ChecksumErrors, len(p.ProblemVdevs))
		}
	}
}

// Rows nested deeper than the bound are left out, with the vdevs under them.
func TestZpoolLayoutTextDepthBound(t *testing.T) {
	var b strings.Builder
	b.WriteString("  pool: deep\n state: ONLINE\nconfig:\n\n\tNAME  STATE  READ WRITE CKSUM\n\tdeep  ONLINE  0 0 0\n")
	for d := 1; d <= maxLayoutDepth+3; d++ {
		fmt.Fprintf(&b, "\t%smirror-%d  ONLINE  0 0 0\n", strings.Repeat("  ", d), d)
	}
	b.WriteString("\t  sdz  ONLINE  0 0 0\n\nerrors: No known data errors\n")
	pools, err := parseZpoolStatusText([]byte(b.String()))
	if err != nil {
		t.Fatal(err)
	}
	p := pools[0]
	if !p.LayoutTruncated {
		t.Error("not marked truncated")
	}
	depth, v := 0, p.Vdevs
	for len(v) > 0 {
		depth++
		v = v[0].Children
	}
	if depth != maxLayoutDepth {
		t.Errorf("depth %d, want %d", depth, maxLayoutDepth)
	}
	// The row back at depth 1 after the deep chain is a top-level vdev again.
	if len(p.Vdevs) != 2 || p.Vdevs[1].Name != "sdz" {
		t.Errorf("top level: %+v", p.Vdevs)
	}
}
