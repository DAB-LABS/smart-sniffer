package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const configWithOverrides = `port: 9099
scan_interval: 60s
device_overrides:
  - device: /dev/sda
    protocol: megaraid,0
  - device: /dev/sdb
    protocol: sat
# my note
standby_mode: standby
`

const configWithout = "port: 9099\nscan_interval: 60s\n"

func writeConfig(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func confirm(t *testing.T, content, input string, terminal, yes bool) (bool, string) {
	t.Helper()
	var out strings.Builder
	a := discoverAsk{in: strings.NewReader(input), terminal: terminal, yes: yes, out: &out}
	ok, err := a.confirmWrite(writeConfig(t, content))
	if err != nil {
		t.Fatal(err)
	}
	return ok, out.String()
}

// No device_overrides yet: the question is as it was, Enter means yes.
func TestDiscoverConfirmWithoutOverrides(t *testing.T) {
	for _, tc := range []struct {
		input    string
		terminal bool
		yes      bool
		want     bool
	}{
		{"\n", true, false, true},
		{"y\n", true, false, true},
		{"n\n", true, false, false},
		{"", false, false, true}, // unchanged from before v1.0: no input is the default answer
		{"n\n", false, true, true},
	} {
		got, out := confirm(t, configWithout, tc.input, tc.terminal, tc.yes)
		if got != tc.want {
			t.Errorf("input %q terminal=%v yes=%v: write=%v, want %v\n%s", tc.input, tc.terminal, tc.yes, got, tc.want, out)
		}
		if strings.Contains(out, "WARNING") {
			t.Errorf("warned with no device_overrides:\n%s", out)
		}
	}
}

// device_overrides already there: a warning listing them, then a question
// whose default is no.
func TestDiscoverConfirmWarnsBeforeReplacing(t *testing.T) {
	got, out := confirm(t, configWithOverrides, "\n", true, false)
	if got {
		t.Errorf("Enter replaced the existing device_overrides")
	}
	for _, want := range []string{"WARNING:", "already has device_overrides", "/dev/sda", "megaraid,0", "/dev/sdb", "Replace them? [y/N]", ".bak"} {
		if !strings.Contains(out, want) {
			t.Errorf("output lacks %q:\n%s", want, out)
		}
	}
	for _, input := range []string{"y\n", "yes\n", "Y\n"} {
		if got, _ := confirm(t, configWithOverrides, input, true, false); !got {
			t.Errorf("input %q: did not replace", input)
		}
	}
	for _, input := range []string{"n\n", "no\n", "", "maybe\n"} {
		if got, _ := confirm(t, configWithOverrides, input, true, false); got {
			t.Errorf("input %q: replaced", input)
		}
	}
}

// No terminal: never replaces, never reads input, says how to force.
func TestDiscoverConfirmNoTerminalDoesNotReplace(t *testing.T) {
	got, out := confirm(t, configWithOverrides, "y\n", false, false)
	if got {
		t.Fatalf("replaced with no terminal")
	}
	if !strings.Contains(out, "--yes") || !strings.Contains(out, "Not changed") {
		t.Errorf("output does not say how to force:\n%s", out)
	}
	if strings.Contains(out, "[y/N]") {
		t.Errorf("asked with no terminal:\n%s", out)
	}
}

func TestDiscoverConfirmYesReplaces(t *testing.T) {
	got, out := confirm(t, configWithOverrides, "", false, true)
	if !got || !strings.Contains(out, "WARNING") || !strings.Contains(out, "(--yes)") {
		t.Errorf("--yes: write=%v\n%s", got, out)
	}
}

// An empty device_overrides is nothing to lose: no warning.
func TestDiscoverConfirmEmptyOverridesIsNotAWarning(t *testing.T) {
	for _, content := range []string{"port: 9099\ndevice_overrides: []\n", "port: 9099\ndevice_overrides:\n"} {
		_, out := confirm(t, content, "\n", true, false)
		if strings.Contains(out, "WARNING") {
			t.Errorf("%q warned:\n%s", content, out)
		}
	}
}

func TestSpliceDeviceOverrides(t *testing.T) {
	section := "device_overrides:\n    - device: /dev/sata1\n      protocol: sat\n"
	cases := map[string]string{
		// Appended when absent, with a newline added if the file lacks one.
		"port: 9099\n": "port: 9099\n" + section,
		"port: 9099":   "port: 9099\n" + section,
		"":             section,
		// Replaced in place, the rest of the file untouched.
		configWithOverrides: "port: 9099\nscan_interval: 60s\n" + section + "# my note\nstandby_mode: standby\n",
		// Compact sequence style, at the end of the file.
		"port: 9099\ndevice_overrides:\n- device: /dev/sdc\n  protocol: sat\n": "port: 9099\n" + section,
		// Flow style on one line, first in the file.
		"device_overrides: [{device: /dev/sdc, protocol: sat}]\nport: 9099\n": section + "port: 9099\n",
		// A blank line inside the block goes with it; one after it stays.
		"device_overrides:\n  - device: /dev/sdc\n\n    protocol: sat\n\nport: 9099\n": section + "\nport: 9099\n",
	}
	for in, want := range cases {
		if got := spliceDeviceOverrides(in, section); got != want {
			t.Errorf("splice(%q)\n got %q\nwant %q", in, got, want)
		}
	}
	// A key that only starts with the name is not the key.
	in := "device_overrides_note: x\n"
	if got := spliceDeviceOverrides(in, section); got != in+section {
		t.Errorf("splice(%q) = %q", in, got)
	}
}

// The written file has one device_overrides, the new entries, and every other
// setting as it was; the old file is kept as .bak.
func TestWriteDeviceOverridesReplaces(t *testing.T) {
	path := writeConfig(t, configWithOverrides)
	drives := []discoverDriveResult{{path: "/dev/sata1"}, {path: "/dev/sata2"}}
	if err := writeDeviceOverrides(path, drives); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(path)
	if n := strings.Count(string(data), "device_overrides:"); n != 1 {
		t.Fatalf("device_overrides appears %d times:\n%s", n, data)
	}
	var cfg Config
	if err := decodeConfigFile(data, &cfg); err != nil {
		t.Fatalf("written file does not parse: %v\n%s", err, data)
	}
	if len(cfg.DeviceOverrides) != 2 || cfg.DeviceOverrides[0].Device != "/dev/sata1" || cfg.StandbyMode != "standby" || cfg.Port != 9099 {
		t.Errorf("cfg = %+v", cfg)
	}
	if !strings.Contains(string(data), "# my note") {
		t.Errorf("comment lost:\n%s", data)
	}
	bak, _ := os.ReadFile(path + ".bak")
	if string(bak) != configWithOverrides {
		t.Errorf(".bak = %q", bak)
	}
}

// A result that would not parse is not written.
func TestWriteDeviceOverridesRefusesABrokenResult(t *testing.T) {
	broken := "device_overrides: [\n  {device: /dev/sdc, protocol: sat}\n]\nport: 9099\n"
	path := writeConfig(t, broken)
	err := writeDeviceOverrides(path, []discoverDriveResult{{path: "/dev/sata1"}})
	if err == nil || !strings.Contains(err.Error(), "would not parse") {
		t.Fatalf("err = %v", err)
	}
	if data, _ := os.ReadFile(path); string(data) != broken {
		t.Errorf("file changed:\n%s", data)
	}
}
