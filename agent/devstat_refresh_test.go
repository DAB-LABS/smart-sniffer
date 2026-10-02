//go:build !windows

package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"
)

var sdaATA = scanDevice{Name: "/dev/sda", Protocol: "ATA"}

// rigATA sets up one ATA drive on the rig from a fixture pair.
func rigATA(t *testing.T, r *smartctlRig, name string) {
	t.Helper()
	r.scan(sdaATA)
	r.set("sda", "a", fixture(t, name, "a"), 0)
	r.set("sda", "devstat", fixture(t, name, "devstat"), 0)
}

func countContaining(lines []string, sub string) int {
	n := 0
	for _, l := range lines {
		if strings.Contains(l, sub) {
			n++
		}
	}
	return n
}

// Every row of the skip table (plan 7.4) and the run memory (4.2).
func TestDevstatSkipRules(t *testing.T) {
	t.Run("main not fetchOK: cached entry served unchanged, no devstat call", func(t *testing.T) {
		captureLog(t)
		r := newSmartctlRig(t)
		rigATA(t, r, "ata_devstat_hgst")
		dc := newRigCache(r, "never", true)
		dc.Refresh()
		before := onlyDrive(t, dc)
		if before.DeviceStatistics == nil || before.DeviceStatistics.Status != devstatPresent {
			t.Fatalf("poll 1 devstat = %+v", before.DeviceStatistics)
		}
		r.calls()
		r.set("sda", "a", []byte(fakeErrBody), 2)
		dc.Refresh()
		_, _, ds := splitCalls(r.calls())
		if len(ds) != 0 {
			t.Errorf("devstat asked for an unreadable drive: %v", ds)
		}
		after := onlyDrive(t, dc)
		if after.Readable || !reflect.DeepEqual(after.DeviceStatistics, before.DeviceStatistics) || !reflect.DeepEqual(after.Derived, before.Derived) {
			t.Errorf("cached entry changed: %+v", after)
		}
	})

	t.Run("D10 off", func(t *testing.T) {
		captureLog(t)
		r := newSmartctlRig(t)
		rigATA(t, r, "ata_devstat_hgst")
		dc := newRigCache(r, "never", false)
		dc.Refresh()
		if _, _, ds := splitCalls(r.calls()); len(ds) != 0 {
			t.Errorf("devstat asked with D10 off: %v", ds)
		}
		got := onlyDrive(t, dc).DeviceStatistics
		if got == nil || got.Status != devstatOff || got.Reason != "config" {
			t.Errorf("status = %+v, want off/config", got)
		}
	})

	t.Run("macOS", func(t *testing.T) {
		captureLog(t)
		r := newSmartctlRig(t)
		rigATA(t, r, "ata_devstat_hgst")
		dc := newRigCache(r, "never", true)
		dc.goos = "darwin"
		dc.Refresh()
		if _, _, ds := splitCalls(r.calls()); len(ds) != 0 {
			t.Errorf("devstat asked on macOS: %v", ds)
		}
		got := onlyDrive(t, dc).DeviceStatistics
		if got == nil || got.Status != devstatOff || got.Reason != "os" {
			t.Errorf("status = %+v, want off/os", got)
		}
	})

	for _, tc := range []struct{ name, proto string }{{"nvme_sabrent", "NVMe"}, {"scsi_view", "SCSI"}} {
		t.Run("not ATA: "+tc.name, func(t *testing.T) {
			captureLog(t)
			r := newSmartctlRig(t)
			r.scan(scanDevice{Name: "/dev/sda", Protocol: tc.proto})
			r.set("sda", "a", fixture(t, tc.name, "a"), 0)
			r.set("sda", "sat", []byte(fakeErrBody), 2)
			dc := newRigCache(r, "never", true)
			dc.Refresh()
			if _, _, ds := splitCalls(r.calls()); len(ds) != 0 {
				t.Errorf("devstat asked: %v", ds)
			}
			if got := onlyDrive(t, dc).DeviceStatistics; got == nil || got.Status != devstatNotApplicable {
				t.Errorf("status = %+v, want not_applicable", got)
			}
		})
	}

	t.Run("absent is remembered for the run; a new serial at the path is asked; a new cache asks", func(t *testing.T) {
		captureLog(t)
		r := newSmartctlRig(t)
		rigATA(t, r, "ata_no_devstat_samsung850")
		dc := newRigCache(r, "never", true)
		for poll := 1; poll <= 3; poll++ {
			dc.Refresh()
			_, _, ds := splitCalls(r.calls())
			if want := map[bool]int{true: 1, false: 0}[poll == 1]; len(ds) != want {
				t.Errorf("poll %d: %d devstat calls, want %d", poll, len(ds), want)
			}
			if got := onlyDrive(t, dc).DeviceStatistics; got.Status != devstatAbsent {
				t.Errorf("poll %d: status %+v", poll, got)
			}
		}
		swap := func(b []byte) []byte {
			return edit(t, b, func(d map[string]any) { d["serial_number"] = "FIXTURE-swapped" })
		}
		r.set("sda", "a", swap(fixture(t, "ata_no_devstat_samsung850", "a")), 0)
		r.set("sda", "devstat", swap(fixture(t, "ata_no_devstat_samsung850", "devstat")), 0)
		dc.Refresh()
		if _, _, ds := splitCalls(r.calls()); len(ds) != 1 {
			t.Errorf("new drive at the same path: %d devstat calls, want 1", len(ds))
		}
		dc2 := newRigCache(r, "never", true)
		dc2.Refresh()
		if _, _, ds := splitCalls(r.calls()); len(ds) != 1 {
			t.Errorf("new cache: %d devstat calls, want 1", len(ds))
		}
	})

	t.Run("three failures log once and stop", func(t *testing.T) {
		logs := captureLog(t)
		r := newSmartctlRig(t)
		rigATA(t, r, "ata_devstat_hgst")
		r.set("sda", "devstat", withExit(t, fixture(t, "ata_no_devstat_samsung850", "devstat"), 4), 4)
		// The no-devstat fixture's serial differs; give it the HGST serial.
		r.set("sda", "devstat", edit(t, withExit(t, fixture(t, "ata_no_devstat_samsung850", "devstat"), 4),
			func(d map[string]any) { d["serial_number"] = "FIXTURE-m01-sdc" }), 4)
		dc := newRigCache(r, "never", true)
		wantStatus := []string{"failed", "failed", "failed", "stopped", "stopped"}
		for poll := 1; poll <= 5; poll++ {
			dc.Refresh()
			_, _, ds := splitCalls(r.calls())
			if want := map[bool]int{true: 1, false: 0}[poll <= 3]; len(ds) != want {
				t.Errorf("poll %d: %d devstat calls, want %d", poll, len(ds), want)
			}
			got := onlyDrive(t, dc).DeviceStatistics
			if got.Status != devstatUnavailable || got.Reason != wantStatus[poll-1] {
				t.Errorf("poll %d: %+v, want unavailable/%s", poll, got, wantStatus[poll-1])
			}
			d := onlyDrive(t, dc).Derived
			if d.HostWritesOmitted != omitDevstatUnavailable || d.HostReadsOmitted != omitDevstatUnavailable {
				t.Errorf("poll %d: derived %+v", poll, d)
			}
		}
		lines := strings.Split(strings.TrimSpace(logs.String()), "\n")
		want := "INFO: device statistics not readable on /dev/sda after 3 attempts (failed); not asking again until the agent restarts"
		if n := countContaining(lines, "device statistics"); n != 1 || countContaining(lines, want) != 1 {
			t.Errorf("stop line logged %d times, want once as %q:\n%s", n, want, logs.String())
		}
	})

	t.Run("a timeout stops at once", func(t *testing.T) {
		logs := captureLog(t)
		shortenDevstatTimeout(t)
		r := newSmartctlRig(t)
		rigATA(t, r, "ata_devstat_hgst")
		r.hang("sda.devstat")
		dc := newRigCache(r, "never", true)
		dc.Refresh()
		if got := onlyDrive(t, dc).DeviceStatistics; got.Status != devstatUnavailable || got.Reason != "timeout" {
			t.Errorf("poll 1: %+v", got)
		}
		r.calls()
		dc.Refresh()
		if _, _, ds := splitCalls(r.calls()); len(ds) != 0 {
			t.Errorf("asked again after a timeout: %v", ds)
		}
		if got := onlyDrive(t, dc).DeviceStatistics; got.Reason != "stopped" {
			t.Errorf("poll 2: %+v", got)
		}
		if !strings.Contains(logs.String(), "after 1 attempts (timeout)") {
			t.Errorf("log:\n%s", logs.String())
		}
	})

	for _, tc := range []struct {
		name string
		body func(t *testing.T) []byte
		code int
	}{
		{"standby", func(t *testing.T) []byte { return []byte(standbyBody) }, 2},
		{"mismatch", func(t *testing.T) []byte {
			return edit(t, fixture(t, "ata_devstat_hgst", "devstat"), func(d map[string]any) { d["serial_number"] = "FIXTURE-other" })
		}, 0},
	} {
		t.Run(tc.name+" takes no strike", func(t *testing.T) {
			captureLog(t)
			r := newSmartctlRig(t)
			rigATA(t, r, "ata_devstat_hgst")
			r.set("sda", "devstat", tc.body(t), tc.code)
			dc := newRigCache(r, "standby", true)
			for poll := 1; poll <= 5; poll++ {
				dc.Refresh()
				if _, _, ds := splitCalls(r.calls()); len(ds) != 1 {
					t.Fatalf("poll %d: %d devstat calls, want 1", poll, len(ds))
				}
				if got := onlyDrive(t, dc).DeviceStatistics; got.Status != devstatUnavailable || got.Reason != tc.name {
					t.Errorf("poll %d: %+v", poll, got)
				}
			}
		})
	}

	t.Run("a block resets the strikes", func(t *testing.T) {
		captureLog(t)
		r := newSmartctlRig(t)
		rigATA(t, r, "ata_devstat_hgst")
		failed := edit(t, withExit(t, fixture(t, "ata_no_devstat_samsung850", "devstat"), 4),
			func(d map[string]any) { d["serial_number"] = "FIXTURE-m01-sdc" })
		good := fixture(t, "ata_devstat_hgst", "devstat")
		dc := newRigCache(r, "never", true)
		for i, step := range []struct {
			body []byte
			code int
		}{{failed, 4}, {failed, 4}, {good, 0}, {failed, 4}, {failed, 4}, {good, 0}} {
			r.set("sda", "devstat", step.body, step.code)
			dc.Refresh()
			if _, _, ds := splitCalls(r.calls()); len(ds) != 1 {
				t.Fatalf("poll %d: %d devstat calls, want 1", i+1, len(ds))
			}
		}
		if got := onlyDrive(t, dc).DeviceStatistics; got.Status != devstatPresent {
			t.Errorf("final: %+v", got)
		}
	})

	t.Run("partial counts as success", func(t *testing.T) {
		captureLog(t)
		r := newSmartctlRig(t)
		rigATA(t, r, "ata_devstat_wdc_unc")
		r.set("sda", "devstat", withExit(t, fixture(t, "ata_devstat_wdc_unc", "devstat"), 4), 4)
		dc := newRigCache(r, "never", true)
		for poll := 1; poll <= 4; poll++ {
			dc.Refresh()
			if _, _, ds := splitCalls(r.calls()); len(ds) != 1 {
				t.Fatalf("poll %d: not asked", poll)
			}
		}
		got := onlyDrive(t, dc).DeviceStatistics
		if got.Status != devstatPresent || got.Complete == nil || *got.Complete || got.ExitStatus == nil || *got.ExitStatus != 4 {
			t.Errorf("partial: %+v", got)
		}
	})
}

// D9: the learned SAT protocol (plan Part 5).
func TestSATLearned(t *testing.T) {
	sdaSCSI := scanDevice{Name: "/dev/sda", Protocol: "SCSI"}
	hgstA := fixture(t, "ata_devstat_hgst", "a")
	setup := func(t *testing.T, standby string) (*smartctlRig, *DriveCache) {
		r := newSmartctlRig(t)
		r.scan(sdaSCSI)
		r.set("sda", "a", []byte(fakeErrBody), 4)
		r.set("sda", "sat", hgstA, 0)
		r.set("sda", "devstat", fixture(t, "ata_devstat_hgst", "devstat"), 0)
		return r, newRigCache(r, standby, true)
	}

	t.Run("survives the scan, logs once, devstat gets -d sat", func(t *testing.T) {
		logs := captureLog(t)
		r, dc := setup(t, "never")
		dc.Refresh()
		_, mains, ds := splitCalls(r.calls())
		want := []string{"--json -a /dev/sda", "--json -a -d sat /dev/sda"}
		if !reflect.DeepEqual(mains, want) {
			t.Errorf("poll 1 mains = %q, want %q", mains, want)
		}
		if !reflect.DeepEqual(ds, []string{"--json -i -l devstat -d sat /dev/sda"}) {
			t.Errorf("poll 1 devstat = %q", ds)
		}
		for poll := 2; poll <= 4; poll++ {
			dc.Refresh()
			_, mains, ds := splitCalls(r.calls())
			if !reflect.DeepEqual(mains, []string{"--json -a -d sat /dev/sda"}) {
				t.Errorf("poll %d mains = %q, want SAT only", poll, mains)
			}
			if !reflect.DeepEqual(ds, []string{"--json -i -l devstat -d sat /dev/sda"}) {
				t.Errorf("poll %d devstat = %q", poll, ds)
			}
			if dc.protocolCache["/dev/sda"] != "sat" || !dc.satLearned["/dev/sda"] {
				t.Errorf("poll %d: cache %q learned %v", poll, dc.protocolCache["/dev/sda"], dc.satLearned["/dev/sda"])
			}
		}
		if n := strings.Count(logs.String(), "SAT succeeded"); n != 1 {
			t.Errorf("SAT succeeded logged %d times, want 1", n)
		}
		d := onlyDrive(t, dc)
		if !d.Readable || d.Protocol != "ATA" || d.DeviceStatistics.Status != devstatPresent {
			t.Errorf("drive %+v", d)
		}
	})

	t.Run("standby keeps the entry with no SCSI call", func(t *testing.T) {
		captureLog(t)
		r, dc := setup(t, "standby")
		dc.Refresh()
		r.calls()
		r.set("sda", "sat", []byte(standbyBody), 2)
		dc.Refresh()
		_, mains, ds := splitCalls(r.calls())
		if !reflect.DeepEqual(mains, []string{"--json -a -d sat -n standby /dev/sda"}) || len(ds) != 0 {
			t.Errorf("mains %q devstat %q", mains, ds)
		}
		if !dc.satLearned["/dev/sda"] || dc.protocolCache["/dev/sda"] != "sat" {
			t.Error("standby forgot SAT")
		}
		if d := onlyDrive(t, dc); !d.InStandby || !d.Readable {
			t.Errorf("drive %+v", d)
		}
		r.set("sda", "sat", hgstA, 0)
		dc.Refresh()
		if _, mains, _ := splitCalls(r.calls()); !reflect.DeepEqual(mains, []string{"--json -a -d sat -n standby /dev/sda"}) {
			t.Errorf("after standby: %q", mains)
		}
	})

	t.Run("failure forgets and re-reads, no second SAT", func(t *testing.T) {
		logs := captureLog(t)
		r, dc := setup(t, "never")
		dc.Refresh()
		r.calls()
		r.set("sda", "sat", []byte(fakeErrBody), 2)
		r.set("sda", "a", hgstA, 4)
		dc.Refresh()
		_, mains, ds := splitCalls(r.calls())
		if !reflect.DeepEqual(mains, []string{"--json -a -d sat /dev/sda", "--json -a /dev/sda"}) {
			t.Errorf("mains = %q, want SAT then the scan protocol", mains)
		}
		if !reflect.DeepEqual(ds, []string{"--json -i -l devstat /dev/sda"}) {
			t.Errorf("devstat = %q, want no -d", ds)
		}
		if dc.satLearned["/dev/sda"] || dc.protocolCache["/dev/sda"] != "SCSI" {
			t.Errorf("learned %v cache %q", dc.satLearned["/dev/sda"], dc.protocolCache["/dev/sda"])
		}
		if d := onlyDrive(t, dc); !d.Readable {
			t.Errorf("drive %+v", d)
		}
		// Next poll: the v0.7.0 sequence again (SCSI, then the SAT retry).
		dc.Refresh()
		if _, mains, _ := splitCalls(r.calls()); !reflect.DeepEqual(mains, []string{"--json -a /dev/sda", "--json -a -d sat /dev/sda"}) {
			t.Errorf("poll 3 mains = %q", mains)
		}
		if n := strings.Count(logs.String(), "SAT succeeded"); n != 1 {
			t.Errorf("SAT succeeded logged %d times", n)
		}
	})

	t.Run("override wins", func(t *testing.T) {
		captureLog(t)
		r, _ := setup(t, "never")
		cfg := &Config{ScanInterval: time.Minute, SmartctlPath: r.path, StandbyMode: "never",
			DeviceOverrides: []DeviceOverride{{Device: "/dev/sda", Protocol: "scsi"}}}
		dc := NewDriveCache(cfg)
		dc.goos = "linux"
		for poll := 1; poll <= 3; poll++ {
			dc.Refresh()
			_, mains, _ := splitCalls(r.calls())
			if !reflect.DeepEqual(mains, []string{"--json -a -d scsi /dev/sda", "--json -a -d sat /dev/sda"}) {
				t.Errorf("poll %d mains = %q", poll, mains)
			}
			if dc.satLearned["/dev/sda"] {
				t.Errorf("poll %d: an override path was learned", poll)
			}
		}
	})
}

// mainSnapshot is everything the devstat call must never change.
type mainSnapshot struct {
	Mains    []string
	Drives   []string
	Cache    map[string]string
	Learned  map[string]bool
	LogLines []string
}

func snapshot(t *testing.T, dc *DriveCache, mains []string, logs string) mainSnapshot {
	t.Helper()
	s := mainSnapshot{Mains: mains, Cache: map[string]string{}, Learned: map[string]bool{}}
	dc.mu.RLock()
	for id, d := range dc.drives {
		s.Drives = append(s.Drives, fmt.Sprintf("%s path=%s model=%q serial=%q proto=%s standby=%v readable=%v raw=%s",
			id, d.DevicePath, d.Model, d.Serial, d.Protocol, d.InStandby, d.Readable, string(d.RawJSON)))
	}
	for k, v := range dc.protocolCache {
		s.Cache[k] = v
	}
	for k, v := range dc.satLearned {
		s.Learned[k] = v
	}
	dc.mu.RUnlock()
	sort.Strings(s.Drives)
	for _, l := range strings.Split(logs, "\n") {
		if l != "" && !strings.Contains(l, "device statistics") {
			s.LogLines = append(s.LogLines, l)
		}
	}
	return s
}

// The devstat call never touches the main call: for every devstat outcome
// and every main outcome, the main call's arguments, the published entry
// (minus the two new fields), the protocol cache, the learned SAT set and
// the main log lines equal the run with devstat off (plan 7.6).
func TestDevstatNeverTouchesMainCall(t *testing.T) {
	shortenDevstatTimeout(t)
	hgstA := fixture(t, "ata_devstat_hgst", "a")
	hgstDS := fixture(t, "ata_devstat_hgst", "devstat")
	noBlock := edit(t, fixture(t, "ata_no_devstat_samsung850", "devstat"), func(d map[string]any) { d["serial_number"] = "FIXTURE-m01-sdc" })

	type mainOutcome struct {
		name  string
		proto string
		polls [2]func(r *smartctlRig)
	}
	ok := func(r *smartctlRig) { r.set("sda", "a", hgstA, 0) }
	mains := []mainOutcome{
		{"ok", "ATA", [2]func(*smartctlRig){ok, ok}},
		{"exit 4", "ATA", [2]func(*smartctlRig){
			func(r *smartctlRig) { r.set("sda", "a", withExit(t, hgstA, 4), 4) },
			func(r *smartctlRig) { r.set("sda", "a", withExit(t, hgstA, 4), 4) }}},
		{"standby", "ATA", [2]func(*smartctlRig){ok, func(r *smartctlRig) { r.set("sda", "a", []byte(standbyBody), 2) }}},
		{"unreadable", "ATA", [2]func(*smartctlRig){ok, func(r *smartctlRig) { r.set("sda", "a", []byte(fakeErrBody), 2) }}},
		{"SCSI then SAT", "SCSI", [2]func(*smartctlRig){
			func(r *smartctlRig) { r.set("sda", "a", []byte(fakeErrBody), 4); r.set("sda", "sat", hgstA, 0) },
			func(r *smartctlRig) { r.set("sda", "a", []byte(fakeErrBody), 4); r.set("sda", "sat", hgstA, 0) }}},
		{"SAT fail", "SCSI", [2]func(*smartctlRig){
			func(r *smartctlRig) {
				r.set("sda", "a", withExit(t, hgstA, 4), 4)
				r.set("sda", "sat", []byte(fakeErrBody), 2)
			},
			func(r *smartctlRig) {
				r.set("sda", "a", withExit(t, hgstA, 4), 4)
				r.set("sda", "sat", []byte(fakeErrBody), 2)
			}}},
		{"learned SAT then fails", "SCSI", [2]func(*smartctlRig){
			func(r *smartctlRig) { r.set("sda", "a", []byte(fakeErrBody), 4); r.set("sda", "sat", hgstA, 0) },
			func(r *smartctlRig) {
				r.set("sda", "a", withExit(t, hgstA, 4), 4)
				r.set("sda", "sat", []byte(fakeErrBody), 2)
			}}},
	}

	type devstatOutcome struct {
		name string
		set  func(r *smartctlRig, dc *DriveCache)
	}
	var outcomes []devstatOutcome
	for code := 0; code <= 7; code++ {
		code := code
		outcomes = append(outcomes,
			devstatOutcome{fmt.Sprintf("exit %d with block", code), func(r *smartctlRig, _ *DriveCache) {
				r.set("sda", "devstat", withExit(t, hgstDS, code), code)
			}},
			devstatOutcome{fmt.Sprintf("exit %d without block", code), func(r *smartctlRig, _ *DriveCache) {
				r.set("sda", "devstat", withExit(t, noBlock, code), code)
			}})
	}
	outcomes = append(outcomes,
		devstatOutcome{"timeout", func(r *smartctlRig, _ *DriveCache) { r.hang("sda.devstat") }},
		devstatOutcome{"exec error", func(_ *smartctlRig, dc *DriveCache) {
			dc.devstatRun = func(string, []string, time.Duration) ([]byte, int, error) {
				return nil, -1, errors.New("fork/exec: resource temporarily unavailable")
			}
		}},
		devstatOutcome{"garbage", func(r *smartctlRig, _ *DriveCache) { r.set("sda", "devstat", []byte("{not json"), 0) }},
	)

	run := func(t *testing.T, m mainOutcome, o devstatOutcome, devstatOn bool) []mainSnapshot {
		logs := captureLog(t)
		r := newSmartctlRig(t)
		r.scan(scanDevice{Name: "/dev/sda", Protocol: m.proto})
		dc := newRigCache(r, "standby", devstatOn)
		o.set(r, dc)
		var snaps []mainSnapshot
		for poll := 0; poll < 2; poll++ {
			m.polls[poll](r)
			logs.Reset()
			dc.Refresh()
			_, mainCalls, ds := splitCalls(r.calls())
			if !devstatOn && len(ds) != 0 {
				t.Fatalf("devstat called with D10 off: %v", ds)
			}
			snaps = append(snaps, snapshot(t, dc, mainCalls, logs.String()))
		}
		return snaps
	}

	for _, m := range mains {
		for _, o := range outcomes {
			t.Run(m.name+"/"+o.name, func(t *testing.T) {
				off := run(t, m, o, false)
				on := run(t, m, o, true)
				if !reflect.DeepEqual(on, off) {
					a, _ := json.MarshalIndent(off, "", " ")
					b, _ := json.MarshalIndent(on, "", " ")
					t.Errorf("devstat on differs from off\noff: %s\n on: %s", a, b)
				}
			})
		}
	}
}

// For every fixture, with devstat on and off, the main -a call's arguments
// are v0.7.0's, written out literally here, and smart_data is the fixture's
// bytes exactly (plan Part 1: smart_data is v0.7.0's output).
func TestMainCallByteIdenticalForEveryFixture(t *testing.T) {
	names := []string{"ata_devstat_hgst", "ata_devstat_seagate", "ata_devstat_samsung_ssd", "ata_devstat_wdc_unc",
		"ata_devstat_hgst_nopoh", "ata_no_devstat_samsung850", "ata_skhynix", "ata_skhynix_sc311", "nvme_sabrent", "scsi_view"}
	for _, name := range names {
		for _, on := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/devstat=%v", name, on), func(t *testing.T) {
				captureLog(t)
				a := fixture(t, name, "a")
				var doc struct {
					Smartctl struct {
						ExitStatus int `json:"exit_status"`
					} `json:"smartctl"`
					Device struct {
						Protocol string `json:"protocol"`
					} `json:"device"`
				}
				if err := json.Unmarshal(a, &doc); err != nil {
					t.Fatal(err)
				}
				r := newSmartctlRig(t)
				r.scan(scanDevice{Name: "/dev/sda", Protocol: doc.Device.Protocol})
				r.set("sda", "a", a, doc.Smartctl.ExitStatus)
				r.set("sda", "sat", []byte(fakeErrBody), 2)
				if name != "nvme_sabrent" && name != "scsi_view" {
					r.set("sda", "devstat", fixture(t, name, "devstat"), 0)
				}
				dc := newRigCache(r, "standby", on)
				wantMains := [][]string{{"--json -a /dev/sda"}, {"--json -a -n standby /dev/sda"}}
				if doc.Device.Protocol == "SCSI" {
					// v0.7.0: a SCSI view exiting 4 is retried with SAT every poll.
					wantMains = [][]string{
						{"--json -a /dev/sda", "--json -a -d sat /dev/sda"},
						{"--json -a -n standby /dev/sda", "--json -a -d sat -n standby /dev/sda"},
					}
				}
				for poll := 0; poll < 2; poll++ {
					dc.Refresh()
					_, mains, ds := splitCalls(r.calls())
					if !reflect.DeepEqual(mains, wantMains[poll]) {
						t.Errorf("poll %d mains = %q, want %q", poll+1, mains, wantMains[poll])
					}
					d := onlyDrive(t, dc)
					if string(d.RawJSON) != string(a) {
						t.Errorf("poll %d: smart_data is not the fixture's bytes", poll+1)
					}
					wantDS := 0
					if on && doc.Device.Protocol == "ATA" && (poll == 0 || d.DeviceStatistics.Status != devstatAbsent) {
						wantDS = 1
					}
					if len(ds) != wantDS {
						t.Errorf("poll %d: %d devstat calls, want %d", poll+1, len(ds), wantDS)
					}
					// The published smart_data is the -a output, re-encoded the
					// way v0.7.0 encoded it (encoding/json compacts RawMessage).
					var pub map[string]json.RawMessage
					b, _ := json.Marshal(d)
					if err := json.Unmarshal(b, &pub); err != nil {
						t.Fatal(err)
					}
					var compact bytes.Buffer
					_ = json.Compact(&compact, a)
					if string(pub["smart_data"]) != compact.String() {
						t.Errorf("poll %d: published smart_data differs from the fixture", poll+1)
					}
				}
			})
		}
	}
}

// Scan timeouts (plan 4.4).
func TestScanTimeouts(t *testing.T) {
	shorten := func(t *testing.T) {
		s1, s2 := scanTimeout, scanOpenTimeout
		scanTimeout, scanOpenTimeout = 200*time.Millisecond, 300*time.Millisecond
		t.Cleanup(func() { scanTimeout, scanOpenTimeout = s1, s2 })
	}

	t.Run("runtime --scan: logs once, serves cached data", func(t *testing.T) {
		logs := captureLog(t)
		shorten(t)
		r := newSmartctlRig(t)
		rigATA(t, r, "ata_devstat_hgst")
		dc := newRigCache(r, "never", true)
		dc.Refresh()
		before := onlyDrive(t, dc)
		r.hang("scan")
		logs.Reset()
		for i := 0; i < 3; i++ {
			start := time.Now()
			dc.Refresh()
			if time.Since(start) > 3*time.Second {
				t.Fatal("scan timeout did not fire")
			}
		}
		if n := strings.Count(logs.String(), "drive scan timed out after 0s; serving cached data"); n != 1 {
			t.Errorf("logged %d times:\n%s", n, logs.String())
		}
		if after := onlyDrive(t, dc); after.ID != before.ID || !after.Readable {
			t.Errorf("cache not served: %+v", after)
		}
	})

	t.Run("first-poll --scan-open falls back to --scan", func(t *testing.T) {
		logs := captureLog(t)
		shorten(t)
		r := newSmartctlRig(t)
		rigATA(t, r, "ata_devstat_hgst")
		r.hang("scan-open")
		dc := newRigCache(r, "never", true)
		dc.Refresh()
		if !strings.Contains(logs.String(), "--scan-open timed out after 0s, falling back to --scan") {
			t.Errorf("log:\n%s", logs.String())
		}
		onlyDrive(t, dc)
		scans, _, _ := splitCalls(r.calls())
		if !reflect.DeepEqual(scans, []string{"--json --scan-open", "--json --scan"}) {
			t.Errorf("scans = %q", scans)
		}
	})

	t.Run("both hang on the first poll", func(t *testing.T) {
		logs := captureLog(t)
		shorten(t)
		r := newSmartctlRig(t)
		rigATA(t, r, "ata_devstat_hgst")
		r.hang("scan-open")
		r.hang("scan")
		dc := newRigCache(r, "never", true)
		dc.Refresh()
		if !strings.Contains(logs.String(), "drive scan timed out after 0s; serving cached data") {
			t.Errorf("log:\n%s", logs.String())
		}
		if len(dc.drives) != 0 {
			t.Errorf("drives: %v", dc.drives)
		}
	})

	t.Run("preflight logs and continues", func(t *testing.T) {
		logs := captureLog(t)
		shorten(t)
		r := newSmartctlRig(t)
		r.hang("scan")
		drives, timedOut, err := preflightScanDrives(r.path)
		if err != nil || !timedOut || len(drives) != 0 {
			t.Errorf("preflight = %v %v %v", drives, timedOut, err)
		}
		if !strings.Contains(logs.String(), "WARNING: smartctl --scan timed out after 0s at startup; continuing") {
			t.Errorf("log:\n%s", logs.String())
		}
	})

	t.Run("preflight permission error stays fatal", func(t *testing.T) {
		captureLog(t)
		r := newSmartctlRig(t)
		r.write("scan.json", []byte("smartctl: Permission denied\n"))
		if _, timedOut, err := preflightScanDrives(r.path); err == nil || timedOut {
			t.Errorf("err = %v timedOut = %v", err, timedOut)
		}
	})

	t.Run("discover: a --scan timeout is a clear error", func(t *testing.T) {
		captureLog(t)
		shorten(t)
		r := newSmartctlRig(t)
		r.hang("scan-open")
		r.hang("scan")
		err := RunDiscover(&Config{SmartctlPath: r.path}, true)
		if err == nil || err.Error() != "smartctl --scan timed out after 0s" {
			t.Errorf("err = %v", err)
		}
	})
}

// /api/health pools_status (plan 4.4): pending before the first read, ok
// with the count, failed without it; absent with pool status off.
func TestHealthPoolsStatus(t *testing.T) {
	captureLog(t)
	dc := NewDriveCache(&Config{ScanInterval: time.Minute})
	if h := healthOf(t, dc); h["pools_status"] != nil || h["pools"] != nil {
		t.Errorf("pool status off: %v", h)
	}

	path, _ := stubZpool(t, stubOld)
	dc.poolCache = NewPoolCache(path, false)
	if h := healthOf(t, dc); string(h["pools_status"]) != `"pending"` || h["pools"] != nil {
		t.Errorf("pending: pools_status=%s pools=%s", h["pools_status"], h["pools"])
	}
	waitRefresh(t, dc.poolCache)
	if h := healthOf(t, dc); string(h["pools_status"]) != `"ok"` || string(h["pools"]) != "3" {
		t.Errorf("ok: pools_status=%s pools=%s", h["pools_status"], h["pools"])
	}

	logs := captureLog(t)
	bad, _ := stubZpool(t, stubNoModule)
	dc.poolCache.zpoolPath = bad
	waitRefresh(t, dc.poolCache)
	if h := healthOf(t, dc); string(h["pools_status"]) != `"failed"` || h["pools"] != nil {
		t.Errorf("failed: pools_status=%s pools=%s", h["pools_status"], h["pools"])
	}
	if !strings.Contains(logs.String(), "; pool status unavailable (/api/pools answers 503 until zpool succeeds)") {
		t.Errorf("zpool failure wording:\n%s", logs.String())
	}
	if strings.Contains(logs.String(), "reporting no pools") {
		t.Error("old wording still logged")
	}
}
