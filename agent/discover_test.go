package main

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// The readability verdict and the SAT retry trigger are the shared definition
// both the runtime poll and --discover use. GH #51: --discover used bits 0-2
// for readability while the runtime used bits 0-1, so a drive exiting 4 every
// poll was listed as unreadable and then worked in Home Assistant.
func TestSmartctlExitCodeHelpers(t *testing.T) {
	for code := 0; code <= 255; code++ {
		wantReadable := code&0x03 == 0
		if got := smartctlReadable(code); got != wantReadable {
			t.Errorf("smartctlReadable(%d) = %v, want %v", code, got, wantReadable)
		}
		wantRetry := code&0x07 != 0
		if got := smartctlWantsSATRetry(code); got != wantRetry {
			t.Errorf("smartctlWantsSATRetry(%d) = %v, want %v", code, got, wantRetry)
		}
	}
	// Spot checks that state the intent in words.
	if !smartctlReadable(4) {
		t.Error("exit 4 (some commands failed) must read as readable")
	}
	if !smartctlWantsSATRetry(4) {
		t.Error("exit 4 must still trigger the SAT retry (QNAP signals the mismatch with bit 2)")
	}
	if smartctlReadable(2) || smartctlReadable(1) || smartctlReadable(6) {
		t.Error("bits 0-1 must read as unreadable")
	}
}

// A drive shaped like the GH #51 report: SK hynix SC311 on OpenWrt x86-64.
const fakeGoodBody = `{"json_format_version":[1,0],"smartctl":{"version":[7,4]},` +
	`"device":{"name":"/dev/sda","info_name":"/dev/sda","type":"sat","protocol":"ATA"},` +
	`"model_name":"SK hynix SC311 SATA 256GB","serial_number":"MI71N000000000000",` +
	`"smart_status":{"passed":true}}`

const fakeErrBody = `{"json_format_version":[1,0],"smartctl":{"version":[7,4],` +
	`"messages":[{"string":"Smartctl open device: /dev/sda failed: No such device","severity":"error"}]}}`

// writeFakeSmartctl writes a shell script that stands in for smartctl. It
// exits origCode for a normal call and satCode when called with "-d sat",
// printing a readable JSON body unless bits 0-1 are set. Every invocation's
// arguments are appended to the returned log file.
func writeFakeSmartctl(t *testing.T, origCode, satCode int) (path, logPath string) {
	t.Helper()
	dir := t.TempDir()
	path = filepath.Join(dir, "smartctl")
	logPath = filepath.Join(dir, "calls.log")
	script := "#!/bin/sh\n" +
		"echo \"$*\" >> '" + logPath + "'\n" +
		"sat=0\nprev=\"\"\n" +
		"for a in \"$@\"; do\n" +
		"  if [ \"$prev\" = \"-d\" ] && [ \"$a\" = \"sat\" ]; then sat=1; fi\n" +
		"  prev=\"$a\"\n" +
		"done\n" +
		fmt.Sprintf("if [ $sat = 1 ]; then code=%d; else code=%d; fi\n", satCode, origCode) +
		"if [ $((code & 3)) -ne 0 ]; then\n" +
		"  printf '%s' '" + fakeErrBody + "'\n" +
		"else\n" +
		"  printf '%s' '" + fakeGoodBody + "'\n" +
		"fi\n" +
		"exit $code\n"
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return path, logPath
}

func satCalls(t *testing.T, logPath string) int {
	t.Helper()
	b, err := os.ReadFile(logPath)
	if err != nil {
		if os.IsNotExist(err) {
			return 0
		}
		t.Fatal(err)
	}
	n := 0
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		if strings.Contains(line, "-d sat") {
			n++
		}
	}
	return n
}

// captureDriveResult returns what printDriveResult writes to stdout.
func captureDriveResult(t *testing.T, r discoverDriveResult) string {
	t.Helper()
	saved := os.Stdout
	rd, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	printDriveResult(r, &Config{})
	w.Close()
	os.Stdout = saved
	var buf bytes.Buffer
	if _, err := io.Copy(&buf, rd); err != nil {
		t.Fatal(err)
	}
	return buf.String()
}

func TestProbeOneDriveExitCodes(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a shell script as a fake smartctl")
	}

	tests := []struct {
		name       string
		protocol   string
		origCode   int
		satCode    int
		wantSAT    bool // a SAT retry was attempted
		wantSmart  bool // readable via the scan protocol
		wantSATOK  bool // readable via SAT
		wantNote   bool
		wantOutput []string
	}{
		{
			name: "exit 0", protocol: "ata", origCode: 0, satCode: 0,
			wantSmart:  true,
			wantOutput: []string{"SMART data: Yes", "SK hynix SC311 SATA 256GB", "Result:     OK"},
		},
		{
			name: "exit 4 non-SCSI", protocol: "ata", origCode: 4, satCode: 0,
			wantSmart: true, wantNote: true,
			wantOutput: []string{
				"SMART data: Yes",
				"    Note:       smartctl reported some commands unsupported (exit code 4); the agent reads this drive normally\n",
				"Result:     OK",
			},
		},
		{
			name: "exit 2", protocol: "ata", origCode: 2, satCode: 0,
			wantOutput: []string{"SMART data: No", "WARNING: could not read SMART data"},
		},
		{
			name: "SCSI exit 4 SAT fails", protocol: "scsi", origCode: 4, satCode: 2,
			wantSAT: true, wantSmart: true, wantNote: true,
			wantOutput: []string{
				"SMART data:    Yes",
				"SAT retry:     No -- keeping scan protocol",
				"    Note:          smartctl reported some commands unsupported (exit code 4); the agent reads this drive normally\n",
				"Result:        OK",
			},
		},
		{
			name: "SCSI exit 4 SAT succeeds", protocol: "scsi", origCode: 4, satCode: 0,
			wantSAT: true, wantSATOK: true,
			wantOutput: []string{"SAT retry:     Yes -- SMART data available", "Result:        OK (agent will auto-detect SAT at runtime)"},
		},
		{
			name: "SCSI exit 2 SAT succeeds", protocol: "scsi", origCode: 2, satCode: 0,
			wantSAT: true, wantSATOK: true,
			wantOutput: []string{"SMART data:    No", "SAT retry:     Yes -- SMART data available"},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			fake, logPath := writeFakeSmartctl(t, tc.origCode, tc.satCode)
			r := probeOneDrive(fake, "/dev/sda", tc.protocol)

			if got := satCalls(t, logPath) > 0; got != tc.wantSAT {
				t.Errorf("SAT attempted = %v, want %v", got, tc.wantSAT)
			}
			if r.satRetried != tc.wantSAT || r.smartOK != tc.wantSmart || r.satOK != tc.wantSATOK {
				t.Errorf("satRetried=%v smartOK=%v satOK=%v, want %v %v %v",
					r.satRetried, r.smartOK, r.satOK, tc.wantSAT, tc.wantSmart, tc.wantSATOK)
			}
			if (r.partialCode != 0) != tc.wantNote {
				t.Errorf("partialCode = %d, note wanted = %v", r.partialCode, tc.wantNote)
			}
			if (r.smartOK || r.satOK) && r.model != "SK hynix SC311 SATA 256GB" {
				t.Errorf("model = %q", r.model)
			}

			out := captureDriveResult(t, r)
			t.Logf("--discover output:%s", out)
			for _, want := range tc.wantOutput {
				if !strings.Contains(out, want) {
					t.Errorf("output missing %q:\n%s", want, out)
				}
			}
			if !tc.wantNote && strings.Contains(out, "Note:") {
				t.Errorf("unexpected note:\n%s", out)
			}
		})
	}
}

// runtimeVerdict runs fetchDriveInfo against a fake smartctl and reports
// whether the drive was published and whether it ended up on SAT.
func runtimeVerdict(t *testing.T, fake, protocol string) (readable, viaSAT bool) {
	t.Helper()
	cfg := &Config{ScanInterval: time.Minute, SmartctlPath: fake, StandbyMode: "never"}
	dc := NewDriveCache(cfg)
	_, outcome := dc.fetchDriveInfo("/dev/sda", protocol, true)
	dc.mu.RLock()
	viaSAT = dc.protocolCache["/dev/sda"] == "sat"
	dc.mu.RUnlock()
	return outcome == fetchOK, viaSAT
}

// The runtime verdict for the GH #51 cases, pinned so the call-site swap in
// fetchDriveInfo provably changed nothing.
func TestFetchDriveInfoVerdictUnchanged(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a shell script as a fake smartctl")
	}
	tests := []struct {
		name               string
		protocol           string
		origCode, satCode  int
		wantReadable, wSAT bool
	}{
		{"exit 0", "ata", 0, 0, true, false},
		{"exit 4 non-SCSI", "ata", 4, 0, true, false},
		{"exit 2", "ata", 2, 0, false, false},
		{"exit 8 health concern still read", "ata", 8, 0, true, false},
		{"exit 64 error log still read", "ata", 64, 0, true, false},
		{"SCSI exit 4 SAT fails", "scsi", 4, 2, true, false},
		{"SCSI exit 4 SAT returns 4", "scsi", 4, 4, true, false},
		{"SCSI exit 4 SAT succeeds", "scsi", 4, 0, true, true},
		{"SCSI exit 2 SAT succeeds", "scsi", 2, 0, true, true},
		{"SCSI exit 2 SAT fails", "scsi", 2, 2, false, false},
		{"SCSI exit 2 SAT returns 4", "scsi", 2, 4, false, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			fake, _ := writeFakeSmartctl(t, tc.origCode, tc.satCode)
			readable, viaSAT := runtimeVerdict(t, fake, tc.protocol)
			if readable != tc.wantReadable || viaSAT != tc.wSAT {
				t.Errorf("readable=%v viaSAT=%v, want %v %v", readable, viaSAT, tc.wantReadable, tc.wSAT)
			}
		})
	}
}

// --discover and the runtime must reach the same verdict for every exit code,
// including which protocol wins when a SCSI drive is retried with SAT.
func TestDiscoverMatchesRuntimeForEveryExitCode(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a shell script as a fake smartctl")
	}
	if testing.Short() {
		t.Skip("spawns a few thousand fake smartctl processes")
	}
	type combo struct {
		protocol          string
		origCode, satCode int
	}
	var combos []combo
	for code := 0; code <= 255; code++ {
		combos = append(combos, combo{"ata", code, 0})
		for _, sat := range []int{0, 2, 4} {
			combos = append(combos, combo{"scsi", code, sat})
		}
	}
	for _, c := range combos {
		fake, _ := writeFakeSmartctl(t, c.origCode, c.satCode)
		rtReadable, rtSAT := runtimeVerdict(t, fake, c.protocol)
		r := probeOneDrive(fake, "/dev/sda", c.protocol)
		if dReadable := r.smartOK || r.satOK; dReadable != rtReadable || r.satOK != rtSAT {
			t.Errorf("%s orig=%d sat=%d: discover readable=%v viaSAT=%v, runtime readable=%v viaSAT=%v",
				c.protocol, c.origCode, c.satCode, dReadable, r.satOK, rtReadable, rtSAT)
		}
	}
}
