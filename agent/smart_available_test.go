package main

import (
	"encoding/json"
	"testing"
)

func TestSmartAvailability(t *testing.T) {
	cases := []struct {
		name   string
		out    string
		avail  bool
		reason string
	}{
		{"ATA with status and attributes",
			`{"smart_support":{"available":true,"enabled":true},"smart_status":{"passed":true},"ata_smart_attributes":{"table":[{"id":5}]}}`,
			true, ""},
		{"NVMe health log only",
			`{"nvme_smart_health_information_log":{"critical_warning":0}}`, true, ""},
		{"status only", `{"smart_status":{"passed":false}}`, true, ""},
		{"attributes only", `{"ata_smart_attributes":{"table":[{"id":9}]}}`, true, ""},
		// GH #61: what a USB drive on macOS returns once the SAT driver
		// stops passing SMART through. Exit 0, identity, nothing else.
		{"SMART disabled, identity only",
			`{"model_name":"FAKE MODEL","serial_number":"FAKE0001","smart_support":{"available":true,"enabled":false}}`,
			false, smartReasonDisabled},
		{"SMART not supported",
			`{"smart_support":{"available":false}}`, false, smartReasonNotSupported},
		{"not supported wins over disabled",
			`{"smart_support":{"available":false,"enabled":false}}`, false, smartReasonNotSupported},
		{"no smart_support and no data",
			`{"model_name":"FAKE MODEL","serial_number":"FAKE0002"}`, false, smartReasonNoData},
		{"supported and enabled but nothing read",
			`{"smart_support":{"available":true,"enabled":true}}`, false, smartReasonNoData},
		{"empty attribute table and empty NVMe log",
			`{"ata_smart_attributes":{"table":[]},"nvme_smart_health_information_log":{}}`, false, smartReasonNoData},
		{"status without passed", `{"smart_status":{}}`, false, smartReasonNoData},
		{"not JSON", `not json`, false, smartReasonNoData},
		{"empty", ``, false, smartReasonNoData},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			avail, reason := smartAvailability([]byte(tc.out))
			if avail != tc.avail || reason != tc.reason {
				t.Errorf("got (%v, %q), want (%v, %q)", avail, reason, tc.avail, tc.reason)
			}
		})
	}
}

// The new fields are additive: a drive with SMART data carries
// smart_available true and nothing else new; one without carries the
// reason, and smart_lost only once it had SMART data before.
func TestSmartAvailabilityJSONFields(t *testing.T) {
	yes, no := true, false
	cases := []struct {
		name string
		info DriveInfo
		want map[string]interface{}
		gone []string
	}{
		{"available", DriveInfo{SmartAvailable: &yes},
			map[string]interface{}{"smart_available": true},
			[]string{"smart_unavailable_reason", "smart_lost"}},
		{"never had", DriveInfo{SmartAvailable: &no, SmartUnavailableReason: smartReasonNoData},
			map[string]interface{}{"smart_available": false, "smart_unavailable_reason": "no_data"},
			[]string{"smart_lost"}},
		{"lost", DriveInfo{SmartAvailable: &no, SmartUnavailableReason: smartReasonDisabled, SmartLost: true},
			map[string]interface{}{"smart_available": false, "smart_unavailable_reason": "disabled", "smart_lost": true},
			nil},
		{"unset", DriveInfo{}, map[string]interface{}{},
			[]string{"smart_available", "smart_unavailable_reason", "smart_lost"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tc.info.RawJSON = json.RawMessage(`{}`)
			b, err := json.Marshal(tc.info)
			if err != nil {
				t.Fatal(err)
			}
			var got map[string]interface{}
			if err := json.Unmarshal(b, &got); err != nil {
				t.Fatal(err)
			}
			for k, v := range tc.want {
				if got[k] != v {
					t.Errorf("%s = %v, want %v (%s)", k, got[k], v, b)
				}
			}
			for _, k := range tc.gone {
				if _, ok := got[k]; ok {
					t.Errorf("%s present: %s", k, b)
				}
			}
			// The fields every existing consumer reads are still there.
			for _, k := range []string{"id", "device_path", "model", "serial", "protocol", "readable", "smart_data"} {
				if _, ok := got[k]; !ok {
					t.Errorf("%s missing: %s", k, b)
				}
			}
		})
	}
}
