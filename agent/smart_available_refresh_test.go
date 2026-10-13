//go:build !windows

package main

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
)

// smartLostBody turns an -a fixture into what a USB drive on macOS returns
// once the SAT driver stops passing SMART through (GH #61): the same
// identity, exit 0, smart_support.enabled false and no SMART data.
func smartLostBody(t *testing.T, full []byte) []byte {
	t.Helper()
	var m map[string]interface{}
	if err := json.Unmarshal(full, &m); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"smart_status", "ata_smart_attributes", "ata_smart_data",
		"ata_smart_error_log", "ata_smart_self_test_log", "temperature", "power_on_time",
		"power_cycle_count"} {
		delete(m, k)
	}
	m["smart_support"] = map[string]interface{}{"available": true, "enabled": false}
	b, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// Available, then unavailable, then available again: the drive stays
// published and readable throughout, the fields follow, and each change
// logs exactly once.
func TestSmartAvailableLostAndBack(t *testing.T) {
	logs := captureLog(t)
	r := newSmartctlRig(t)
	full := fixture(t, "ata_devstat_hgst", "a")
	rigATA(t, r, "ata_devstat_hgst")
	dc := newRigCache(r, "never", false)

	dc.Refresh()
	d := onlyDrive(t, dc)
	id := d.ID
	if d.SmartAvailable == nil || !*d.SmartAvailable || d.SmartUnavailableReason != "" || d.SmartLost {
		t.Fatalf("poll 1: available=%v reason=%q lost=%v", d.SmartAvailable, d.SmartUnavailableReason, d.SmartLost)
	}

	r.set("sda", "a", smartLostBody(t, full), 0)
	for poll := 2; poll <= 4; poll++ {
		dc.Refresh()
		d = onlyDrive(t, dc)
		if d.ID != id || !d.Readable {
			t.Fatalf("poll %d: id %q readable %v; the drive must stay published as before", poll, d.ID, d.Readable)
		}
		if d.SmartAvailable == nil || *d.SmartAvailable || d.SmartUnavailableReason != smartReasonDisabled || !d.SmartLost {
			t.Fatalf("poll %d: available=%v reason=%q lost=%v", poll, d.SmartAvailable, d.SmartUnavailableReason, d.SmartLost)
		}
	}
	lines := strings.Split(logs.String(), "\n")
	if n := countContaining(lines, "stopped giving SMART data"); n != 1 {
		t.Errorf("lost logged %d times, want 1:\n%s", n, logs)
	}
	if n := countContaining(lines, "smartctl says SMART is disabled"); n != 1 {
		t.Errorf("reason logged %d times, want 1:\n%s", n, logs)
	}

	// /api/drives says so too, for someone checking with curl.
	rec := httptest.NewRecorder()
	dc.HandleDrives(rec, httptest.NewRequest("GET", "/api/drives", nil))
	var summary []map[string]interface{}
	if err := json.Unmarshal(rec.Body.Bytes(), &summary); err != nil || len(summary) != 1 {
		t.Fatalf("summary %s: %v", rec.Body, err)
	}
	if summary[0]["smart_available"] != false || summary[0]["readable"] != true {
		t.Errorf("summary = %v", summary[0])
	}

	r.set("sda", "a", full, 0)
	dc.Refresh()
	dc.Refresh()
	d = onlyDrive(t, dc)
	if d.SmartAvailable == nil || !*d.SmartAvailable || d.SmartUnavailableReason != "" || d.SmartLost {
		t.Fatalf("back: available=%v reason=%q lost=%v", d.SmartAvailable, d.SmartUnavailableReason, d.SmartLost)
	}
	lines = strings.Split(logs.String(), "\n")
	if n := countContaining(lines, "is giving SMART data again"); n != 1 {
		t.Errorf("back logged %d times, want 1:\n%s", n, logs)
	}
	if n := countContaining(lines, "stopped giving SMART data"); n != 1 {
		t.Errorf("lost logged %d times in all, want 1:\n%s", n, logs)
	}
}

// A drive that never gave SMART data (a USB enclosure that does not pass it
// through) is published as before, says why, is not "lost", and logs nothing
// new.
func TestSmartAvailableNeverHad(t *testing.T) {
	logs := captureLog(t)
	r := newSmartctlRig(t)
	r.scan(sdaATA)
	r.set("sda", "a", smartLostBody(t, fixture(t, "ata_devstat_hgst", "a")), 0)
	dc := newRigCache(r, "never", false)
	for poll := 1; poll <= 3; poll++ {
		dc.Refresh()
		d := onlyDrive(t, dc)
		if !d.Readable || d.SmartAvailable == nil || *d.SmartAvailable || d.SmartUnavailableReason != smartReasonDisabled || d.SmartLost {
			t.Fatalf("poll %d: readable=%v available=%v reason=%q lost=%v", poll,
				d.Readable, d.SmartAvailable, d.SmartUnavailableReason, d.SmartLost)
		}
	}
	if strings.Contains(logs.String(), "SMART data") {
		t.Errorf("logged for a drive that never had SMART data:\n%s", logs)
	}
}

// A standby or unreadable poll serves the cached entry, SMART fields and all,
// and is not a change in SMART availability.
func TestSmartAvailableCachedEntryKeepsFields(t *testing.T) {
	logs := captureLog(t)
	r := newSmartctlRig(t)
	rigATA(t, r, "ata_devstat_hgst")
	dc := newRigCache(r, "never", false)
	dc.Refresh()
	r.set("sda", "a", []byte(fakeErrBody), 2)
	dc.Refresh()
	d := onlyDrive(t, dc)
	if d.Readable || d.SmartAvailable == nil || !*d.SmartAvailable || d.SmartLost {
		t.Fatalf("cached: readable=%v available=%v lost=%v", d.Readable, d.SmartAvailable, d.SmartLost)
	}
	if strings.Contains(logs.String(), "SMART data") {
		t.Errorf("an unreadable poll logged a SMART availability change:\n%s", logs)
	}
}
