package main

import (
	"fmt"
	"os"
	"strings"
	"testing"
)

// v1.0 (J7): one WARNING per unknown top-level key, its name only.
func TestUnknownConfigKeysAreWarnedOncePerKey(t *testing.T) {
	data := []byte("port: 9099\ntokn: \"secret-value-1234\"\nscan_interval: 60\n" +
		"filesystems:\n  - path: /\n    mystery: 1\ndevice_overrides:\n  - device: /dev/sda\n    protocol: sat\n" +
		"scan_intervall: 5m\ntokn: again\n")
	var lines []string
	warnUnknownConfigKeys("/etc/smartha-agent/config.yaml", data, func(f string, a ...any) {
		lines = append(lines, fmt.Sprintf(f, a...))
	})
	want := []string{
		`WARNING: /etc/smartha-agent/config.yaml: unknown key "tokn" is ignored (check its spelling)`,
		`WARNING: /etc/smartha-agent/config.yaml: unknown key "scan_intervall" is ignored (check its spelling)`,
	}
	if strings.Join(lines, "\n") != strings.Join(want, "\n") {
		t.Fatalf("lines =\n%s", strings.Join(lines, "\n"))
	}
	for _, l := range lines {
		if strings.Contains(l, "secret-value") || strings.Contains(l, "again") || strings.Contains(l, "5m") {
			t.Errorf("a value reached the log: %q", l)
		}
	}
}

// Every key the installers and the App write is known: no warning on their
// output. install.sh and install.ps1 (fresh and rewritten, with every
// optional key), the App's mounts.py (JSON, which is YAML), and
// config.yaml.example.
func TestWhatTheInstallersAndTheAppWriteIsKnown(t *testing.T) {
	outputs := map[string]string{
		"install.sh": "port: 9099\nscan_interval: 60s\ntoken: 'pw'\nadvertise_interface: eth0\n" +
			"standby_mode: standby\n\nexclude_devices:\n  - /dev/sdb\n\nfilesystems:\n  - path: \"/\"\n" +
			"    uuid: \"\"\n    device: \"/dev/sda1\"\n    fstype: \"ext4\"\n",
		"install.ps1": "port: 9099\nscan_interval: 60s\ntoken: 'pw'\nadvertise_interface: Ethernet\n" +
			"standby_mode: standby\nsmartctl_path: 'C:\\Program Files\\smartmontools\\bin\\smartctl.exe'\n" +
			"filesystems:\n  - path: \"C:\\\\\"\n    uuid: \"\"\n    device: \"C:\"\n    fstype: \"NTFS\"\n",
		"App mounts.py": `{"filesystems": [{"path": "/data", "uuid": "abc", "device": "/dev/sda8", "fstype": "ext4"}], "token": "pw"}`,
	}
	for who, text := range outputs {
		if got := unknownConfigKeys([]byte(text)); len(got) != 0 {
			t.Errorf("%s: unknown keys %v", who, got)
		}
	}
	example := mustRead(t, "config.yaml.example")
	if got := unknownConfigKeys(example); len(got) != 0 {
		t.Errorf("config.yaml.example: unknown keys %v", got)
	}
}

func TestEveryConfigFieldIsAKnownKey(t *testing.T) {
	known := knownConfigKeys()
	for _, key := range []string{"port", "token", "scan_interval", "mdns", "advertise_interface",
		"mdns_name", "filesystems", "mount_prefix", "standby_mode", "verbose", "device_overrides",
		"exclude_devices", "zfs_pool_status", "zpool_path", "device_statistics", "smartctl_path",
		"agent_id"} {
		if !known[key] {
			t.Errorf("%s is not known", key)
		}
		delete(known, key)
	}
	if len(known) != 0 {
		t.Errorf("keys not listed here (add them to the docs table too): %v", known)
	}
}

func mustRead(t *testing.T, path string) []byte {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// v1.0 (J12): --advertise-interface is --interface; both is an error.
func TestAdvertiseInterfaceAlias(t *testing.T) {
	cfg, err := loadWith(t, "", "--advertise-interface", "eth1")
	if err != nil || cfg.AdvertiseInterface != "eth1" {
		t.Fatalf("alias: %v %v", cfg, err)
	}
	cfg, err = loadWith(t, "advertise_interface: eth0\n", "--interface", "eth2")
	if err != nil || cfg.AdvertiseInterface != "eth2" {
		t.Fatalf("--interface over the file: %v %v", cfg, err)
	}
	if _, err := loadWith(t, "", "--interface", "eth1", "--advertise-interface", "eth1"); err == nil ||
		!strings.Contains(err.Error(), "give one of them") {
		t.Fatalf("both: err = %v", err)
	}
}

// v1.0 (J12): SMARTHA_TOKEN sits between --token and the file; empty is
// not set. Only the source is kept for the log.
func TestSmarthaTokenPrecedence(t *testing.T) {
	cases := []struct {
		file, env string
		args      []string
		token     string
		source    string
	}{
		{"token: from-file\n", "", nil, "from-file", "config file"},
		{"token: from-file\n", "from-env", nil, "from-env", "SMARTHA_TOKEN"},
		{"", "from-env", nil, "from-env", "SMARTHA_TOKEN"},
		{"token: from-file\n", "from-env", []string{"--token", "from-flag"}, "from-flag", "--token"},
		{"token: from-file\n", "", []string{"--token", "from-flag"}, "from-flag", "--token"},
		{"", "", nil, "", ""},
	}
	for _, tc := range cases {
		t.Setenv("SMARTHA_TOKEN", tc.env)
		cfg, err := loadWith(t, tc.file+"port: 9099\n", tc.args...)
		if err != nil {
			t.Fatal(err)
		}
		if cfg.Token != tc.token || cfg.TokenSource != tc.source {
			t.Errorf("file %q env %q args %v: token %q from %q, want %q from %q",
				tc.file, tc.env, tc.args, cfg.Token, cfg.TokenSource, tc.token, tc.source)
		}
	}
}

// config.yaml.example names every key the agent reads (J12).
func TestTheExampleListsEveryKey(t *testing.T) {
	example := string(mustRead(t, "config.yaml.example"))
	for key := range knownConfigKeys() {
		if !strings.Contains(example, "\n"+key+":") && !strings.Contains(example, "\n# "+key+":") {
			t.Errorf("config.yaml.example does not list %s", key)
		}
	}
}
