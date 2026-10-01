// ZFS pool health, read from `zpool status` (GH #50).
//
// v0.6.0 already reports how full each pool is through the filesystem
// fallback (filesystem_zfs.go). This file reports whether each pool is
// healthy: its state, read/write/checksum error totals, the count behind the
// "errors:" line, and the last scrub.
//
// The error totals are the sums of the READ, WRITE and CKSUM columns over
// every vdev in the pool, not the pool's own row. OpenZFS counts an I/O error
// on the vdev the I/O was issued to (zio.c, zio_done and zio_checksum_verify),
// so a disk returning errors that redundancy repaired shows them on the disk's
// row while the pool row stays at zero. Reading the pool row alone would miss
// exactly the early warning this exists for.
//
// Two readers, decided by trying rather than by parsing a version string:
//
//   - `zpool status -j --json-int` (OpenZFS 2.3 and later). Stable keys and
//     exact integers, including the bytes a scrub repaired.
//   - `zpool status -p` text (every OpenZFS release). Exact error counts, but
//     the scan line is always printed with zfs_nicebytes, so the bytes a scrub
//     repaired are only as precise as that ("1.50M").
//
// Both readers produce the same PoolInfo for the same pool. Only pool-level
// data is kept: the vdev tree is walked to sum its counters and is not served
// (owner ruling 2026-09-30, pool level only).
//
// ZFS is never required. No zpool binary means pool status is off, nothing is
// advertised and nothing is logged. A zpool that exists but fails (module not
// loaded, permissions) reports zero pools and logs through the shared throttle,
// once and then at most hourly.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// PoolInfo is the per-pool payload served by GET /api/pools.
type PoolInfo struct {
	Name  string `json:"name"`
	State string `json:"state"` // ONLINE, DEGRADED, FAULTED, ... as zpool prints it

	// The pool's "status:" and "action:" text, whitespace collapsed to single
	// spaces, or null when zpool printed none. Informational only: a status
	// such as "Some supported and requested features are not enabled" says
	// nothing about the pool's health, which is why nothing here interprets it.
	Status *string `json:"status"`
	Action *string `json:"action"`

	// Sums of the READ, WRITE and CKSUM columns over every vdev in the pool:
	// the pool row, top-level and interior vdevs, disks, and the log, cache,
	// special and dedup vdevs. One fault can show at more than one level, so
	// these are an indicator rather than a count of bad blocks. They reset on
	// `zpool clear`.
	ReadErrors     uint64 `json:"read_errors"`
	WriteErrors    uint64 `json:"write_errors"`
	ChecksumErrors uint64 `json:"checksum_errors"`

	// Count behind the "errors:" line: 0 for "No known data errors", N for
	// "N data errors, use '-v' for a list". Null when zpool printed no errors
	// line, which happens when the pool's configuration cannot be read.
	DataErrors *uint64 `json:"data_errors"`

	// The most recent scan zpool knows about. A pool keeps one scan record, so
	// a resilver that ran after the last scrub replaces it.
	ScanFunction *string `json:"scan_function"` // SCRUB, RESILVER, ERRORSCRUB, NONE; null when no scan was ever run
	ScanState    *string `json:"scan_state"`    // SCANNING, FINISHED, CANCELED; null with scan_function

	// Scrub fields, filled only when the last scan was a scrub.
	ScrubInProgress   bool    `json:"scrub_in_progress"`   // also true while a scrub is paused
	LastScrubEnd      *string `json:"last_scrub_end"`      // RFC 3339 UTC, last completed scrub
	LastScrubRepaired *uint64 `json:"last_scrub_repaired"` // bytes repaired by that scrub
	LastScrubErrors   *uint64 `json:"last_scrub_errors"`   // errors that scrub found
}

// zpoolTimeout bounds one zpool run. A var so tests can shorten it.
var zpoolTimeout = 10 * time.Second

// zpoolSearchPaths are checked, in order, when zpool is not on PATH. The agent
// often runs from a service manager whose PATH lacks the sbin directories.
var zpoolSearchPaths = []string{
	"/usr/sbin/zpool",                  // Debian, Ubuntu, Proxmox, RHEL (zfs-on-linux)
	"/sbin/zpool",                      // older layouts, Alpine
	"/usr/local/sbin/zpool",            // FreeBSD ports, TrueNAS CORE
	"/usr/local/zfs/bin/zpool",         // OpenZFS on OS X
	"/run/current-system/sw/bin/zpool", // NixOS
	"/usr/bin/zpool",                   // Arch (zfs-utils), Fedora usrmerge
}

// Errors a read can end in. Each is logged through the throttle, never per poll.
var (
	errZpoolTimeout = errors.New("zpool status timed out")
	errZpoolParse   = errors.New("zpool status output could not be parsed")
)

// zpool output readers.
const (
	zpoolModeUnknown = iota // not decided yet, or the last decision stopped working
	zpoolModeJSON
	zpoolModeText
)

// PoolCache holds the last pool read. It is refreshed on the drive cache's
// cycle, in the background, so a zpool stuck in the kernel (a suspended pool
// can do that) never holds up SMART data or an HTTP request.
type PoolCache struct {
	mu        sync.RWMutex
	pools     []PoolInfo
	zpoolPath string
	mode      int
	running   bool
	logs      *logThrottle
}

// resolveZpoolPath finds the zpool binary. A configured path is used as given
// and must exist; otherwise PATH is searched, then the usual install
// locations. The bool is false when there is no zpool on this machine.
func resolveZpoolPath(configured string) (string, bool) {
	if configured != "" {
		if isExecutableFile(configured) {
			return configured, true
		}
		return configured, false
	}
	if p, err := exec.LookPath("zpool"); err == nil {
		return p, true
	}
	for _, candidate := range zpoolSearchPaths {
		if isExecutableFile(candidate) {
			return candidate, true
		}
	}
	return "", false
}

// isExecutableFile reports whether path is a regular file with an execute bit.
func isExecutableFile(path string) bool {
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		return false
	}
	return info.Mode()&0o111 != 0
}

// NewPoolCacheFromConfig returns the pool cache for this agent, or nil when
// pool status is disabled or there is no zpool. A machine that has never had
// ZFS logs nothing here unless verbose logging is on; a zpool_path that was
// configured but does not exist is a mistake worth one line.
func NewPoolCacheFromConfig(cfg *Config) *PoolCache {
	if !cfg.PoolStatusEnabled() {
		if cfg.Verbose {
			log.Print("zfs pool status: disabled by zfs_pool_status: false")
		}
		return nil
	}
	path, found := resolveZpoolPath(cfg.ZpoolPath)
	if !found {
		if cfg.ZpoolPath != "" {
			log.Printf("WARNING: zpool_path %q is not an executable file; ZFS pool status disabled", cfg.ZpoolPath)
		} else if cfg.Verbose {
			log.Print("zfs pool status: no zpool binary found; ZFS pool status disabled")
		}
		return nil
	}
	return NewPoolCache(path, cfg.Verbose)
}

// NewPoolCache builds a cache that reads pools with the zpool at path.
func NewPoolCache(path string, verbose bool) *PoolCache {
	return &PoolCache{
		zpoolPath: path,
		logs:      newLogThrottle(logReminderInterval, verbose),
	}
}

// Count returns how many pools the last read found.
func (pc *PoolCache) Count() int {
	pc.mu.RLock()
	defer pc.mu.RUnlock()
	return len(pc.pools)
}

// Pools returns a copy of the last read.
func (pc *PoolCache) Pools() []PoolInfo {
	pc.mu.RLock()
	defer pc.mu.RUnlock()
	out := make([]PoolInfo, len(pc.pools))
	copy(out, pc.pools)
	return out
}

// HandlePools serves GET /api/pools from the cache. It never runs zpool.
func (pc *PoolCache) HandlePools(w http.ResponseWriter, r *http.Request) {
	data := pc.Pools()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(data)
}

// Refresh starts a background read and returns a channel closed when it ends.
// If the previous read is still running, no new one starts and the channel is
// already closed: one zpool at a time, however long the kernel holds it.
func (pc *PoolCache) Refresh() <-chan struct{} {
	done := make(chan struct{})
	pc.mu.Lock()
	if pc.running {
		pc.mu.Unlock()
		if pc.logs.shouldLog("zpool-busy", "busy") {
			log.Print("zfs pool status: previous zpool status still running; skipping this cycle")
		}
		close(done)
		return done
	}
	pc.running = true
	pc.mu.Unlock()

	go func() {
		defer close(done)
		pc.refreshNow()
		pc.mu.Lock()
		pc.running = false
		pc.mu.Unlock()
	}()
	return done
}

// refreshNow reads the pools synchronously and stores the result.
func (pc *PoolCache) refreshNow() {
	pools, mode, err := pc.read()

	pc.mu.Lock()
	pc.mode = mode
	if err != nil {
		pc.pools = nil
	} else {
		pc.pools = pools
	}
	pc.mu.Unlock()

	if err != nil {
		// Stable discriminator: a zpool that keeps failing logs once and then
		// hourly, even if its error text varies between polls.
		if pc.logs.shouldLog("zpool", "failed") {
			log.Printf("WARNING: zfs pool status: %v; reporting no pools", err)
		}
		return
	}
	reader := "json"
	if mode == zpoolModeText {
		reader = "text"
	}
	if msg := fmt.Sprintf("zfs pool status: %d pool(s) via zpool status (%s)", len(pools), reader); pc.logs.shouldLog("zpool", msg) {
		log.Print(msg)
	}
}

// read runs zpool in the decided mode, or decides one. JSON is tried first;
// text is the fallback for releases without -j. A failure in a decided mode
// resets the decision so the next poll tries both again.
func (pc *PoolCache) read() ([]PoolInfo, int, error) {
	pc.mu.RLock()
	mode := pc.mode
	pc.mu.RUnlock()

	switch mode {
	case zpoolModeJSON:
		pools, err := pc.readJSON()
		if err != nil {
			return nil, zpoolModeUnknown, err
		}
		return pools, zpoolModeJSON, nil
	case zpoolModeText:
		pools, err := pc.readText()
		if err != nil {
			return nil, zpoolModeUnknown, err
		}
		return pools, zpoolModeText, nil
	}

	pools, jsonErr := pc.readJSON()
	if jsonErr == nil {
		return pools, zpoolModeJSON, nil
	}
	if errors.Is(jsonErr, errZpoolTimeout) {
		// A zpool that hangs on -j will hang on text too; do not wait twice.
		return nil, zpoolModeUnknown, jsonErr
	}
	pools, textErr := pc.readText()
	if textErr == nil {
		return pools, zpoolModeText, nil
	}
	return nil, zpoolModeUnknown, fmt.Errorf("%v (json: %v)", textErr, jsonErr)
}

func (pc *PoolCache) readJSON() ([]PoolInfo, error) {
	out, err := runZpool(pc.zpoolPath, "status", "-j", "--json-int")
	if err != nil {
		return nil, err
	}
	return parseZpoolStatusJSON(out)
}

func (pc *PoolCache) readText() ([]PoolInfo, error) {
	out, err := runZpool(pc.zpoolPath, "status", "-p")
	if err != nil {
		return nil, err
	}
	return parseZpoolStatusText(out)
}

// runZpool runs zpool with a timeout and returns its stdout.
//
// The environment pins the C locale and UTC: the text reader matches English
// words and parses the scan line's ctime() date, which zpool prints in local
// time. NO_COLOR keeps escape codes out even if ZFS_COLOR is set.
func runZpool(path string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), zpoolTimeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, path, args...)
	cmd.Env = append(os.Environ(), "LC_ALL=C", "LANG=C", "TZ=UTC", "NO_COLOR=1")
	// If zpool is a wrapper whose child keeps the pipes open, Wait would block
	// past the kill. Same guard as runSmartctl.
	cmd.WaitDelay = 2 * time.Second
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("%w after %s", errZpoolTimeout, zpoolTimeout)
	}
	if err != nil {
		detail := strings.TrimSpace(stderr.String())
		if i := strings.IndexByte(detail, '\n'); i >= 0 {
			detail = detail[:i]
		}
		if detail != "" {
			return nil, fmt.Errorf("%s %s: %v: %s", path, strings.Join(args, " "), err, detail)
		}
		return nil, fmt.Errorf("%s %s: %v", path, strings.Join(args, " "), err)
	}
	return stdout.Bytes(), nil
}

// ---------------------------------------------------------------------------
// Shared normalisation
// ---------------------------------------------------------------------------

// knownDataErrorsNone is the errors line of a pool with no data errors.
const knownDataErrorsNone = "No known data errors"

var whitespaceRun = regexp.MustCompile(`\s+`)

// collapseText joins zpool's wrapped status/action lines into one line.
// JSON carries "\n\t" where the text form wraps; both become single spaces.
func collapseText(s string) *string {
	s = strings.TrimSpace(whitespaceRun.ReplaceAllString(s, " "))
	if s == "" {
		return nil
	}
	return &s
}

func strPtr(s string) *string { return &s }
func u64Ptr(v uint64) *uint64 { return &v }

// applyScan fills the scan fields of p from a function, a state, and the
// scrub's figures. Scrub fields are only set for a scrub: a resilver is not a
// scrub, and the last completed scrub is only known when the scan record is
// still that scrub's.
func applyScan(p *PoolInfo, function, state string, end time.Time, repaired, errs uint64) {
	p.ScanFunction = strPtr(function)
	p.ScanState = strPtr(state)
	if function != "SCRUB" {
		return
	}
	switch state {
	case "SCANNING":
		p.ScrubInProgress = true
	case "FINISHED":
		p.LastScrubEnd = strPtr(end.UTC().Format(time.RFC3339))
		p.LastScrubRepaired = u64Ptr(repaired)
		p.LastScrubErrors = u64Ptr(errs)
	}
}

func sortPools(pools []PoolInfo) {
	sort.Slice(pools, func(i, j int) bool { return pools[i].Name < pools[j].Name })
}

// ---------------------------------------------------------------------------
// JSON reader: zpool status -j --json-int (OpenZFS 2.3+)
// ---------------------------------------------------------------------------
//
// Shape from cmd/zpool/zpool_main.c, status_callback_json() and helpers, at
// zfs-2.3.0 through zfs-2.4.4 (unchanged across them):
//
//	{"output_version": {...},
//	 "pools": {"<name>": {
//	     "name", "state", "pool_guid", "txg", "spa_version", "zpl_version",
//	     "status"?, "action"?, "msgid"?, "moreinfo"?,
//	     "scan_stats"?: {"function", "state", "start_time", "end_time",
//	                     "processed", "errors", ..., "rebuild_stats"?},
//	     "vdevs": {"<name>": {root vdev: "read_errors", "write_errors",
//	                          "checksum_errors", "vdevs": {...}}},
//	     "dedup"?, "special"?, "logs"?, "l2cache"?: {"<name>": {vdev}},
//	     "spares"?: {"<name>": {state only, no counters}},
//	     "error_count": N}}}
//
// With --json-int every counter and timestamp is a JSON number (timestamps in
// seconds since the epoch); without it they are nicenum strings.

// zfsNum accepts a JSON number or a numeric string, so output without
// --json-int still reads where the strings happen to be plain integers.
type zfsNum uint64

func (n *zfsNum) UnmarshalJSON(b []byte) error {
	s := strings.Trim(string(b), `"`)
	v, err := strconv.ParseUint(s, 10, 64)
	if err != nil {
		return fmt.Errorf("not an integer: %s", b)
	}
	*n = zfsNum(v)
	return nil
}

type zpoolJSONDoc struct {
	Pools map[string]zpoolJSONPool `json:"pools"`
}

type zpoolJSONPool struct {
	Name       string                   `json:"name"`
	State      string                   `json:"state"`
	Status     string                   `json:"status"`
	Action     string                   `json:"action"`
	ScanStats  *zpoolJSONScan           `json:"scan_stats"`
	Vdevs      map[string]zpoolJSONVdev `json:"vdevs"`
	Dedup      map[string]zpoolJSONVdev `json:"dedup"`
	Special    map[string]zpoolJSONVdev `json:"special"`
	Logs       map[string]zpoolJSONVdev `json:"logs"`
	L2cache    map[string]zpoolJSONVdev `json:"l2cache"`
	ErrorCount *zfsNum                  `json:"error_count"`
}

type zpoolJSONVdev struct {
	ReadErrors     zfsNum                   `json:"read_errors"`
	WriteErrors    zfsNum                   `json:"write_errors"`
	ChecksumErrors zfsNum                   `json:"checksum_errors"`
	Vdevs          map[string]zpoolJSONVdev `json:"vdevs"`
}

// addErrors adds the counters of every vdev in the tree to p.
func addErrors(p *PoolInfo, tree map[string]zpoolJSONVdev) {
	for _, v := range tree {
		p.ReadErrors += uint64(v.ReadErrors)
		p.WriteErrors += uint64(v.WriteErrors)
		p.ChecksumErrors += uint64(v.ChecksumErrors)
		addErrors(p, v.Vdevs)
	}
}

type zpoolJSONScan struct {
	Function     string                          `json:"function"`
	State        string                          `json:"state"`
	EndTime      zfsNum                          `json:"end_time"`
	Processed    zfsNum                          `json:"processed"`
	Errors       zfsNum                          `json:"errors"`
	RebuildStats map[string]zpoolJSONRebuildVdev `json:"rebuild_stats"`
}

type zpoolJSONRebuildVdev struct {
	State string `json:"state"` // ACTIVE, CANCELED, COMPLETE
}

// parseZpoolStatusJSON reads `zpool status -j --json-int`.
func parseZpoolStatusJSON(out []byte) ([]PoolInfo, error) {
	var doc zpoolJSONDoc
	if err := json.Unmarshal(out, &doc); err != nil {
		return nil, fmt.Errorf("%w: %v", errZpoolParse, err)
	}
	if doc.Pools == nil {
		return nil, fmt.Errorf("%w: no \"pools\" object", errZpoolParse)
	}

	pools := make([]PoolInfo, 0, len(doc.Pools))
	for key, jp := range doc.Pools {
		name := jp.Name
		if name == "" {
			name = key
		}
		p := PoolInfo{
			Name:   name,
			State:  jp.State,
			Status: collapseText(jp.Status),
			Action: collapseText(jp.Action),
		}
		if p.State == "" {
			return nil, fmt.Errorf("%w: pool %q has no state", errZpoolParse, name)
		}

		// "vdevs" holds the root vdev, keyed by the pool name, and the tree
		// under it. The allocation classes and cache devices sit beside it.
		// Spares carry no counters.
		if len(jp.Vdevs) == 0 {
			return nil, fmt.Errorf("%w: pool %q has no vdevs", errZpoolParse, name)
		}
		for _, tree := range []map[string]zpoolJSONVdev{jp.Vdevs, jp.Dedup, jp.Special, jp.Logs, jp.L2cache} {
			addErrors(&p, tree)
		}

		if jp.ErrorCount != nil {
			p.DataErrors = u64Ptr(uint64(*jp.ErrorCount))
		}

		if s := jp.ScanStats; s != nil {
			function, state := s.Function, s.State
			if (function == "" || function == "NONE") && len(s.RebuildStats) > 0 {
				// A sequential rebuild (zpool attach -s, dRAID) keeps its own
				// record; the text form reports it as a resilver.
				function, state = "RESILVER", rebuildScanState(s.RebuildStats)
			}
			if function != "" && function != "NONE" {
				applyScan(&p, function, state, time.Unix(int64(s.EndTime), 0), uint64(s.Processed), uint64(s.Errors))
			}
		}

		pools = append(pools, p)
	}
	sortPools(pools)
	return pools, nil
}

// rebuildScanState folds per-vdev rebuild states into one scan state.
func rebuildScanState(stats map[string]zpoolJSONRebuildVdev) string {
	state := "FINISHED"
	for _, v := range stats {
		switch v.State {
		case "ACTIVE":
			return "SCANNING"
		case "CANCELED":
			state = "CANCELED"
		}
	}
	return state
}

// ---------------------------------------------------------------------------
// Text reader: zpool status -p (every OpenZFS release)
// ---------------------------------------------------------------------------
//
// Format from status_callback() and print_scan_scrub_resilver_status() in
// cmd/zpool/zpool_main.c (zfs-2.1.5 through zfs-2.4.4):
//
//	  pool: <name>
//	 state: <health>
//	status: <text, continued on tab-indented lines>
//	action: <text, continued on tab-indented lines>
//	   see: <url>
//	  scan: <one scan line, continued on tab-indented lines>
//	config:
//
//		NAME  STATE  READ WRITE CKSUM
//		<pool> <state> <r> <w> <c>      <- the pool row
//		  <vdev> <state> <r> <w> <c>   <- every vdev below it, indented
//		logs | cache | special | dedup  <- class headings, a name alone
//		  <vdev> <state> <r> <w> <c>
//		spares
//		  <disk> AVAIL                  <- spares carry no counters
//
//	errors: No known data errors | <N> data errors, use '-v' for a list
//
// Section keys are right-aligned to "status:" so they start within the first
// few columns; continuation and table lines are tab-indented. A pool that has
// never been scanned prints no scan line at all (since OpenZFS 2.0).

// sectionKey matches a section line ("  pool: tank"). Continuations start with
// a tab (or, in output pasted from a terminal, eight spaces), so they never
// match the at-most-six leading spaces allowed here.
var sectionKey = regexp.MustCompile(`^ {0,6}([a-z]+):(?: (.*))?$`)

// ansiEscape strips colour codes, in case ZFS_COLOR reached a terminal-less run.
var ansiEscape = regexp.MustCompile("\x1b\\[[0-9;]*m")

// dataErrorsLine matches "2 data errors, use '-v' for a list".
var dataErrorsLine = regexp.MustCompile(`^(\d+) data errors`)

// parseZpoolStatusText reads `zpool status -p`. Empty output (zpool prints
// "no pools available" to stderr) is zero pools, not an error.
func parseZpoolStatusText(out []byte) ([]PoolInfo, error) {
	text := ansiEscape.ReplaceAllString(string(out), "")
	text = strings.ReplaceAll(text, "\r", "")
	lines := strings.Split(text, "\n")

	var pools []PoolInfo
	var cur *PoolInfo
	var section string       // current section key
	var sectionText []string // status/action/scan lines collected so far
	var scanLines []string   // first scan block of the current pool
	var tableHeaderSeen, rootRowSeen bool

	flush := func() {
		if cur == nil {
			return
		}
		switch section {
		case "status":
			cur.Status = collapseText(strings.Join(sectionText, " "))
		case "action":
			cur.Action = collapseText(strings.Join(sectionText, " "))
		case "scan":
			if scanLines == nil {
				scanLines = append([]string(nil), sectionText...)
			}
		}
		section, sectionText = "", nil
	}
	finishPool := func() error {
		flush()
		if cur == nil {
			return nil
		}
		if cur.State == "" {
			return fmt.Errorf("%w: pool %q has no state line", errZpoolParse, cur.Name)
		}
		if len(scanLines) > 0 {
			if err := applyScanText(cur, scanLines); err != nil {
				return err
			}
		}
		pools = append(pools, *cur)
		cur, scanLines = nil, nil
		return nil
	}

	for _, line := range lines {
		if m := sectionKey.FindStringSubmatch(line); m != nil {
			key, value := m[1], strings.TrimSpace(m[2])
			flush()
			switch key {
			case "pool":
				if err := finishPool(); err != nil {
					return nil, err
				}
				cur = &PoolInfo{Name: value}
				tableHeaderSeen, rootRowSeen = false, false
			case "state":
				if cur != nil {
					cur.State = value
				}
			case "status", "action", "scan":
				section, sectionText = key, []string{value}
			case "errors":
				if cur == nil {
					continue
				}
				if value == knownDataErrorsNone {
					cur.DataErrors = u64Ptr(0)
				} else if dm := dataErrorsLine.FindStringSubmatch(value); dm != nil {
					n, _ := strconv.ParseUint(dm[1], 10, 64)
					cur.DataErrors = u64Ptr(n)
				}
			case "config":
				section = "config"
			}
			continue
		}

		if cur == nil {
			continue
		}
		trimmed := strings.TrimSpace(line)
		switch section {
		case "status", "action", "scan":
			if trimmed != "" {
				sectionText = append(sectionText, trimmed)
			}
		case "config":
			if trimmed == "" {
				continue
			}
			fields := strings.Fields(trimmed)
			if !tableHeaderSeen {
				if len(fields) >= 5 && fields[0] == "NAME" {
					tableHeaderSeen = true
				}
				continue
			}
			if !rootRowSeen {
				// The first row is the pool itself. Anything else means
				// this is not the table this reader knows.
				rootRowSeen = true
				if len(fields) < 5 || fields[0] != cur.Name {
					return nil, fmt.Errorf("%w: pool %q: unexpected first config row %q", errZpoolParse, cur.Name, trimmed)
				}
			}
			// Class headings ("logs") and spares ("sdf AVAIL") have no
			// counters; a row whose three counter columns do not all read
			// as numbers is not a counted vdev.
			if len(fields) < 5 {
				continue
			}
			r, errR := parseZFSCount(fields[2])
			w, errW := parseZFSCount(fields[3])
			c, errC := parseZFSCount(fields[4])
			if errR != nil || errW != nil || errC != nil {
				if fields[0] == cur.Name {
					return nil, fmt.Errorf("%w: pool %q: counters in %q", errZpoolParse, cur.Name, trimmed)
				}
				continue
			}
			cur.ReadErrors += r
			cur.WriteErrors += w
			cur.ChecksumErrors += c
		}
	}
	if err := finishPool(); err != nil {
		return nil, err
	}
	sortPools(pools)
	if pools == nil {
		pools = []PoolInfo{}
	}
	return pools, nil
}

// Scan line patterns, from print_scan_scrub_resilver_status() and
// print_rebuild_status_impl(). The date is ctime() output, read as UTC
// because runZpool sets TZ=UTC.
var (
	scanScrubDone    = regexp.MustCompile(`^scrub repaired (\S+) in .* with (\d+) errors on (.+)$`)
	scanResilverDone = regexp.MustCompile(`^resilvered (?:\(\S+\) )?(\S+) in .* with (\d+) errors on (.+)$`)
)

// applyScanText interprets the first line of the scan block.
func applyScanText(p *PoolInfo, scan []string) error {
	first := strings.TrimSpace(scan[0])
	switch {
	case first == "none requested":
		// Printed by releases before OpenZFS 2.0 for a pool never scanned.
		// Left null, like the JSON form, which has no scan record then.
		return nil
	case strings.HasPrefix(first, "scrub repaired "):
		m := scanScrubDone.FindStringSubmatch(first)
		if m == nil {
			return fmt.Errorf("%w: pool %q: scan line %q", errZpoolParse, p.Name, first)
		}
		repaired, err := parseNiceBytes(m[1])
		if err != nil {
			return fmt.Errorf("%w: pool %q: repaired %q: %v", errZpoolParse, p.Name, m[1], err)
		}
		errs, _ := strconv.ParseUint(m[2], 10, 64)
		end, err := time.ParseInLocation(time.ANSIC, strings.TrimSpace(m[3]), time.UTC)
		if err != nil {
			return fmt.Errorf("%w: pool %q: scrub date %q: %v", errZpoolParse, p.Name, m[3], err)
		}
		applyScan(p, "SCRUB", "FINISHED", end, repaired, errs)
	case strings.HasPrefix(first, "scrub canceled on "):
		applyScan(p, "SCRUB", "CANCELED", time.Time{}, 0, 0)
	case strings.HasPrefix(first, "scrub in progress since "),
		strings.HasPrefix(first, "scrub paused since "):
		applyScan(p, "SCRUB", "SCANNING", time.Time{}, 0, 0)
	case strings.HasPrefix(first, "resilvered "):
		if scanResilverDone.FindStringSubmatch(first) == nil {
			return fmt.Errorf("%w: pool %q: scan line %q", errZpoolParse, p.Name, first)
		}
		applyScan(p, "RESILVER", "FINISHED", time.Time{}, 0, 0)
	case strings.HasPrefix(first, "resilver ") && strings.Contains(first, " canceled on "):
		applyScan(p, "RESILVER", "CANCELED", time.Time{}, 0, 0)
	case strings.HasPrefix(first, "resilver ") && strings.Contains(first, " in progress since "):
		applyScan(p, "RESILVER", "SCANNING", time.Time{}, 0, 0)
	case strings.HasPrefix(first, "error scrub "):
		state := "SCANNING"
		if strings.Contains(first, " canceled on ") {
			state = "CANCELED"
		}
		applyScan(p, "ERRORSCRUB", state, time.Time{}, 0, 0)
	default:
		// An unfamiliar scan line is not worth losing the pool's health over.
		// The scan fields stay null.
	}
	return nil
}

// parseZFSCount reads an error counter: exact with -p, or a nicenum such as
// "1.2K" from output captured without -p.
func parseZFSCount(s string) (uint64, error) {
	if v, err := strconv.ParseUint(s, 10, 64); err == nil {
		return v, nil
	}
	return parseNiceNum(s, "")
}

// parseNiceBytes reads zfs_nicebytes output ("0B", "512B", "1.50M").
func parseNiceBytes(s string) (uint64, error) {
	return parseNiceNum(s, "B")
}

// parseNiceNum reverses zfs_nicenum_format (lib/libzutil/zutil_nicenum.c):
// a number, optionally with up to two decimals, then a unit from
// K M G T P E (base 1024) or the bare unit. The result is as precise as the
// printed figure, no more.
func parseNiceNum(s, bareUnit string) (uint64, error) {
	units := map[string]float64{
		"K": 1 << 10, "M": 1 << 20, "G": 1 << 30,
		"T": 1 << 40, "P": 1 << 50, "E": 1 << 60,
	}
	num, mult := s, 1.0
	if bareUnit != "" && strings.HasSuffix(num, bareUnit) {
		num = strings.TrimSuffix(num, bareUnit)
	} else if n := len(num); n > 0 {
		if m, ok := units[num[n-1:]]; ok {
			num, mult = num[:n-1], m
		}
	}
	v, err := strconv.ParseFloat(num, 64)
	if err != nil || v < 0 {
		return 0, fmt.Errorf("not a size: %q", s)
	}
	return uint64(v*mult + 0.5), nil
}
