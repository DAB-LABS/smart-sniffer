// smart_available.go -- does a drive that answers smartctl give SMART data?
//
// A drive can answer smartctl -a with its model and serial and exit 0, yet
// carry no SMART data at all. On macOS this is what a USB drive looks like
// when the third-party SAT driver that passes SMART through a USB bridge has
// stopped working: smartctl reports smart_support.enabled false and nothing
// else (GH #61). A USB enclosure that never passes SMART through looks the
// same. The drive is still read and published exactly as before; the agent
// only says, per drive, whether SMART data was there, and logs when that
// changes.
package main

import (
	"encoding/json"
	"log"
)

// Values of DriveInfo.SmartUnavailableReason.
const (
	smartReasonNotSupported = "not_supported" // smart_support.available is false
	smartReasonDisabled     = "disabled"      // smart_support.enabled is false
	smartReasonNoData       = "no_data"       // no SMART status, attributes or NVMe health log
)

// smartReasonText is the wording of each reason in the log.
var smartReasonText = map[string]string{
	smartReasonNotSupported: "smartctl says SMART is not supported",
	smartReasonDisabled:     "smartctl says SMART is disabled",
	smartReasonNoData:       "the output has no SMART status or attributes",
}

// smartAvailability reports whether an accepted -a output carries SMART data
// and, when it does not, why. It is unavailable when any of these holds:
//
//   - smart_support.available is false
//   - smart_support.enabled is false
//   - there is no smart_status.passed, no ata_smart_attributes.table entry
//     and no nvme_smart_health_information_log
//
// The last rule is the integration's own test for usable data
// (_has_usable_smart_data in attention.py), so a drive the agent calls
// available is one the integration can evaluate. An output that is not a
// JSON object counts as no data.
func smartAvailability(out []byte) (bool, string) {
	var parsed struct {
		SmartSupport *struct {
			Available *bool `json:"available"`
			Enabled   *bool `json:"enabled"`
		} `json:"smart_support"`
		SmartStatus *struct {
			Passed *bool `json:"passed"`
		} `json:"smart_status"`
		ATAAttributes *struct {
			Table []json.RawMessage `json:"table"`
		} `json:"ata_smart_attributes"`
		NVMeLog map[string]json.RawMessage `json:"nvme_smart_health_information_log"`
	}
	if err := json.Unmarshal(out, &parsed); err != nil {
		return false, smartReasonNoData
	}
	if s := parsed.SmartSupport; s != nil {
		if s.Available != nil && !*s.Available {
			return false, smartReasonNotSupported
		}
		if s.Enabled != nil && !*s.Enabled {
			return false, smartReasonDisabled
		}
	}
	hasStatus := parsed.SmartStatus != nil && parsed.SmartStatus.Passed != nil
	hasATA := parsed.ATAAttributes != nil && len(parsed.ATAAttributes.Table) > 0
	hasNVMe := len(parsed.NVMeLog) > 0
	if !hasStatus && !hasATA && !hasNVMe {
		return false, smartReasonNoData
	}
	return true, ""
}

// smartMem is what the agent remembers about a drive's SMART data for the
// run, keyed by drive id. Touched only by Refresh, never persisted.
type smartMem struct {
	available bool // SMART data was there at the last read
	seen      bool // SMART data was there at some read this run
}

// noteSmartAvailability sets the SMART availability fields on a freshly read
// drive and logs a change: once when a drive that gave SMART data stops
// giving it, and once when SMART data appears for a drive that had none.
// Nothing is logged at a drive's first read, so a USB enclosure that never
// passes SMART through stays as quiet as before.
func (dc *DriveCache) noteSmartAvailability(info *DriveInfo) {
	avail, reason := smartAvailability(info.RawJSON)
	info.SmartAvailable = &avail
	info.SmartUnavailableReason = reason

	m := dc.smartMemory[info.ID]
	switch {
	case m == nil:
		m = &smartMem{}
		dc.smartMemory[info.ID] = m
	case m.available && !avail:
		log.Printf("WARNING: drive %s (%s) still answers smartctl but stopped giving SMART data: %s. "+
			"A USB bridge or the driver that passes SMART through it may have stopped working.",
			info.DevicePath, info.Model, smartReasonText[reason])
	case !m.available && avail && m.seen:
		log.Printf("drive %s (%s) is giving SMART data again", info.DevicePath, info.Model)
	case !m.available && avail:
		log.Printf("drive %s (%s) is giving SMART data now", info.DevicePath, info.Model)
	}
	m.available = avail
	m.seen = m.seen || avail
	info.SmartLost = !avail && m.seen
}
