// Physical drive of a macOS filesystem.
//
// On macOS a filesystem is reported on a device such as /dev/disk3s5: an
// APFS volume inside a container disk that macOS synthesizes, so the device
// name says nothing about the drive it lives on. Drives, meanwhile, come
// from smartctl --scan as IOService paths (or /dev/diskN on some Macs). The
// Control Center and the integration could not place such a filesystem on
// its drive.
//
// For each filesystem on macOS this works out the whole physical disk(s)
// under it and the /api/drives id of the drive that is that disk:
//
//  1. `diskutil info -plist <device>`. An APFS volume lists its container's
//     physical stores under APFSPhysicalStores (e.g. disk0s2); if the
//     volume itself does not, its container (APFSContainerReference) is
//     asked. Anything else is its own store.
//  2. Each store's whole disk is its ParentWholeDisk (disk0s2 -> disk0).
//  3. The whole disk is matched to a drive: a drive whose device path is
//     that disk; else the drive whose serial is the one `smartctl -i` reads
//     from the disk; else the only drive whose model is the disk's
//     MediaName.
//
// The diskutil and smartctl answers are cached per device and per disk, so
// they run once per filesystem (again only when the device behind a mount
// changes, or an hour after a failure). Matching against the drive list is
// done every poll from memory. Only on darwin; other platforms never get
// here.
package main

import (
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"log"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// diskutilTimeout bounds one diskutil run; smartctlInfoTimeout one smartctl
// -i run on a whole disk.
const (
	diskutilTimeout     = 10 * time.Second
	smartctlInfoTimeout = 15 * time.Second
)

// driveRef is what physical linking needs from one /api/drives entry.
type driveRef struct {
	ID, DevicePath, Model, Serial string
}

// physEntry caches where one filesystem device lives.
type physEntry struct {
	whole []string          // whole physical disks, e.g. ["/dev/disk0"]
	media map[string]string // whole disk -> diskutil MediaName
	err   error
	at    time.Time
}

// serialEntry caches the serial smartctl reads from one whole disk.
type serialEntry struct {
	serial string
	at     time.Time
}

// physRetry is how long a failed lookup is kept before it is tried again.
const physRetry = time.Hour

// diskRe matches a BSD disk name and captures its whole disk: disk3s5 ->
// disk3, disk3s1s1 -> disk3.
var diskRe = regexp.MustCompile(`^(?:/dev/)?r?(disk[0-9]+)(s[0-9]+)*$`)

// bsdName strips /dev/ and the raw r prefix: /dev/rdisk0s2 -> disk0s2.
func bsdName(dev string) string {
	dev = strings.TrimPrefix(dev, "/dev/")
	if strings.HasPrefix(dev, "rdisk") {
		dev = dev[1:]
	}
	return dev
}

// linkPhysical fills PhysicalDevices and DriveID for one filesystem. source
// is the device behind the mount (from statfs, else the configured device).
// It does nothing off darwin or for a source that is not a /dev/disk device.
func (fc *FilesystemCache) linkPhysical(info *FilesystemInfo, source string) {
	if fc.goos != "darwin" || fc.run == nil {
		return
	}
	if source == "" {
		source = info.Device
	}
	if !diskRe.MatchString(source) {
		return
	}
	entry := fc.physFor(source)
	if entry.err != nil || len(entry.whole) == 0 {
		return
	}
	info.PhysicalDevices = entry.whole

	var drives []driveRef
	if fc.drives != nil {
		drives = fc.drives()
	}
	id := ""
	for _, whole := range entry.whole {
		got := fc.driveFor(whole, entry.media[whole], drives)
		if got == "" || (id != "" && got != id) {
			// A disk with no drive, or a filesystem across two drives
			// (Fusion): no single drive to place it on.
			id = ""
			break
		}
		id = got
	}
	info.DriveID = id

	msg := fmt.Sprintf("filesystem %s: on %s, drive %s", info.Mountpoint, strings.Join(entry.whole, ", "), orNone(id))
	if fc.logs != nil && fc.logs.shouldLog("physical:"+info.Mountpoint, msg) {
		log.Print(msg)
	}
}

func orNone(s string) string {
	if s == "" {
		return "not matched"
	}
	return s
}

// physFor returns the cached physical disks for a device, looking them up
// when there is no entry or a failed one is due a retry.
func (fc *FilesystemCache) physFor(source string) *physEntry {
	key := bsdName(source)
	if e, ok := fc.phys[key]; ok && (e.err == nil || time.Since(e.at) < physRetry) {
		return e
	}
	whole, media, err := resolvePhysicalDisks(fc.run, key)
	e := &physEntry{whole: whole, media: media, err: err, at: time.Now()}
	if fc.phys == nil {
		fc.phys = make(map[string]*physEntry)
	}
	fc.phys[key] = e
	if err != nil && fc.logs != nil {
		if msg := fmt.Sprintf("filesystem: cannot find the drive under /dev/%s: %v", key, err); fc.logs.shouldLog("physical-err:"+key, msg) {
			log.Print(msg)
		}
	}
	return e
}

// resolvePhysicalDisks asks diskutil for the whole physical disks under a
// device and each one's MediaName.
func resolvePhysicalDisks(run cmdRunner, dev string) ([]string, map[string]string, error) {
	vol, err := diskutilInfo(run, dev)
	if err != nil {
		return nil, nil, err
	}
	stores := apfsPhysicalStores(vol)
	if len(stores) == 0 {
		if c := plistString(vol, "APFSContainerReference"); c != "" && c != dev {
			if cont, err := diskutilInfo(run, c); err == nil {
				stores = apfsPhysicalStores(cont)
			}
		}
	}
	if len(stores) == 0 {
		if plistString(vol, "APFSContainerReference") != "" {
			return nil, nil, errors.New("diskutil names no APFS physical store")
		}
		stores = []string{dev} // not APFS: the device is on a physical disk itself
	}

	var whole []string
	media := map[string]string{}
	for _, store := range stores {
		store = bsdName(store)
		parent := ""
		if store == dev {
			parent = plistString(vol, "ParentWholeDisk")
		} else if info, err := diskutilInfo(run, store); err == nil {
			parent = plistString(info, "ParentWholeDisk")
		}
		if parent == "" {
			if m := diskRe.FindStringSubmatch(store); m != nil {
				parent = m[1]
			}
		}
		if parent == "" {
			return nil, nil, fmt.Errorf("no whole disk for %s", store)
		}
		path := "/dev/" + parent
		if _, seen := media[path]; seen {
			continue
		}
		name := ""
		if info, err := diskutilInfo(run, parent); err == nil {
			name = plistString(info, "MediaName")
		}
		media[path] = name
		whole = append(whole, path)
	}
	return whole, media, nil
}

// apfsPhysicalStores reads APFSPhysicalStores: an array of dicts, each with
// an APFSPhysicalStore name.
func apfsPhysicalStores(info map[string]any) []string {
	arr, _ := info["APFSPhysicalStores"].([]any)
	var out []string
	for _, item := range arr {
		switch v := item.(type) {
		case map[string]any:
			if s, _ := v["APFSPhysicalStore"].(string); s != "" {
				out = append(out, s)
			}
		case string:
			if v != "" {
				out = append(out, v)
			}
		}
	}
	return out
}

func plistString(m map[string]any, key string) string {
	s, _ := m[key].(string)
	return strings.TrimSpace(s)
}

// diskutilInfo runs `diskutil info -plist <dev>` and decodes the top dict.
func diskutilInfo(run cmdRunner, dev string) (map[string]any, error) {
	out, err := run("diskutil", []string{"info", "-plist", bsdName(dev)}, diskutilTimeout)
	if err != nil {
		return nil, fmt.Errorf("diskutil info %s: %w", bsdName(dev), err)
	}
	v, err := parsePlist(out)
	if err != nil {
		return nil, fmt.Errorf("diskutil info %s: %w", bsdName(dev), err)
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("diskutil info %s: not a dict", bsdName(dev))
	}
	return m, nil
}

// driveFor finds the /api/drives id of the drive that is whole disk path,
// or "".
func (fc *FilesystemCache) driveFor(whole, media string, drives []driveRef) string {
	if len(drives) == 0 {
		return ""
	}
	name := bsdName(whole)
	for _, d := range drives {
		if bsdName(d.DevicePath) == name {
			return d.ID
		}
	}
	if serial := fc.serialFor(whole); serial != "" {
		for _, d := range drives {
			if strings.EqualFold(strings.TrimSpace(d.Serial), serial) {
				return d.ID
			}
		}
	}
	if media = normSpace(media); media != "" {
		match := ""
		for _, d := range drives {
			if strings.EqualFold(normSpace(d.Model), media) {
				if match != "" {
					return "" // two drives of that model: cannot tell which
				}
				match = d.ID
			}
		}
		return match
	}
	return ""
}

func normSpace(s string) string { return strings.Join(strings.Fields(s), " ") }

// serialFor reads the serial of a whole disk with smartctl -i, once (an
// empty answer is tried again after physRetry). Tries plain, then -d nvme.
func (fc *FilesystemCache) serialFor(whole string) string {
	if fc.smartRun == nil || fc.smartctlPath == "" {
		return ""
	}
	if e, ok := fc.serials[whole]; ok && (e.serial != "" || time.Since(e.at) < physRetry) {
		return e.serial
	}
	serial := ""
	for _, args := range [][]string{
		{"-i", "--json", whole},
		{"-i", "--json", "-d", "nvme", whole},
	} {
		out, _, err := fc.smartRun(fc.smartctlPath, args, smartctlInfoTimeout)
		if err != nil {
			continue
		}
		var parsed struct {
			Serial string `json:"serial_number"`
		}
		if json.Unmarshal(out, &parsed) == nil && strings.TrimSpace(parsed.Serial) != "" {
			serial = strings.TrimSpace(parsed.Serial)
			break
		}
	}
	if fc.serials == nil {
		fc.serials = make(map[string]*serialEntry)
	}
	fc.serials[whole] = &serialEntry{serial: serial, at: time.Now()}
	return serial
}

// ---------------------------------------------------------------------------
// Minimal XML property list decoder
// ---------------------------------------------------------------------------

// parsePlist decodes an XML property list into map[string]any, []any,
// string, int64, float64 and bool. <data> and <date> come back as their
// text. Enough for diskutil; not a general plist library.
func parsePlist(b []byte) (any, error) {
	dec := xml.NewDecoder(strings.NewReader(string(b)))
	dec.Strict = false
	for {
		tok, err := dec.Token()
		if err != nil {
			if err == io.EOF {
				return nil, errors.New("empty property list")
			}
			return nil, err
		}
		if se, ok := tok.(xml.StartElement); ok && se.Name.Local != "plist" {
			return plistValue(dec, se)
		}
	}
}

func plistValue(dec *xml.Decoder, se xml.StartElement) (any, error) {
	switch se.Name.Local {
	case "dict":
		m := map[string]any{}
		key := ""
		for {
			tok, err := dec.Token()
			if err != nil {
				return nil, err
			}
			switch t := tok.(type) {
			case xml.StartElement:
				if t.Name.Local == "key" {
					var k string
					if err := dec.DecodeElement(&k, &t); err != nil {
						return nil, err
					}
					key = k
					continue
				}
				v, err := plistValue(dec, t)
				if err != nil {
					return nil, err
				}
				m[key] = v
			case xml.EndElement:
				return m, nil
			}
		}
	case "array":
		var arr []any
		for {
			tok, err := dec.Token()
			if err != nil {
				return nil, err
			}
			switch t := tok.(type) {
			case xml.StartElement:
				v, err := plistValue(dec, t)
				if err != nil {
					return nil, err
				}
				arr = append(arr, v)
			case xml.EndElement:
				return arr, nil
			}
		}
	case "true", "false":
		if err := dec.Skip(); err != nil {
			return nil, err
		}
		return se.Name.Local == "true", nil
	default:
		var text string
		if err := dec.DecodeElement(&text, &se); err != nil {
			return nil, err
		}
		text = strings.TrimSpace(text)
		switch se.Name.Local {
		case "integer":
			if n, err := strconv.ParseInt(text, 10, 64); err == nil {
				return n, nil
			}
		case "real":
			if f, err := strconv.ParseFloat(text, 64); err == nil {
				return f, nil
			}
		}
		return text, nil
	}
}
