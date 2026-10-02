package main

// Device Statistics (ATA log 0x04) and the Data Written / Data Read values
// derived from it. Design: devstat plan v3, 2026-10-01, Parts 4 and 7.
//
// The statistics come from a second smartctl call per ATA drive per poll,
// "smartctl --json -i -l devstat [-n MODE] [-d TYPE] DEV", made only after the
// v0.7.0 "-a" call read the drive. Nothing here may change what the -a call
// produced: smart_data, readability, protocol, the SAT retry, standby, the
// drive ID and the -a log lines are all decided before readDevstat runs, and
// readDevstat only writes DriveInfo.DeviceStatistics and DriveInfo.Derived.

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math"
	"math/bits"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Tunables. Variables rather than constants so tests can shorten them;
// nothing else should assign to them.
var (
	// devstatTimeout bounds one devstat call. Shorter than the -a call's: the
	// call reads a handful of single-sector logs, and a hung READ LOG EXT is
	// the case where waiting longer only keeps a USB bridge wedged.
	devstatTimeout = 15 * time.Second
	// devstatMaxStrikes consecutive failed devstat calls stop asking a drive
	// for the rest of the run (owner ruling O2). A timeout counts as all of
	// them at once.
	devstatMaxStrikes = 3
	// sataBytesPerSecondBound and minPOHForBound bound a vendor attribute's
	// byte count by what the drive could have moved since power-on.
	sataBytesPerSecondBound = 1.2e9
	minPOHForBound          = 24
)

// devstatGOOS is the operating system the skip table checks. macOS refuses
// READ LOG EXT, so devstat is never asked there. A variable so tests can
// exercise the macOS row on any host.
var devstatGOOS = runtime.GOOS

// Published device_statistics status values.
const (
	devstatPresent       = "present"
	devstatAbsent        = "absent"
	devstatUnavailable   = "unavailable"
	devstatNotApplicable = "not_applicable"
	devstatOff           = "off"
)

// DeviceStatistics is the device_statistics field of /api/drives/{id}.
// Status is one of the devstat* constants. Reason is set for unavailable
// (failed, timeout, standby, mismatch, stopped) and off (config, os). The
// remaining fields are set only for present; Pages is smartctl's raw
// ata_device_statistics.pages array.
type DeviceStatistics struct {
	Status           string          `json:"status"`
	Reason           string          `json:"reason,omitempty"`
	Complete         *bool           `json:"complete,omitempty"`
	ExitStatus       *int            `json:"exit_status,omitempty"`
	LogicalBlockSize int             `json:"logical_block_size,omitempty"`
	Pages            json.RawMessage `json:"pages,omitempty"`
}

// Volume is one derived byte count and where it came from.
type Volume struct {
	Bytes         uint64 `json:"bytes"`
	Source        string `json:"source"` // ata_device_statistics, ata_attribute, nvme
	AttributeID   int    `json:"attribute_id,omitempty"`
	AttributeName string `json:"attribute_name,omitempty"`
}

// Derived is the derived field of /api/drives/{id}: host writes and reads in
// bytes, or the reason each was left out.
type Derived struct {
	HostWrites        *Volume `json:"host_writes,omitempty"`
	HostReads         *Volume `json:"host_reads,omitempty"`
	HostWritesOmitted string  `json:"host_writes_omitted,omitempty"`
	HostReadsOmitted  string  `json:"host_reads_omitted,omitempty"`
}

// Omit reasons for derived.host_writes_omitted / host_reads_omitted.
const (
	omitNoSource           = "no_source"
	omitDevstatUnavailable = "device_statistics_unavailable"
	omitEntryInvalid       = "entry_invalid"
	omitBlockSize          = "block_size"
	omitNotInDatabase      = "not_in_database"
	omitExcludedModel      = "excluded_model"
	omitPOHUnknown         = "power_on_hours_unknown"
	omitRateBound          = "rate_bound"
	omitCandidatesDisagree = "candidates_disagree"
	omitOverflow           = "overflow"
)

// devstatArgs builds the devstat call. -n follows standby_mode exactly as the
// main call's does, except that it is passed on the first poll too (the main
// call omits it there to take a baseline; this call follows that read, so a
// drive back asleep is skipped by the user's own rule). "never" passes no -n
// at all, as the main call does (owner ruling O1). -d is passed exactly when
// the main call passed one, with the same value: "", "sat" or the override.
func devstatArgs(standbyMode, dtype, devicePath string) []string {
	args := []string{"--json", "-i", "-l", "devstat"}
	if standbyMode != "" && standbyMode != "never" {
		args = append(args, "-n", standbyMode)
	}
	if dtype != "" {
		args = append(args, "-d", dtype)
	}
	return append(args, devicePath)
}

// Classification results of one devstat call.
const (
	devstatResPresent  = "present"
	devstatResAbsent   = "absent"
	devstatResFailed   = "failed"
	devstatResTimeout  = "timeout"
	devstatResStandby  = "standby"
	devstatResMismatch = "mismatch"
)

// devstatResult is what classifyDevstat made of one devstat call.
type devstatResult struct {
	kind     string // one of the devstatRes* constants
	complete bool   // present only: exit bit 2 clear
	exit     int    // present only: smartctl's exit status
	lbs      int    // present only: logical_block_size from -i (0 if absent)
	pages    json.RawMessage
}

// classifyDevstat reads one devstat call. Pure; first match wins:
//
//	timeout                                   timeout
//	other exec error, or output not JSON      failed
//	bit 1 and smartctl says low-power mode    standby
//	bit 0 or bit 1                            failed
//	serial differs from the main call's       mismatch (discarded)
//	ata_device_statistics.pages non-empty     present, complete = bit 2 clear
//	no pages, bit 2 clear                     absent (the drive keeps no log)
//	no pages, bit 2 set                       failed
//
// smartctl sets no exit bit and prints no key for a drive without the log
// (ataprint.cpp pout only), so "no pages, no bit 2" is the only proof of
// absence. Exit bits 3-7 cannot occur without -a. Bit 0 is not named in the
// plan's table; it is a command-line error, read as failed, never as absent.
func classifyDevstat(out []byte, code int, execErr error, mainSerial string) devstatResult {
	if execErr != nil {
		var te *smartctlTimeoutError
		if errors.As(execErr, &te) {
			return devstatResult{kind: devstatResTimeout}
		}
		return devstatResult{kind: devstatResFailed}
	}
	var doc struct {
		SerialNumber     *string         `json:"serial_number"`
		LogicalBlockSize json.RawMessage `json:"logical_block_size"`
		Stats            *struct {
			Pages []json.RawMessage `json:"pages"`
		} `json:"ata_device_statistics"`
	}
	if err := json.Unmarshal(out, &doc); err != nil {
		return devstatResult{kind: devstatResFailed}
	}
	if code&0x02 != 0 && smartctlReportsLowPower(out) {
		return devstatResult{kind: devstatResStandby}
	}
	if code&0x03 != 0 {
		return devstatResult{kind: devstatResFailed}
	}
	serial := ""
	if doc.SerialNumber != nil {
		serial = *doc.SerialNumber
	}
	if serial != mainSerial {
		return devstatResult{kind: devstatResMismatch}
	}
	if doc.Stats != nil && len(doc.Stats.Pages) > 0 {
		pages, err := json.Marshal(doc.Stats.Pages)
		if err != nil {
			return devstatResult{kind: devstatResFailed}
		}
		lbs := 0
		if v, st := parseUint(doc.LogicalBlockSize); st == numOK && v <= math.MaxInt32 {
			lbs = int(v)
		}
		return devstatResult{
			kind:     devstatResPresent,
			complete: code&0x04 == 0,
			exit:     code,
			lbs:      lbs,
			pages:    pages,
		}
	}
	if code&0x04 == 0 {
		return devstatResult{kind: devstatResAbsent}
	}
	return devstatResult{kind: devstatResFailed}
}

// devstatMem is the run memory for one drive, keyed path + "|" + serial of
// the main call. Never persisted: a restart asks every drive again.
type devstatMem struct {
	absent     bool // the drive keeps no devstat log; not asked again
	strikes    int  // consecutive failures; devstatMaxStrikes stops asking
	attempts   int  // consecutive failed calls, for the stop log line
	loggedStop bool
}

// smartctlRunner runs smartctl with a timeout; runSmartctlWithTimeout in
// production, replaceable in tests.
type smartctlRunner func(smartctlPath string, args []string, timeout time.Duration) ([]byte, int, error)

// readDevstat applies the skip table, runs the devstat call when it is due,
// and sets info.DeviceStatistics and info.Derived. Called only for a fetchOK
// main result, only from Refresh, after everything about the main call has
// been decided. It reads info and writes only those two fields.
func (dc *DriveCache) readDevstat(info *DriveInfo, dtype string) {
	main := parseMainDoc(info.RawJSON)
	info.DeviceStatistics = dc.devstatStatus(info, main, dtype)
	info.Derived = deriveVolumes(main, info.DeviceStatistics)
}

// devstatStatus is the skip table (plan 7.4) followed by the call itself.
func (dc *DriveCache) devstatStatus(info *DriveInfo, main *mainDoc, dtype string) *DeviceStatistics {
	if dc.cfg != nil && !dc.cfg.DeviceStatisticsEnabled() {
		return &DeviceStatistics{Status: devstatOff, Reason: "config"}
	}
	if dc.goos == "darwin" {
		return &DeviceStatistics{Status: devstatOff, Reason: "os"}
	}
	if !strings.EqualFold(main.protocol, "ATA") {
		return &DeviceStatistics{Status: devstatNotApplicable}
	}
	key := info.DevicePath + "|" + info.Serial
	mem := dc.devstatMemory[key]
	if mem == nil {
		mem = &devstatMem{}
		dc.devstatMemory[key] = mem
	}
	if mem.absent {
		return &DeviceStatistics{Status: devstatAbsent}
	}
	if mem.strikes >= devstatMaxStrikes {
		return &DeviceStatistics{Status: devstatUnavailable, Reason: "stopped"}
	}

	run := dc.devstatRun
	if run == nil {
		run = runSmartctlWithTimeout
	}
	smartctlPath := ""
	if dc.cfg != nil {
		smartctlPath = dc.cfg.SmartctlPath
	}
	out, code, execErr := run(smartctlPath, devstatArgs(dc.standbyMode, dtype, info.DevicePath), devstatTimeout)
	res := classifyDevstat(out, code, execErr, info.Serial)

	switch res.kind {
	case devstatResPresent:
		mem.strikes, mem.attempts = 0, 0
		complete, exit := res.complete, res.exit
		return &DeviceStatistics{
			Status:           devstatPresent,
			Complete:         &complete,
			ExitStatus:       &exit,
			LogicalBlockSize: res.lbs,
			Pages:            res.pages,
		}
	case devstatResAbsent:
		mem.absent = true
		return &DeviceStatistics{Status: devstatAbsent}
	case devstatResTimeout:
		mem.strikes = devstatMaxStrikes
		mem.attempts++
	case devstatResFailed:
		mem.strikes++
		mem.attempts++
	}
	// standby and mismatch leave the memory alone.
	if mem.strikes >= devstatMaxStrikes && !mem.loggedStop {
		mem.loggedStop = true
		msg := fmt.Sprintf("INFO: device statistics not readable on %s after %d attempts (%s); not asking again until the agent restarts",
			info.DevicePath, mem.attempts, res.kind)
		if dc.logs.shouldLog("devstat:"+info.DevicePath, msg) {
			log.Print(msg)
		}
	}
	return &DeviceStatistics{Status: devstatUnavailable, Reason: res.kind}
}

// ---------------------------------------------------------------------------
// Data Written / Data Read (plan 7.5)
// ---------------------------------------------------------------------------

// mainDoc is the part of the main -a output the derivation reads.
type mainDoc struct {
	protocol    string
	model       string
	inDatabase  bool
	blockSize   uint64 // 0 when missing
	pohKnown    bool
	poh         uint64
	attributes  []mainAttr
	nvme        map[string]json.RawMessage
	nvmePresent bool
}

type mainAttr struct {
	id   int
	name string
	raw  map[string]json.RawMessage
}

// parseMainDoc reads what it can from the -a output. A part that does not
// parse is treated as missing; nothing here can fail the drive.
func parseMainDoc(out []byte) *mainDoc {
	m := &mainDoc{}
	var top map[string]json.RawMessage
	if json.Unmarshal(out, &top) != nil {
		return m
	}
	var dev struct {
		Protocol string `json:"protocol"`
	}
	if json.Unmarshal(top["device"], &dev) == nil {
		m.protocol = dev.Protocol
	}
	_ = json.Unmarshal(top["model_name"], &m.model)
	_ = json.Unmarshal(top["in_smartctl_database"], &m.inDatabase)
	if v, st := parseUint(top["logical_block_size"]); st == numOK {
		m.blockSize = v
	}
	var pot map[string]json.RawMessage
	if json.Unmarshal(top["power_on_time"], &pot) == nil {
		if v, st := numField(pot, "hours"); st == numOK {
			m.poh, m.pohKnown = v, true
		}
	}
	var attrs struct {
		Table []json.RawMessage `json:"table"`
	}
	if json.Unmarshal(top["ata_smart_attributes"], &attrs) == nil {
		for _, rawAttr := range attrs.Table {
			var a struct {
				ID   int                        `json:"id"`
				Name string                     `json:"name"`
				Raw  map[string]json.RawMessage `json:"raw"`
			}
			if json.Unmarshal(rawAttr, &a) != nil {
				continue
			}
			m.attributes = append(m.attributes, mainAttr{id: a.ID, name: a.Name, raw: a.Raw})
		}
	}
	if json.Unmarshal(top["nvme_smart_health_information_log"], &m.nvme) == nil && m.nvme != nil {
		m.nvmePresent = true
	}
	return m
}

// Number parse outcomes.
const (
	numMissing = iota
	numOK
	numInvalid  // not a non-negative integer
	numOverflow // an integer that does not fit in uint64
)

// parseUint reads a JSON number (or a JSON string holding one, the KEY_s
// form) as a uint64. smartctl prints 64-bit and wider values both as a number
// and, when a double could not hold them exactly, as a decimal string under
// KEY_s (lib/json.cpp); numField prefers the string.
func parseUint(raw json.RawMessage) (uint64, int) {
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 || bytes.Equal(raw, []byte("null")) {
		return 0, numMissing
	}
	s := string(raw)
	if raw[0] == '"' {
		if json.Unmarshal(raw, &s) != nil {
			return 0, numInvalid
		}
		s = strings.TrimSpace(s)
	}
	v, err := strconv.ParseUint(s, 10, 64)
	if err != nil {
		var ne *strconv.NumError
		if errors.As(err, &ne) && ne.Err == strconv.ErrRange {
			return 0, numOverflow
		}
		return 0, numInvalid
	}
	return v, numOK
}

// numField reads obj[key] as a uint64, preferring obj[key+"_s"].
func numField(obj map[string]json.RawMessage, key string) (uint64, int) {
	if raw, ok := obj[key+"_s"]; ok {
		if v, st := parseUint(raw); st != numMissing {
			return v, st
		}
	}
	return parseUint(obj[key])
}

// mulCheck multiplies with an overflow check.
func mulCheck(a, b uint64) (uint64, bool) {
	hi, lo := bits.Mul64(a, b)
	return lo, hi == 0
}

// deriveVolumes computes derived.host_writes and host_reads from the main
// output and this poll's devstat.
func deriveVolumes(main *mainDoc, ds *DeviceStatistics) *Derived {
	d := &Derived{}
	switch {
	case strings.EqualFold(main.protocol, "ATA"):
		d.HostWrites, d.HostWritesOmitted = ataVolume(main, ds, writeSpec)
		d.HostReads, d.HostReadsOmitted = ataVolume(main, ds, readSpec)
	case strings.EqualFold(main.protocol, "NVMe"):
		d.HostWrites, d.HostWritesOmitted = nvmeVolume(main, "data_units_written")
		d.HostReads, d.HostReadsOmitted = nvmeVolume(main, "data_units_read")
	default:
		d.HostWritesOmitted, d.HostReadsOmitted = omitNoSource, omitNoSource
	}
	return d
}

// nvmeDataUnit is the NVMe data unit: 1000 512-byte units.
const nvmeDataUnit = 512000

func nvmeVolume(main *mainDoc, key string) (*Volume, string) {
	if !main.nvmePresent {
		return nil, omitNoSource
	}
	v, st := numField(main.nvme, key)
	switch st {
	case numMissing:
		return nil, omitNoSource
	case numInvalid:
		return nil, omitEntryInvalid
	case numOverflow:
		return nil, omitOverflow
	}
	b, ok := mulCheck(v, nvmeDataUnit)
	if !ok {
		return nil, omitOverflow
	}
	return &Volume{Bytes: b, Source: "nvme"}, ""
}

// volumeSpec names one direction: the devstat entry and the vendor
// attributes that may stand in for it.
type volumeSpec struct {
	devstatOffset int               // General Statistics (page 1) entry
	named         map[string]uint64 // attribute names that state their unit, and the unit in bytes
	sectors       string            // the sector-count attribute
	preferredID   int
}

// The vendor allowlist (plan 7.5). Never NAND names, Total_Writes_GB,
// unit-less Host_Writes, Total_LBAs_Written_Low or Host_Writes_MiB: names not
// listed here are never read.
var (
	writeSpec = volumeSpec{
		devstatOffset: 0x018, // Logical Sectors Written
		named: map[string]uint64{
			"Host_Writes_32MiB":   32 << 20,
			"Host_Writes_GiB":     1 << 30,
			"Lifetime_Writes_GiB": 1 << 30,
			"Total_Writes_GiB":    1 << 30,
		},
		sectors:     "Total_LBAs_Written",
		preferredID: 241,
	}
	readSpec = volumeSpec{
		devstatOffset: 0x028, // Logical Sectors Read
		named: map[string]uint64{
			"Host_Reads_32MiB":   32 << 20,
			"Host_Reads_GiB":     1 << 30,
			"Lifetime_Reads_GiB": 1 << 30,
			"Total_Reads_GiB":    1 << 30,
		},
		sectors:     "Total_LBAs_Read",
		preferredID: 242,
	}
)

// kioxiaExceriaRe matches the KIOXIA EXCERIA SATA models whose Lifetime GiB
// pair drivedb names but which do not count host bytes (drivedb.h:1115, 1169).
var kioxiaExceriaRe = regexp.MustCompile(`^KIOXIA-EXCERIA SATA SSD`)

func ataVolume(main *mainDoc, ds *DeviceStatistics, spec volumeSpec) (*Volume, string) {
	status := ""
	if ds != nil {
		status = ds.Status
	}
	switch status {
	case devstatPresent:
		return devstatVolume(main, ds, spec)
	case devstatUnavailable:
		return nil, omitDevstatUnavailable
	case devstatAbsent, devstatOff:
		return vendorVolume(main, spec)
	}
	return nil, omitNoSource
}

// devstatVolume reads Logical Sectors Written/Read from page 1.
func devstatVolume(main *mainDoc, ds *DeviceStatistics, spec volumeSpec) (*Volume, string) {
	var pages []struct {
		Number int                          `json:"number"`
		Table  []map[string]json.RawMessage `json:"table"`
	}
	if json.Unmarshal(ds.Pages, &pages) != nil {
		return nil, omitEntryInvalid
	}
	var entry map[string]json.RawMessage
	for _, p := range pages {
		if p.Number != 1 {
			continue
		}
		for _, e := range p.Table {
			if off, st := parseUint(e["offset"]); st == numOK && off == uint64(spec.devstatOffset) {
				entry = e
			}
		}
	}
	if entry == nil {
		return nil, omitEntryInvalid
	}
	var flags struct {
		Valid bool `json:"valid"`
	}
	if json.Unmarshal(entry["flags"], &flags) != nil || !flags.Valid {
		return nil, omitEntryInvalid
	}
	v, st := numField(entry, "value")
	switch st {
	case numMissing, numInvalid:
		return nil, omitEntryInvalid
	case numOverflow:
		return nil, omitOverflow
	}
	lbs := uint64(ds.LogicalBlockSize)
	if lbs == 0 {
		lbs = main.blockSize
	}
	if !validBlockSize(lbs) {
		return nil, omitBlockSize
	}
	b, ok := mulCheck(v, lbs)
	if !ok {
		return nil, omitOverflow
	}
	return &Volume{Bytes: b, Source: "ata_device_statistics"}, ""
}

// validBlockSize: a power of two from 512 to 65536.
func validBlockSize(n uint64) bool {
	return n >= 512 && n <= 65536 && n&(n-1) == 0
}

type vendorCandidate struct {
	id    int
	name  string
	unit  uint64
	named bool
	raw   uint64
}

// vendorVolume picks an allowlisted vendor attribute (plan 7.5 steps 1-6).
func vendorVolume(main *mainDoc, spec volumeSpec) (*Volume, string) {
	// (1) candidates by name.
	var cands []vendorCandidate
	for _, a := range main.attributes {
		if unit, ok := spec.named[a.name]; ok {
			cands = append(cands, vendorCandidate{id: a.id, name: a.name, unit: unit, named: true})
		} else if a.name == spec.sectors {
			cands = append(cands, vendorCandidate{id: a.id, name: a.name, unit: 512})
		}
	}
	if len(cands) == 0 {
		return nil, omitNoSource
	}
	// (2) KIOXIA EXCERIA SATA: the Lifetime GiB pair is not host bytes.
	if kioxiaExceriaRe.MatchString(main.model) {
		cands = filterCands(cands, func(c vendorCandidate) bool { return !strings.HasPrefix(c.name, "Lifetime_") })
		if len(cands) == 0 {
			return nil, omitExcludedModel
		}
	}
	// Sector counts only with 512-byte logical sectors and a drivedb entry
	// (the database is what says the attribute counts host sectors).
	sectorReason := ""
	if main.blockSize != 512 {
		sectorReason = omitBlockSize
	} else if !main.inDatabase {
		sectorReason = omitNotInDatabase
	}
	if sectorReason != "" {
		cands = filterCands(cands, func(c vendorCandidate) bool { return c.named })
		if len(cands) == 0 {
			return nil, sectorReason
		}
	}
	// (3) raw an integer, 0 <= v < 2^48.
	attrByID := map[int]mainAttr{}
	for _, a := range main.attributes {
		attrByID[a.id] = a
	}
	var valid []vendorCandidate
	for _, c := range cands {
		v, st := numField(attrByID[c.id].raw, "value")
		if st != numOK || v >= 1<<48 {
			continue
		}
		c.raw = v
		valid = append(valid, c)
	}
	if len(valid) == 0 {
		return nil, omitEntryInvalid
	}
	// (4) the named-unit class over sectors; in it ID 241/242, then lowest ID.
	named := filterCands(valid, func(c vendorCandidate) bool { return c.named })
	class := named
	if len(class) == 0 {
		class = valid
	}
	chosen := class[0]
	for _, c := range class[1:] {
		if c.id < chosen.id {
			chosen = c
		}
	}
	for _, c := range class {
		if c.id == spec.preferredID {
			chosen = c
			break
		}
	}
	chosenBytes, ok := mulCheck(chosen.raw, chosen.unit)
	if !ok {
		return nil, omitOverflow
	}
	// (5) the other candidates of the same class must agree within 1 %.
	for _, c := range class {
		if c.id == chosen.id {
			continue
		}
		b, ok := mulCheck(c.raw, c.unit)
		if !ok || !within1pct(b, chosenBytes) {
			return nil, omitCandidatesDisagree
		}
	}
	// (6) rate bound.
	if !main.pohKnown || main.poh < uint64(minPOHForBound) {
		return nil, omitPOHUnknown
	}
	if !rateBoundOK(chosenBytes, main.poh) {
		return nil, omitRateBound
	}
	return &Volume{Bytes: chosenBytes, Source: "ata_attribute", AttributeID: chosen.id, AttributeName: chosen.name}, ""
}

func filterCands(in []vendorCandidate, keep func(vendorCandidate) bool) []vendorCandidate {
	var out []vendorCandidate
	for _, c := range in {
		if keep(c) {
			out = append(out, c)
		}
	}
	return out
}

// within1pct reports |a-b| <= 1 % of the larger.
func within1pct(a, b uint64) bool {
	hi, lo := a, b
	if lo > hi {
		hi, lo = lo, hi
	}
	return float64(hi-lo) <= 0.01*float64(hi)
}

// rateBoundOK reports whether bytes could have been moved in hours of
// power-on time at sataBytesPerSecondBound.
func rateBoundOK(bytes, hours uint64) bool {
	return float64(bytes) <= float64(hours)*3600*sataBytesPerSecondBound
}
