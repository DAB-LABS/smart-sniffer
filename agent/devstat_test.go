package main

import (
	"encoding/json"
	"errors"
	"flag"
	"go/format"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

// fixture reads testdata/devstat/<name>.<kind>.json.
func fixture(t *testing.T, name, kind string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", "devstat", name+"."+kind+".json"))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// withExit returns a copy of a smartctl JSON document with exit_status set.
func withExit(t *testing.T, body []byte, code int) []byte {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatal(err)
	}
	sc, _ := doc["smartctl"].(map[string]any)
	if sc == nil {
		sc = map[string]any{}
		doc["smartctl"] = sc
	}
	sc["exit_status"] = code
	out, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// edit returns a copy of a JSON document with fn applied to it.
func edit(t *testing.T, body []byte, fn func(map[string]any)) []byte {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal(body, &doc); err != nil {
		t.Fatal(err)
	}
	fn(doc)
	out, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

const standbyBody = `{"json_format_version":[1,0],"smartctl":{"version":[7,5],"messages":[{"string":"Device is in STANDBY mode, exit(2)","severity":"information"}],"exit_status":2}}`

// The devstat call's arguments for every standby_mode and -d form (plan 4.1).
func TestDevstatArgs(t *testing.T) {
	tests := []struct {
		mode, dtype string
		want        string
	}{
		{"never", "", "--json -i -l devstat /dev/sda"},
		{"never", "sat", "--json -i -l devstat -d sat /dev/sda"},
		{"standby", "", "--json -i -l devstat -n standby /dev/sda"},
		{"sleep", "", "--json -i -l devstat -n sleep /dev/sda"},
		{"idle", "", "--json -i -l devstat -n idle /dev/sda"},
		{"standby", "sat", "--json -i -l devstat -n standby -d sat /dev/sda"},
		{"idle", "jmb39x-q,0", "--json -i -l devstat -n idle -d jmb39x-q,0 /dev/sda"},
		{"never", "ata", "--json -i -l devstat -d ata /dev/sda"},
	}
	for _, tc := range tests {
		if got := strings.Join(devstatArgs(tc.mode, tc.dtype, "/dev/sda"), " "); got != tc.want {
			t.Errorf("devstatArgs(%q, %q) = %q, want %q", tc.mode, tc.dtype, got, tc.want)
		}
	}
}

// Every row of the classification table (plan 4.2), first match wins.
func TestClassifyDevstat(t *testing.T) {
	present := fixture(t, "ata_devstat_wdc_unc", "devstat")
	noBlock := fixture(t, "ata_no_devstat_samsung850", "devstat")
	presentSerial := "FIXTURE-m01-sdl"
	noBlockSerial := "FIXTURE-m02-sda"
	emptyPages := edit(t, present, func(d map[string]any) {
		d["ata_device_statistics"] = map[string]any{"pages": []any{}}
	})
	pageFailed := withExit(t, edit(t, noBlock, func(d map[string]any) {
		d["smartctl"].(map[string]any)["messages"] = []any{map[string]any{"string": "Read Device Statistics page 0x00 failed", "severity": "error"}}
	}), 4)

	tests := []struct {
		name    string
		out     []byte
		code    int
		err     error
		serial  string
		want    string
		compl   bool
		wantLBS int
	}{
		{"timeout", nil, -1, &smartctlTimeoutError{timeout: time.Second}, presentSerial, devstatResTimeout, false, 0},
		{"exec error", nil, -1, errors.New("exec: not found"), presentSerial, devstatResFailed, false, 0},
		{"garbage", []byte("not json at all"), 0, nil, presentSerial, devstatResFailed, false, 0},
		{"empty output", nil, 0, nil, presentSerial, devstatResFailed, false, 0},
		{"standby", []byte(standbyBody), 2, nil, "", devstatResStandby, false, 0},
		{"bit 1 not standby", []byte(fakeErrBody), 2, nil, "", devstatResFailed, false, 0},
		{"bit 1 with a block", present, 2, nil, presentSerial, devstatResFailed, false, 0},
		{"bit 0", noBlock, 1, nil, noBlockSerial, devstatResFailed, false, 0},
		{"serial differs", present, 0, nil, "FIXTURE-other", devstatResMismatch, false, 0},
		{"serial missing from devstat", edit(t, present, func(d map[string]any) { delete(d, "serial_number") }), 0, nil, presentSerial, devstatResMismatch, false, 0},
		{"present", present, 0, nil, presentSerial, devstatResPresent, true, 512},
		{"partial (exit 4 with a block)", present, 4, nil, presentSerial, devstatResPresent, false, 512},
		{"no block, bit 2 clear", noBlock, 0, nil, noBlockSerial, devstatResAbsent, false, 0},
		{"empty pages, bit 2 clear", emptyPages, 0, nil, presentSerial, devstatResAbsent, false, 0},
		{"no block, bit 2 set (page 0x00 failed)", pageFailed, 4, nil, noBlockSerial, devstatResFailed, false, 0},
		{"serial-less drive, both empty", edit(t, present, func(d map[string]any) { delete(d, "serial_number") }), 0, nil, "", devstatResPresent, true, 512},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := classifyDevstat(tc.out, tc.code, tc.err, tc.serial)
			if got.kind != tc.want {
				t.Fatalf("kind = %q, want %q", got.kind, tc.want)
			}
			if got.kind != devstatResPresent {
				return
			}
			if got.complete != tc.compl || got.exit != tc.code || got.lbs != tc.wantLBS {
				t.Errorf("complete=%v exit=%d lbs=%d, want %v %d %d", got.complete, got.exit, got.lbs, tc.compl, tc.code, tc.wantLBS)
			}
			var pages []json.RawMessage
			if err := json.Unmarshal(got.pages, &pages); err != nil || len(pages) == 0 {
				t.Errorf("pages = %s", got.pages)
			}
		})
	}
}

// devstatOf classifies a devstat fixture as the agent would publish it.
func devstatOf(t *testing.T, body []byte, code int, serial string) *DeviceStatistics {
	t.Helper()
	res := classifyDevstat(body, code, nil, serial)
	switch res.kind {
	case devstatResPresent:
		c, e := res.complete, res.exit
		return &DeviceStatistics{Status: devstatPresent, Complete: &c, ExitStatus: &e, LogicalBlockSize: res.lbs, Pages: res.pages}
	case devstatResAbsent:
		return &DeviceStatistics{Status: devstatAbsent}
	}
	return &DeviceStatistics{Status: devstatUnavailable, Reason: res.kind}
}

func vol(b uint64, src string) *Volume { return &Volume{Bytes: b, Source: src} }
func attrVol(b uint64, id int, name string) *Volume {
	return &Volume{Bytes: b, Source: "ata_attribute", AttributeID: id, AttributeName: name}
}

// attrsDoc builds a minimal -a output with the given attributes.
func attrsDoc(model string, inDB bool, lbs int, poh any, attrs ...[3]any) []byte {
	var table []any
	for _, a := range attrs {
		table = append(table, map[string]any{"id": a[0], "name": a[1], "raw": map[string]any{"value": a[2]}})
	}
	doc := map[string]any{
		"device":               map[string]any{"protocol": "ATA"},
		"model_name":           model,
		"serial_number":        "FIXTURE-synthetic",
		"in_smartctl_database": inDB,
		"ata_smart_attributes": map[string]any{"table": table},
	}
	if lbs != 0 {
		doc["logical_block_size"] = lbs
	}
	if poh != nil {
		doc["power_on_time"] = map[string]any{"hours": poh}
	}
	b, _ := json.Marshal(doc)
	return b
}

var absentDS = &DeviceStatistics{Status: devstatAbsent}

// Data Written / Data Read from the corpus fixtures and the synthetic cases
// of plan 7.5 / 7.7.
func TestDeriveVolumes(t *testing.T) {
	ds := func(name, serial string) *DeviceStatistics {
		return devstatOf(t, fixture(t, name, "devstat"), 0, serial)
	}
	hgstDS := ds("ata_devstat_hgst", "FIXTURE-m01-sdc")
	hgst4Kn := devstatOf(t, edit(t, fixture(t, "ata_devstat_hgst", "devstat"), func(d map[string]any) { d["logical_block_size"] = 4096 }), 0, "FIXTURE-m01-sdc")
	hgstOdd := devstatOf(t, edit(t, fixture(t, "ata_devstat_hgst", "devstat"), func(d map[string]any) { d["logical_block_size"] = 520 }), 0, "FIXTURE-m01-sdc")
	partialNoP1 := devstatOf(t, edit(t, fixture(t, "ata_devstat_wdc_unc", "devstat"), func(d map[string]any) {
		pages := d["ata_device_statistics"].(map[string]any)["pages"].([]any)
		var keep []any
		for _, p := range pages {
			if p.(map[string]any)["number"].(float64) != 1 {
				keep = append(keep, p)
			}
		}
		d["ata_device_statistics"].(map[string]any)["pages"] = keep
	}), 4, "FIXTURE-m01-sdl")
	invalidP1 := devstatOf(t, edit(t, fixture(t, "ata_devstat_hgst", "devstat"), func(d map[string]any) {
		p1 := d["ata_device_statistics"].(map[string]any)["pages"].([]any)[0].(map[string]any)
		for _, e := range p1["table"].([]any) {
			em := e.(map[string]any)
			em["flags"].(map[string]any)["valid"] = false
		}
	}), 0, "FIXTURE-m01-sdc")
	valueS := devstatOf(t, edit(t, fixture(t, "ata_devstat_hgst", "devstat"), func(d map[string]any) {
		p1 := d["ata_device_statistics"].(map[string]any)["pages"].([]any)[0].(map[string]any)
		for _, e := range p1["table"].([]any) {
			em := e.(map[string]any)
			if em["offset"].(float64) == 24 {
				em["value_s"] = "1000"
			}
		}
	}), 0, "FIXTURE-m01-sdc")

	samsung850 := fixture(t, "ata_no_devstat_samsung850", "a")
	nvme := fixture(t, "nvme_sabrent", "a")

	tests := []struct {
		name               string
		main               []byte
		ds                 *DeviceStatistics
		writes, reads      *Volume
		wOmitted, rOmitted string
	}{
		{"m01-sdc HGST: 163.66 / 184.22 TB", fixture(t, "ata_devstat_hgst", "a"), hgstDS,
			vol(163663499752448, "ata_device_statistics"), vol(184215110001152, "ata_device_statistics"), "", ""},
		{"m01-sdd Seagate: devstat over 241", fixture(t, "ata_devstat_seagate", "a"), ds("ata_devstat_seagate", "FIXTURE-m01-sdd"),
			vol(133447789406208, "ata_device_statistics"), vol(335193745236480, "ata_device_statistics"), "", ""},
		{"m01-sda Samsung SSD", fixture(t, "ata_devstat_samsung_ssd", "a"), ds("ata_devstat_samsung_ssd", "FIXTURE-m01-sda"),
			vol(49705310966784, "ata_device_statistics"), vol(242620523722240, "ata_device_statistics"), "", ""},
		{"m01-sdl WDC", fixture(t, "ata_devstat_wdc_unc", "a"), ds("ata_devstat_wdc_unc", "FIXTURE-m01-sdl"),
			vol(107830377472000, "ata_device_statistics"), vol(791804967834112, "ata_device_statistics"), "", ""},
		{"m02-sdd HGST, POH entry missing", fixture(t, "ata_devstat_hgst_nopoh", "a"), ds("ata_devstat_hgst_nopoh", "FIXTURE-m02-sdd"),
			vol(175072186337280, "ata_device_statistics"), vol(865368690893824, "ata_device_statistics"), "", ""},
		{"m02-sda Samsung 850: absent, 105.96 TB from 241", samsung850, ds("ata_no_devstat_samsung850", "FIXTURE-m02-sda"),
			attrVol(105959765530112, 241, "Total_LBAs_Written"), nil, "", omitNoSource},
		{"m02-nvme0n1 Sabrent: 206.18 TB", nvme, &DeviceStatistics{Status: devstatNotApplicable},
			vol(206177714688000, "nvme"), vol(294347536384000, "nvme"), "", ""},
		{"NVMe under D10 off still nvme", nvme, &DeviceStatistics{Status: devstatOff, Reason: "config"},
			vol(206177714688000, "nvme"), vol(294347536384000, "nvme"), "", ""},
		{"SCSI view: no source", fixture(t, "scsi_view", "a"), &DeviceStatistics{Status: devstatNotApplicable},
			nil, nil, omitNoSource, omitNoSource},
		{"SK hynix + 235: 59.06 GB, NAND 241 never", fixture(t, "ata_skhynix", "a"), absentDS,
			attrVol(59055800320, 235, "Lifetime_Writes_GiB"), attrVol(3190086959104, 242, "Lifetime_Reads_GiB"), "", ""},
		{"SK hynix SC311: Total_Writes_GB never", fixture(t, "ata_skhynix_sc311", "a"), absentDS,
			nil, nil, omitNoSource, omitNoSource},
		{"D10 off: vendor fallback", samsung850, &DeviceStatistics{Status: devstatOff, Reason: "config"},
			attrVol(105959765530112, 241, "Total_LBAs_Written"), nil, "", omitNoSource},
		{"macOS: vendor fallback", samsung850, &DeviceStatistics{Status: devstatOff, Reason: "os"},
			attrVol(105959765530112, 241, "Total_LBAs_Written"), nil, "", omitNoSource},
		{"unavailable: omitted, HA holds", fixture(t, "ata_devstat_hgst", "a"), &DeviceStatistics{Status: devstatUnavailable, Reason: "stopped"},
			nil, nil, omitDevstatUnavailable, omitDevstatUnavailable},
		{"unavailable never falls back to 241", samsung850, &DeviceStatistics{Status: devstatUnavailable, Reason: "failed"},
			nil, nil, omitDevstatUnavailable, omitDevstatUnavailable},
		{"4Kn devstat", fixture(t, "ata_devstat_hgst", "a"), hgst4Kn,
			vol(1309307998019584, "ata_device_statistics"), vol(359795136721*4096, "ata_device_statistics"), "", ""},
		{"odd block size", fixture(t, "ata_devstat_hgst", "a"), hgstOdd,
			nil, nil, omitBlockSize, omitBlockSize},
		{"partial without P1", fixture(t, "ata_devstat_wdc_unc", "a"), partialNoP1,
			nil, nil, omitEntryInvalid, omitEntryInvalid},
		{"P1 entries invalid", fixture(t, "ata_devstat_hgst", "a"), invalidP1,
			nil, nil, omitEntryInvalid, omitEntryInvalid},
		{"value_s preferred", fixture(t, "ata_devstat_hgst", "a"), valueS,
			vol(512000, "ata_device_statistics"), vol(184215110001152, "ata_device_statistics"), "", ""},
		{"4Kn without devstat: sectors refused", edit(t, samsung850, func(d map[string]any) { d["logical_block_size"] = 4096 }), absentDS,
			nil, nil, omitBlockSize, omitNoSource},
		{"not in database", edit(t, samsung850, func(d map[string]any) { d["in_smartctl_database"] = false }), absentDS,
			nil, nil, omitNotInDatabase, omitNoSource},
		{"KIOXIA EXCERIA drops the Lifetime pair",
			attrsDoc("KIOXIA-EXCERIA SATA SSD", true, 512, 5000, [3]any{241, "Lifetime_Writes_GiB", 100}, [3]any{242, "Lifetime_Reads_GiB", 100}), absentDS,
			nil, nil, omitExcludedModel, omitExcludedModel},
		{"KIOXIA EXCERIA keeps sectors",
			attrsDoc("KIOXIA-EXCERIA SATA SSD 480GB", true, 512, 5000, [3]any{241, "Lifetime_Writes_GiB", 100}, [3]any{246, "Total_LBAs_Written", 1000}), absentDS,
			attrVol(512000, 246, "Total_LBAs_Written"), nil, "", omitNoSource},
		{"Intel 225 + 233: named class wins, sectors ignored",
			attrsDoc("INTEL SSDSC2BB480G7R", true, 512, 30000,
				[3]any{225, "Host_Writes_32MiB", 1000}, [3]any{233, "Media_Wearout_Indicator", 98},
				[3]any{241, "Total_LBAs_Written", 999999999}, [3]any{249, "NAND_Writes_1GiB", 77}), absentDS,
			attrVol(1000*33554432, 225, "Host_Writes_32MiB"), nil, "", omitNoSource},
		{"same class within 1 %: 241 preferred",
			attrsDoc("Dell S3520", true, 512, 30000, [3]any{225, "Host_Writes_32MiB", 1000}, [3]any{241, "Host_Writes_32MiB", 1005}), absentDS,
			attrVol(1005*33554432, 241, "Host_Writes_32MiB"), nil, "", omitNoSource},
		{"same class disagrees",
			attrsDoc("Dell S3520", true, 512, 30000, [3]any{225, "Host_Writes_32MiB", 1000}, [3]any{241, "Host_Writes_32MiB", 2000}), absentDS,
			nil, nil, omitCandidatesDisagree, omitNoSource},
		{"lowest ID when 241 absent",
			attrsDoc("x", true, 512, 30000, [3]any{246, "Total_Writes_GiB", 10}, [3]any{233, "Host_Writes_GiB", 10}), absentDS,
			attrVol(10<<30, 233, "Host_Writes_GiB"), nil, "", omitNoSource},
		{"POH 0", attrsDoc("x", true, 512, 0, [3]any{241, "Host_Writes_GiB", 10}), absentDS,
			nil, nil, omitPOHUnknown, omitNoSource},
		{"POH below 24", attrsDoc("x", true, 512, 23, [3]any{241, "Host_Writes_GiB", 10}), absentDS,
			nil, nil, omitPOHUnknown, omitNoSource},
		{"POH missing", attrsDoc("x", true, 512, nil, [3]any{241, "Host_Writes_GiB", 10}), absentDS,
			nil, nil, omitPOHUnknown, omitNoSource},
		{"rate bound", attrsDoc("x", true, 512, 24, [3]any{241, "Host_Writes_GiB", 100000}), absentDS,
			nil, nil, omitRateBound, omitNoSource},
		{"rate bound edge passes", attrsDoc("x", true, 512, 24, [3]any{241, "Host_Writes_GiB", 96000}), absentDS,
			attrVol(96000<<30, 241, "Host_Writes_GiB"), nil, "", omitNoSource},
		{"raw at 2^48 refused", attrsDoc("x", true, 512, 30000, [3]any{241, "Total_LBAs_Written", uint64(1) << 48}), absentDS,
			nil, nil, omitEntryInvalid, omitNoSource},
		{"overflow", attrsDoc("x", true, 512, 30000, [3]any{241, "Host_Writes_GiB", uint64(1)<<48 - 1}), absentDS,
			nil, nil, omitOverflow, omitNoSource},
		{"unit-less and NAND names never read",
			attrsDoc("x", true, 512, 30000, [3]any{241, "Host_Writes", 10}, [3]any{242, "Host_Reads", 10},
				[3]any{243, "Total_LBAs_Written_Low", 10}, [3]any{244, "Host_Writes_MiB", 10}, [3]any{249, "NAND_Writes_1GiB", 10},
				[3]any{250, "Total_Writes_GB", 10}), absentDS,
			nil, nil, omitNoSource, omitNoSource},
		{"NVMe _s preferred",
			[]byte(`{"device":{"protocol":"NVMe"},"nvme_smart_health_information_log":{"data_units_written":1,"data_units_written_s":"2","data_units_read":3}}`),
			&DeviceStatistics{Status: devstatNotApplicable}, vol(1024000, "nvme"), vol(1536000, "nvme"), "", ""},
		{"NVMe overflow",
			[]byte(`{"device":{"protocol":"NVMe"},"nvme_smart_health_information_log":{"data_units_written_s":"36893488147419103232","data_units_read":1152921504606846976}}`),
			&DeviceStatistics{Status: devstatNotApplicable}, nil, nil, omitOverflow, omitOverflow},
		{"NVMe without the log", []byte(`{"device":{"protocol":"NVMe"}}`),
			&DeviceStatistics{Status: devstatNotApplicable}, nil, nil, omitNoSource, omitNoSource},
		{"unparseable main output", []byte(`garbage`), &DeviceStatistics{Status: devstatNotApplicable},
			nil, nil, omitNoSource, omitNoSource},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := deriveVolumes(parseMainDoc(tc.main), tc.ds)
			want := &Derived{HostWrites: tc.writes, HostReads: tc.reads, HostWritesOmitted: tc.wOmitted, HostReadsOmitted: tc.rOmitted}
			if !reflect.DeepEqual(got, want) {
				g, _ := json.Marshal(got)
				w, _ := json.Marshal(want)
				t.Errorf("derived\n got %s\nwant %s", g, w)
			}
		})
	}
}

// The published JSON shapes of plan 7.3.
func TestDevstatJSONShape(t *testing.T) {
	c, e := true, 0
	got, _ := json.Marshal(DriveInfo{
		DeviceStatistics: &DeviceStatistics{Status: devstatPresent, Complete: &c, ExitStatus: &e, LogicalBlockSize: 512, Pages: json.RawMessage(`[]`)},
		Derived:          &Derived{HostWrites: vol(1, "ata_device_statistics"), HostReadsOmitted: omitNoSource},
	})
	for _, want := range []string{
		`"device_statistics":{"status":"present","complete":true,"exit_status":0,"logical_block_size":512,"pages":[]}`,
		`"derived":{"host_writes":{"bytes":1,"source":"ata_device_statistics"},"host_reads_omitted":"no_source"}`,
	} {
		if !strings.Contains(string(got), want) {
			t.Errorf("missing %s in\n%s", want, got)
		}
	}
	got, _ = json.Marshal(DriveInfo{DeviceStatistics: &DeviceStatistics{Status: devstatUnavailable, Reason: "stopped"}})
	if !strings.Contains(string(got), `"device_statistics":{"status":"unavailable","reason":"stopped"}`) {
		t.Errorf("unavailable shape: %s", got)
	}
	// A v0.7.0-shaped entry (no devstat at all) carries neither field.
	got, _ = json.Marshal(DriveInfo{RawJSON: json.RawMessage(`{}`)})
	if strings.Contains(string(got), "device_statistics") || strings.Contains(string(got), "derived") {
		t.Errorf("empty fields not omitted: %s", got)
	}
}

// D10: the config key and the flag (plan 4.4).
func TestDeviceStatisticsConfig(t *testing.T) {
	if !(&Config{}).DeviceStatisticsEnabled() {
		t.Error("default must be on")
	}
	f, tr := false, true
	if (&Config{DeviceStatistics: &f}).DeviceStatisticsEnabled() {
		t.Error("device_statistics: false must turn it off")
	}
	if !(&Config{DeviceStatistics: &tr}).DeviceStatisticsEnabled() {
		t.Error("device_statistics: true must leave it on")
	}

	load := func(t *testing.T, yaml string, args ...string) *Config {
		t.Helper()
		dir := t.TempDir()
		path := filepath.Join(dir, "config.yaml")
		if err := os.WriteFile(path, []byte(yaml), 0o644); err != nil {
			t.Fatal(err)
		}
		savedArgs, savedFlags := os.Args, flag.CommandLine
		t.Cleanup(func() { os.Args, flag.CommandLine = savedArgs, savedFlags })
		flag.CommandLine = flag.NewFlagSet("smartha-agent", flag.ContinueOnError)
		os.Args = append([]string{"smartha-agent", "--config", path}, args...)
		cfg, err := LoadConfig()
		if err != nil {
			t.Fatal(err)
		}
		return cfg
	}
	if !load(t, "port: 9099\n").DeviceStatisticsEnabled() {
		t.Error("unset key must be on")
	}
	if load(t, "device_statistics: false\n").DeviceStatisticsEnabled() {
		t.Error("config false must be off")
	}
	if !load(t, "device_statistics: true\n").DeviceStatisticsEnabled() {
		t.Error("config true must be on")
	}
	if load(t, "device_statistics: true\n", "--no-device-statistics").DeviceStatisticsEnabled() {
		t.Error("--no-device-statistics must win over the file")
	}

	// The example config documents the key, commented out.
	b, err := os.ReadFile("config.yaml.example")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), "\n# device_statistics: true\n") {
		t.Error("config.yaml.example lacks the commented device_statistics line")
	}
}

// Every Go source file in the agent is gofmt-clean (the CI line the plan
// asks for lives in .github, outside this change; this test holds the line).
func TestSourcesAreGofmted(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range files {
		src, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		formatted, err := format.Source(src)
		if err != nil {
			t.Errorf("%s: %v", f, err)
			continue
		}
		if string(formatted) != string(src) {
			t.Errorf("%s is not gofmt-clean", f)
		}
	}
}
