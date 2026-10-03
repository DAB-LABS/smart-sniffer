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
	_, outcome, _ := dc.fetchDriveInfo("/dev/sda", protocol, true, false)
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

	// The exec-error case: smartctl never ran to completion on the first read.
	// Both paths call it unreadable and neither retries with SAT.
	shortenSmartctlTimeout(t)
	hung, hungLog := writeFakeSmartctlFailing(t, "hang", "0")
	for _, c := range []struct{ smartctl, protocol string }{
		{"/nonexistent/smartctl", "ata"},
		{"/nonexistent/smartctl", "scsi"},
		{"/nonexistent/smartctl", "sat"},
		{hung, "scsi"},
	} {
		rtReadable, rtSAT := runtimeVerdict(t, c.smartctl, c.protocol)
		r := probeOneDrive(c.smartctl, "/dev/sda", c.protocol)
		if rtReadable || rtSAT || r.smartOK || r.satOK || r.satRetried || r.execErr == "" {
			t.Errorf("%s %s: runtime readable=%v viaSAT=%v, discover smartOK=%v satOK=%v satRetried=%v execErr=%q",
				c.smartctl, c.protocol, rtReadable, rtSAT, r.smartOK, r.satOK, r.satRetried, r.execErr)
		}
	}
	if n := satCalls(t, hungLog); n != 0 {
		t.Errorf("a SAT retry ran after the first read timed out (%d calls)", n)
	}
}

// shortenSmartctlTimeout lowers the shared runSmartctl timeout for one test.
func shortenSmartctlTimeout(t *testing.T) {
	t.Helper()
	saved := smartctlTimeout
	smartctlTimeout = 200 * time.Millisecond
	t.Cleanup(func() { smartctlTimeout = saved })
}

// writeFakeSmartctlFailing writes a fake smartctl whose first read and SAT
// retry each either exit with a code or fail in a named way:
//
//	"hang"     exec sleep, so the timeout fires (exec, so the kill reaches the
//	           sleeping process directly and WaitDelay is not needed)
//	"nolaunch" the first read exits origCode and then removes the script's
//	           execute bit, so the SAT retry cannot be launched
//
// A numeric action exits with that code, printing a body as writeFakeSmartctl
// does. Every invocation's arguments are appended to the returned log file.
func writeFakeSmartctlFailing(t *testing.T, orig, sat string) (path, logPath string) {
	t.Helper()
	dir := t.TempDir()
	path = filepath.Join(dir, "smartctl")
	logPath = filepath.Join(dir, "calls.log")
	action := func(a string) string {
		switch a {
		case "hang":
			return "exec sleep 5\n"
		default:
			return "code=" + a + "\n" +
				"if [ $((code & 3)) -ne 0 ]; then printf '%s' '" + fakeErrBody + "'; " +
				"else printf '%s' '" + fakeGoodBody + "'; fi\n" +
				"exit $code\n"
		}
	}
	origAction := orig
	nolaunch := ""
	if strings.HasPrefix(sat, "nolaunch") {
		nolaunch = "chmod -x \"$0\"\n"
	}
	script := "#!/bin/sh\n" +
		"echo \"$*\" >> '" + logPath + "'\n" +
		"sat=0\nprev=\"\"\n" +
		"for a in \"$@\"; do\n" +
		"  if [ \"$prev\" = \"-d\" ] && [ \"$a\" = \"sat\" ]; then sat=1; fi\n" +
		"  prev=\"$a\"\n" +
		"done\n" +
		"if [ $sat = 1 ]; then\n" + action(sat) + "fi\n" +
		nolaunch +
		action(origAction)
	if err := os.WriteFile(path, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return path, logPath
}

// --discover runs smartctl the way the runtime does: the same timeout, and an
// exec error (launch failure or timeout) on the first read means unreadable
// with no SAT retry. An exec error on the SAT retry leaves the original
// verdict standing. Each case is also checked against fetchDriveInfo.
func TestProbeOneDriveExecErrors(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a shell script as a fake smartctl")
	}
	if os.Geteuid() == 0 {
		t.Skip("root ignores the execute bit, so the launch-failure cases cannot fail")
	}
	shortenSmartctlTimeout(t)

	notExec := filepath.Join(t.TempDir(), "smartctl")
	if err := os.WriteFile(notExec, []byte("#!/bin/sh\nexit 0\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	tests := []struct {
		name      string
		smartctl  func(t *testing.T) (path, logPath string)
		protocol  string
		wantSAT   bool // a SAT retry was made (r.satRetried)
		satRan    bool // the fake saw the SAT call (false when it could not launch)
		wantSmart bool
		wantErr   string // r.execErr
		wantSATEr string // r.satExecErr
		wantOut   []string
	}{
		{
			name:     "first read times out",
			smartctl: func(t *testing.T) (string, string) { return writeFakeSmartctlFailing(t, "hang", "0") },
			protocol: "scsi",
			wantErr:  "smartctl timed out after 200ms (device may be unresponsive)",
			wantOut: []string{
				"    Protocol:   scsi\n",
				"    SMART data: No\n",
				"    Error:      smartctl timed out after 200ms (device may be unresponsive)\n",
				"    Result:     WARNING: could not read SMART data\n",
			},
		},
		{
			name:     "smartctl path does not exist",
			smartctl: func(t *testing.T) (string, string) { return "/nonexistent/smartctl", "" },
			protocol: "ata",
			wantErr:  "smartctl did not run (fork/exec /nonexistent/smartctl: no such file or directory)",
			wantOut: []string{
				"    SMART data: No\n",
				"    Error:      smartctl did not run (fork/exec /nonexistent/smartctl: no such file or directory)\n",
				"    Result:     WARNING: could not read SMART data\n",
			},
		},
		{
			name:     "smartctl not on PATH",
			smartctl: func(t *testing.T) (string, string) { return "smartctl-not-installed-here", "" },
			protocol: "ata",
			wantErr:  `smartctl did not run (exec: "smartctl-not-installed-here": executable file not found in $PATH)`,
			wantOut: []string{
				`    Error:      smartctl did not run (exec: "smartctl-not-installed-here": executable file not found in $PATH)` + "\n",
			},
		},
		{
			name:     "smartctl not executable",
			smartctl: func(t *testing.T) (string, string) { return notExec, "" },
			protocol: "ata",
			wantErr:  "smartctl did not run (fork/exec " + notExec + ": permission denied)",
			wantOut:  []string{"    Error:      smartctl did not run (fork/exec " + notExec + ": permission denied)\n"},
		},
		{
			name:      "SCSI exit 4, SAT retry hangs",
			smartctl:  func(t *testing.T) (string, string) { return writeFakeSmartctlFailing(t, "4", "hang") },
			protocol:  "scsi",
			wantSAT:   true,
			satRan:    true,
			wantSmart: true,
			wantSATEr: "smartctl timed out after 200ms (device may be unresponsive)",
			wantOut: []string{
				"    SMART data:    Yes\n",
				"    SAT retry:     No -- keeping scan protocol\n",
				"    SAT error:     smartctl timed out after 200ms (device may be unresponsive)\n",
				"    Result:        OK\n",
			},
		},
		{
			name:      "SCSI exit 2, SAT retry hangs",
			smartctl:  func(t *testing.T) (string, string) { return writeFakeSmartctlFailing(t, "2", "hang") },
			protocol:  "scsi",
			wantSAT:   true,
			satRan:    true,
			wantSATEr: "smartctl timed out after 200ms (device may be unresponsive)",
			wantOut: []string{
				"    SMART data:    No\n",
				"    SAT retry:     No -- drive not readable\n",
				"    SAT error:     smartctl timed out after 200ms (device may be unresponsive)\n",
				"    Result:        WARNING: could not read SMART data\n",
			},
		},
		{
			name:      "SCSI exit 4, SAT retry cannot launch",
			smartctl:  func(t *testing.T) (string, string) { return writeFakeSmartctlFailing(t, "4", "nolaunch") },
			protocol:  "scsi",
			wantSAT:   true,
			wantSmart: true,
			wantSATEr: "smartctl did not run (fork/exec ",
			wantOut: []string{
				"    SAT retry:     No -- keeping scan protocol\n",
				"    SAT error:     smartctl did not run (fork/exec ",
				": permission denied)\n",
				"    Result:        OK\n",
			},
		},
		{
			name:      "SCSI exit 2, SAT retry cannot launch",
			smartctl:  func(t *testing.T) (string, string) { return writeFakeSmartctlFailing(t, "2", "nolaunch") },
			protocol:  "scsi",
			wantSAT:   true,
			wantSATEr: "smartctl did not run (fork/exec ",
			wantOut: []string{
				"    SAT retry:     No -- drive not readable\n",
				"    SAT error:     smartctl did not run (fork/exec ",
				"    Result:        WARNING: could not read SMART data\n",
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			fake, logPath := tc.smartctl(t)
			start := time.Now()
			r := probeOneDrive(fake, "/dev/sda", tc.protocol)
			if elapsed := time.Since(start); elapsed > 2*time.Second {
				t.Fatalf("probeOneDrive took %v; the timeout did not hold", elapsed)
			}

			if logPath != "" {
				if got := satCalls(t, logPath) > 0; got != tc.satRan {
					t.Errorf("fake saw a SAT call = %v, want %v", got, tc.satRan)
				}
			}
			if r.satRetried != tc.wantSAT || r.smartOK != tc.wantSmart || r.satOK {
				t.Errorf("satRetried=%v smartOK=%v satOK=%v, want %v %v false",
					r.satRetried, r.smartOK, r.satOK, tc.wantSAT, tc.wantSmart)
			}
			if r.execErr != tc.wantErr {
				t.Errorf("execErr = %q, want %q", r.execErr, tc.wantErr)
			}
			if !strings.HasPrefix(r.satExecErr, tc.wantSATEr) || (tc.wantSATEr == "") != (r.satExecErr == "") {
				t.Errorf("satExecErr = %q, want prefix %q", r.satExecErr, tc.wantSATEr)
			}
			if !tc.wantSmart && r.model != "" {
				t.Errorf("unreadable drive carries model %q", r.model)
			}

			out := captureDriveResult(t, r)
			t.Logf("--discover output:%s", out)
			for _, want := range tc.wantOut {
				if !strings.Contains(out, want) {
					t.Errorf("output missing %q:\n%s", want, out)
				}
			}

			// Same verdict as the runtime. The SAT launch-failure fake has
			// already removed its own execute bit, so rebuild it.
			if logPath != "" {
				fake, _ = tc.smartctl(t)
			}
			rtReadable, rtSAT := runtimeVerdict(t, fake, tc.protocol)
			if rtReadable != (r.smartOK || r.satOK) || rtSAT != r.satOK {
				t.Errorf("runtime readable=%v viaSAT=%v, discover readable=%v viaSAT=%v",
					rtReadable, rtSAT, r.smartOK || r.satOK, r.satOK)
			}
		})
	}
}

// smartctlExecErrorText relies on runSmartctl's own wording for a timeout.
// Pin it here so a change to that message is caught where it matters.
func TestSmartctlExecErrorTextMatchesRunSmartctl(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses a shell script as a fake smartctl")
	}
	shortenSmartctlTimeout(t)
	fake, _ := writeFakeSmartctlFailing(t, "hang", "0")
	_, code, err := runSmartctl(fake, []string{"--json", "-a", "/dev/sda"})
	if err == nil || code != -1 {
		t.Fatalf("runSmartctl = (%d, %v), want a timeout error", code, err)
	}
	if got, want := smartctlExecErrorText(err), "smartctl timed out after 200ms (device may be unresponsive)"; got != want {
		t.Errorf("timeout text = %q, want %q", got, want)
	}
	_, _, err = runSmartctl("/nonexistent/smartctl", nil)
	if got := smartctlExecErrorText(err); !strings.HasPrefix(got, "smartctl did not run (") {
		t.Errorf("launch failure text = %q", got)
	}
}
