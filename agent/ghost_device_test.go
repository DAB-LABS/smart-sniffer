package main

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

// smartctl sets exit bit 1 for both "device is in STANDBY mode" and "device
// open failed". Treating that bit as standby is what let a permission-blocked
// drive be published under a fabricated path-based identity in
// smart-sniffer-app#7, so the discriminator below is load-bearing.
func TestSmartctlReportsLowPower(t *testing.T) {
	tests := []struct {
		name string
		out  string
		want bool
	}{
		{
			name: "standby message",
			out: `{"smartctl":{"exit_status":2,"messages":[
				{"string":"Device is in STANDBY mode, exit(2)","severity":"information"}]}}`,
			want: true,
		},
		{
			name: "sleep message",
			out: `{"smartctl":{"exit_status":2,"messages":[
				{"string":"Device is in SLEEP mode, exit(2)","severity":"information"}]}}`,
			want: true,
		},
		{
			name: "idle message",
			out: `{"smartctl":{"exit_status":2,"messages":[
				{"string":"Device is in IDLE mode, exit(2)","severity":"information"}]}}`,
			want: true,
		},
		{
			// The App's Protection Mode case. Same exit code, entirely
			// different meaning: this drive must never be published.
			name: "permission denied is not standby",
			out: `{"smartctl":{"exit_status":2,"messages":[
				{"string":"Smartctl open device: /dev/nvme0 failed: Operation not permitted","severity":"error"}]}}`,
			want: false,
		},
		{
			name: "device busy is not standby",
			out: `{"smartctl":{"exit_status":2,"messages":[
				{"string":"Smartctl open device: /dev/sda failed: Device or resource busy","severity":"error"}]}}`,
			want: false,
		},
		{
			name: "no such device is not standby",
			out: `{"smartctl":{"exit_status":2,"messages":[
				{"string":"Smartctl open device: /dev/sdz failed: No such device","severity":"error"}]}}`,
			want: false,
		},
		{
			name: "no messages at all",
			out:  `{"smartctl":{"exit_status":2}}`,
			want: false,
		},
		{
			name: "malformed json is not standby",
			out:  `not json at all`,
			want: false,
		},
		{
			name: "empty output",
			out:  ``,
			want: false,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := smartctlReportsLowPower([]byte(tc.out)); got != tc.want {
				t.Errorf("smartctlReportsLowPower() = %v, want %v", got, tc.want)
			}
		})
	}
}

// makeDriveSlug falls back to the device path when the serial is empty, which
// is correct in isolation but fabricates an identity when the serial is empty
// only because the drive could not be read. This test pins the behaviour that
// makes the early return in fetchDriveInfo necessary: without it, a blocked
// /dev/nvme0 is published as the drive "dev-nvme0".
func TestMakeDriveSlugFabricatesIdentityWithoutSerial(t *testing.T) {
	if got := makeDriveSlug("", "/dev/nvme0"); got != "dev-nvme0" {
		t.Fatalf("makeDriveSlug(\"\", \"/dev/nvme0\") = %q, want %q (the app#7 ghost ID)", got, "dev-nvme0")
	}
	// With a real serial the slug is stable and path-independent, which is why
	// preserving the cached entry keeps Home Assistant pointed at one device.
	a := makeDriveSlug("X1Y2Z3", "/dev/nvme0")
	b := makeDriveSlug("X1Y2Z3", "/dev/nvme1")
	if a != b {
		t.Fatalf("serial-based slug should not depend on device path: %q vs %q", a, b)
	}
}

// fetchOutcome must keep standby and unreadable distinct. A bool cannot, which
// is the root of app#7.
func TestFetchOutcomesAreDistinct(t *testing.T) {
	if fetchOK == fetchStandby || fetchOK == fetchUnreadable || fetchStandby == fetchUnreadable {
		t.Fatal("fetchOutcome values must be distinct")
	}
}

// fetchDriveInfo must return fetchUnreadable, never fetchOK, when smartctl
// fails to execute at all. Before this was enforced, an exec failure fell
// through to build a DriveInfo with an empty serial (so the ID became the
// device path, e.g. "dev-nvme0"), Readable=true, and a RawJSON that was either
// nil or an empty non-nil slice. Nil marshals to null and crashes the
// integration's attention parser; empty fails json.Marshal outright, which
// blanks /api/drives/{id} and takes every drive on the agent unavailable.
// A hung drive hitting the 30s timeout is the realistic trigger.
func TestFetchDriveInfoExecFailureIsUnreadable(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a shell script as a fake smartctl")
	}

	newCache := func(smartctlPath string) *DriveCache {
		cfg := &Config{
			ScanInterval: time.Minute,
			SmartctlPath: smartctlPath,
			StandbyMode:  "never",
		}
		return NewDriveCache(cfg)
	}

	t.Run("binary missing", func(t *testing.T) {
		dc := newCache("/nonexistent/smartctl")
		info, outcome := dc.fetchDriveInfo("/dev/nvme0", "nvme", true)
		if outcome != fetchUnreadable {
			t.Fatalf("outcome = %v, want fetchUnreadable", outcome)
		}
		if info.ID != "" || info.Readable {
			t.Fatalf("must not fabricate an identity: ID=%q Readable=%v", info.ID, info.Readable)
		}
	})

	t.Run("timeout", func(t *testing.T) {
		dir := t.TempDir()
		hung := filepath.Join(dir, "smartctl")
		if err := os.WriteFile(hung, []byte("#!/bin/sh\nsleep 5\n"), 0o755); err != nil {
			t.Fatal(err)
		}
		saved := smartctlTimeout
		smartctlTimeout = 200 * time.Millisecond
		t.Cleanup(func() { smartctlTimeout = saved })

		dc := newCache(hung)
		start := time.Now()
		info, outcome := dc.fetchDriveInfo("/dev/sda", "ata", true)
		if elapsed := time.Since(start); elapsed > 3*time.Second {
			t.Fatalf("timeout did not fire: took %v", elapsed)
		}
		if outcome != fetchUnreadable {
			t.Fatalf("outcome = %v, want fetchUnreadable", outcome)
		}
		if info.ID != "" || info.Readable {
			t.Fatalf("must not fabricate an identity: ID=%q Readable=%v", info.ID, info.Readable)
		}
	})

	t.Run("control: healthy output is fetchOK with serial ID", func(t *testing.T) {
		dir := t.TempDir()
		fake := filepath.Join(dir, "smartctl")
		script := "#!/bin/sh\n" +
			`printf '%s' '{"model_name":"FAKE DRIVE","serial_number":"SN123","smart_status":{"passed":true}}'` + "\n"
		if err := os.WriteFile(fake, []byte(script), 0o755); err != nil {
			t.Fatal(err)
		}
		dc := newCache(fake)
		info, outcome := dc.fetchDriveInfo("/dev/sda", "ata", true)
		if outcome != fetchOK {
			t.Fatalf("outcome = %v, want fetchOK", outcome)
		}
		if info.ID != "sn123" {
			t.Fatalf("ID = %q, want serial-based %q", info.ID, "sn123")
		}
		if !info.Readable {
			t.Fatal("healthy drive must be Readable")
		}
	})
}
