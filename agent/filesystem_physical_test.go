//go:build !windows

package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"
)

// All disk names, serials and models below are invented.

func plistDict(kv string) []byte {
	return []byte(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>` + kv + `</dict>
</plist>`)
}

// fakeDisks is a diskutil stand-in: BSD name -> plist body. It counts runs.
type fakeDisks struct {
	disks map[string]string
	runs  int
}

func (f *fakeDisks) run(name string, args []string, _ time.Duration) ([]byte, error) {
	f.runs++
	if name != "diskutil" || len(args) != 3 || args[0] != "info" || args[1] != "-plist" {
		return nil, fmt.Errorf("unexpected command %s %v", name, args)
	}
	kv, ok := f.disks[args[2]]
	if !ok {
		return nil, errors.New("exit status 1")
	}
	return plistDict(kv), nil
}

// A Mac with an internal SSD (disk0, APFS container disk3) and a USB disk
// (disk4, HFS+ on disk4s2).
func fakeMac() *fakeDisks {
	return &fakeDisks{disks: map[string]string{
		"disk3s5": `<key>APFSContainerReference</key><string>disk3</string>
<key>APFSPhysicalStores</key><array><dict><key>APFSPhysicalStore</key><string>disk0s2</string></dict></array>
<key>DeviceIdentifier</key><string>disk3s5</string>
<key>ParentWholeDisk</key><string>disk3</string>
<key>WholeDisk</key><false/>
<key>Size</key><integer>494384795648</integer>`,
		"disk3s1s1": `<key>APFSContainerReference</key><string>disk3</string>
<key>DeviceIdentifier</key><string>disk3s1s1</string>
<key>ParentWholeDisk</key><string>disk3</string>`,
		"disk3": `<key>APFSPhysicalStores</key><array><dict><key>APFSPhysicalStore</key><string>disk0s2</string></dict></array>
<key>DeviceIdentifier</key><string>disk3</string>
<key>WholeDisk</key><true/>`,
		"disk0s2": `<key>DeviceIdentifier</key><string>disk0s2</string>
<key>ParentWholeDisk</key><string>disk0</string>`,
		"disk0": `<key>DeviceIdentifier</key><string>disk0</string>
<key>MediaName</key><string>FAKEVENDOR SSD FV0512</string>
<key>ParentWholeDisk</key><string>disk0</string>
<key>WholeDisk</key><true/>`,
		"disk4s2": `<key>DeviceIdentifier</key><string>disk4s2</string>
<key>FilesystemType</key><string>hfs</string>
<key>ParentWholeDisk</key><string>disk4</string>`,
		"disk4": `<key>DeviceIdentifier</key><string>disk4</string>
<key>MediaName</key><string>Fake  USB Disk</string>`,
	}}
}

const fakeIOService = "IOService:/FakeRoot/fake-io@0/FakeNVMeController/NS_01@1"

// fakeSmartctl answers smartctl -i for whole disks: disk -> serial.
type fakeSmartctl struct {
	serials map[string]string
	calls   []string
}

func (f *fakeSmartctl) run(_ string, args []string, _ time.Duration) ([]byte, int, error) {
	dev := args[len(args)-1]
	f.calls = append(f.calls, strings.Join(args, " "))
	if s, ok := f.serials[dev]; ok && len(args) == 5 { // answers only with -d nvme
		return []byte(fmt.Sprintf(`{"serial_number":%q}`, s)), 0, nil
	}
	return []byte(`{"smartctl":{"exit_status":2}}`), 2, nil
}

func darwinCache(disks *fakeDisks, smart *fakeSmartctl, drives []driveRef) *FilesystemCache {
	fc := NewFilesystemCache(nil, "", false)
	fc.goos = "darwin"
	fc.run = disks.run
	fc.smartctlPath = "/fake/smartctl"
	if smart != nil {
		fc.smartRun = smart.run
	} else {
		fc.smartRun = nil
	}
	fc.drives = func() []driveRef { return drives }
	return fc
}

func link(fc *FilesystemCache, mount, dev string) FilesystemInfo {
	info := FilesystemInfo{Mountpoint: mount, Device: dev}
	fc.linkPhysical(&info, "")
	return info
}

func TestLinkAPFSVolumeBySerial(t *testing.T) {
	captureLog(t)
	drives := []driveRef{{ID: "fake-sn-0001", DevicePath: fakeIOService, Model: "FAKEVENDOR SSD FV0512", Serial: "FAKE-SN-0001"}}
	disks := fakeMac()
	smart := &fakeSmartctl{serials: map[string]string{"/dev/disk0": "FAKE-SN-0001"}}
	fc := darwinCache(disks, smart, drives)

	info := link(fc, "/System/Volumes/Data", "/dev/disk3s5")
	if !reflect.DeepEqual(info.PhysicalDevices, []string{"/dev/disk0"}) || info.DriveID != "fake-sn-0001" {
		t.Fatalf("got %v %q", info.PhysicalDevices, info.DriveID)
	}
	if len(smart.calls) != 2 || !strings.Contains(smart.calls[1], "-d nvme") {
		t.Errorf("smartctl calls %v, want plain then -d nvme", smart.calls)
	}

	// Next poll: nothing runs again.
	runs, calls := disks.runs, len(smart.calls)
	info = link(fc, "/System/Volumes/Data", "/dev/disk3s5")
	if info.DriveID != "fake-sn-0001" || disks.runs != runs || len(smart.calls) != calls {
		t.Errorf("second poll ran diskutil %d->%d, smartctl %d->%d", runs, disks.runs, calls, len(smart.calls))
	}
}

func TestLinkByModelWhenSmartctlCannotRead(t *testing.T) {
	captureLog(t)
	drives := []driveRef{
		{ID: "fake-sn-0001", DevicePath: fakeIOService, Model: "FAKEVENDOR SSD FV0512", Serial: "FAKE-SN-0001"},
		{ID: "fake-sn-0002", DevicePath: "/dev/disk9", Model: "Other Model", Serial: "FAKE-SN-0002"},
	}
	fc := darwinCache(fakeMac(), &fakeSmartctl{}, drives)
	if info := link(fc, "/System/Volumes/Data", "/dev/disk3s5"); info.DriveID != "fake-sn-0001" {
		t.Errorf("drive_id %q, want the only drive of that model", info.DriveID)
	}
}

func TestLinkTwoDrivesOfOneModelNeedsTheSerial(t *testing.T) {
	captureLog(t)
	drives := []driveRef{
		{ID: "fake-a", DevicePath: fakeIOService, Model: "FAKEVENDOR SSD FV0512", Serial: "FAKE-A"},
		{ID: "fake-b", DevicePath: "IOService:/Fake/other", Model: "FAKEVENDOR SSD FV0512", Serial: "FAKE-B"},
	}
	fc := darwinCache(fakeMac(), nil, drives)
	info := link(fc, "/System/Volumes/Data", "/dev/disk3s5")
	if info.DriveID != "" || !reflect.DeepEqual(info.PhysicalDevices, []string{"/dev/disk0"}) {
		t.Errorf("got %v %q, want the disk but no drive", info.PhysicalDevices, info.DriveID)
	}
}

func TestLinkByDevicePathAndNonAPFS(t *testing.T) {
	captureLog(t)
	drives := []driveRef{{ID: "fake-usb", DevicePath: "/dev/disk4", Model: "x", Serial: "FAKE-USB"}}
	fc := darwinCache(fakeMac(), nil, drives)
	info := link(fc, "/Volumes/FakeBackup", "/dev/disk4s2")
	if !reflect.DeepEqual(info.PhysicalDevices, []string{"/dev/disk4"}) || info.DriveID != "fake-usb" {
		t.Errorf("got %v %q", info.PhysicalDevices, info.DriveID)
	}
}

func TestLinkSnapshotAsksTheContainer(t *testing.T) {
	captureLog(t)
	drives := []driveRef{{ID: "fake-sn-0001", DevicePath: "/dev/disk0", Serial: "FAKE-SN-0001"}}
	fc := darwinCache(fakeMac(), nil, drives)
	// The root snapshot names no store itself; its container does.
	info := link(fc, "/", "/dev/disk3s1s1")
	if !reflect.DeepEqual(info.PhysicalDevices, []string{"/dev/disk0"}) || info.DriveID != "fake-sn-0001" {
		t.Errorf("got %v %q", info.PhysicalDevices, info.DriveID)
	}
}

func TestLinkFusionHasNoSingleDrive(t *testing.T) {
	captureLog(t)
	disks := &fakeDisks{disks: map[string]string{
		"disk5s1": `<key>APFSContainerReference</key><string>disk5</string>
<key>APFSPhysicalStores</key><array>
<dict><key>APFSPhysicalStore</key><string>disk0s2</string></dict>
<dict><key>APFSPhysicalStore</key><string>disk1s2</string></dict></array>`,
	}}
	drives := []driveRef{
		{ID: "fake-ssd", DevicePath: "/dev/disk0"},
		{ID: "fake-hdd", DevicePath: "/dev/disk1"},
	}
	fc := darwinCache(disks, nil, drives)
	// The stores' own lookups fail here, so their whole disks come from
	// their names.
	info := link(fc, "/", "/dev/disk5s1")
	if !reflect.DeepEqual(info.PhysicalDevices, []string{"/dev/disk0", "/dev/disk1"}) || info.DriveID != "" {
		t.Errorf("got %v %q", info.PhysicalDevices, info.DriveID)
	}
}

func TestLinkFailureIsRetriedOnlyAfterAWhile(t *testing.T) {
	logs := captureLog(t)
	disks := &fakeDisks{disks: map[string]string{}}
	fc := darwinCache(disks, nil, nil)
	info := link(fc, "/Volumes/Gone", "/dev/disk7s1")
	if info.PhysicalDevices != nil || info.DriveID != "" {
		t.Errorf("got %v %q on a failed lookup", info.PhysicalDevices, info.DriveID)
	}
	if !strings.Contains(logs.String(), "cannot find the drive under /dev/disk7s1") {
		t.Errorf("no log: %q", logs.String())
	}
	runs := disks.runs
	link(fc, "/Volumes/Gone", "/dev/disk7s1")
	if disks.runs != runs {
		t.Error("failed lookup retried on the next poll")
	}
	fc.phys["disk7s1"].at = time.Now().Add(-physRetry - time.Minute)
	link(fc, "/Volumes/Gone", "/dev/disk7s1")
	if disks.runs == runs {
		t.Error("failed lookup never retried")
	}
}

func TestLinkOnlyOnDarwin(t *testing.T) {
	disks := fakeMac()
	fc := darwinCache(disks, nil, nil)
	fc.goos = "linux"
	info := link(fc, "/System/Volumes/Data", "/dev/disk3s5")
	if info.PhysicalDevices != nil || info.DriveID != "" || disks.runs != 0 {
		t.Errorf("linked off darwin: %v %q runs=%d", info.PhysicalDevices, info.DriveID, disks.runs)
	}
	fc.goos = "darwin"
	if info := link(fc, "/Volumes/nas", "//fake@nas/share"); info.PhysicalDevices != nil || disks.runs != 0 {
		t.Error("a network mount was looked up")
	}
}

func TestFilesystemJSONCarriesTheDriveLink(t *testing.T) {
	captureLog(t)
	dir := t.TempDir()
	drives := []driveRef{{ID: "fake-sn-0001", DevicePath: fakeIOService, Model: "FAKEVENDOR SSD FV0512", Serial: "FAKE-SN-0001"}}
	fc := darwinCache(fakeMac(), &fakeSmartctl{serials: map[string]string{"/dev/disk0": "FAKE-SN-0001"}}, drives)
	fc.configs = []FilesystemConfig{{Path: dir, Device: "/dev/disk3s5", FSType: "apfs"}}
	fc.Refresh()

	rec := httptest.NewRecorder()
	fc.HandleFilesystems(rec, httptest.NewRequest("GET", "/api/filesystems", nil))
	var out []map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if len(out) != 1 || out[0]["drive_id"] != "fake-sn-0001" ||
		!reflect.DeepEqual(out[0]["physical_devices"], []any{"/dev/disk0"}) || out[0]["device"] != "/dev/disk3s5" {
		t.Errorf("got %v", out)
	}

	// Off macOS the fields are absent.
	plain := NewFilesystemCache([]FilesystemConfig{{Path: dir, Device: "/dev/sda1"}}, "", false)
	plain.goos = "linux"
	plain.Refresh()
	rec = httptest.NewRecorder()
	plain.HandleFilesystems(rec, httptest.NewRequest("GET", "/api/filesystems", nil))
	if s := rec.Body.String(); strings.Contains(s, "drive_id") || strings.Contains(s, "physical_devices") {
		t.Errorf("linux output carries the link: %s", s)
	}
}

func TestParsePlist(t *testing.T) {
	v, err := parsePlist(plistDict(`<key>A</key><string> x </string><key>B</key><integer>42</integer>
<key>C</key><true/><key>D</key><false/><key>E</key><array><string>p</string><dict><key>F</key><real>1.5</real></dict></array>
<key>G</key><data>AAEC</data><key>H</key><integer>99999999999999999999</integer>`))
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]any{
		"A": "x", "B": int64(42), "C": true, "D": false,
		"E": []any{"p", map[string]any{"F": 1.5}},
		"G": "AAEC", "H": "99999999999999999999",
	}
	if !reflect.DeepEqual(v, want) {
		t.Errorf("got %#v", v)
	}
	if _, err := parsePlist([]byte("")); err == nil {
		t.Error("empty input parsed")
	}
}
