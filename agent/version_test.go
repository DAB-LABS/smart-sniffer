package main

import (
	"encoding/json"
	"net/http/httptest"
	"slices"
	"testing"
)

func TestNormaliseVersion(t *testing.T) {
	for _, tc := range []struct{ in, want string }{
		{"0.8.2", "0.8.2"},
		{"v0.8.2", "0.8.2"},
		{"V0.8.2", "0.8.2"},
		{"vv0.8.2", "0.8.2"},
		{" v0.8.2\n", "0.8.2"},
		{"v0.8.2-31-g4439849-dirty", "0.8.2-31-g4439849-dirty"},
		{"0.8.3-pretest6", "0.8.3-pretest6"},
		{"4439849", "4439849"},
		{"dev", "dev"},
		{"vdev", "vdev"},
		{"v", "v"},
		{"", ""},
	} {
		if got := normaliseVersion(tc.in); got != tc.want {
			t.Errorf("normaliseVersion(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

// The default needs no change, so init leaves it as it is.
func TestBuiltInVersionIsNormalised(t *testing.T) {
	if version != normaliseVersion(version) {
		t.Errorf("version %q was not normalised at startup", version)
	}
}

// /api/health and the mDNS TXT record carry the normalised value.
func TestVersionInHealthAndTXT(t *testing.T) {
	saved := version
	t.Cleanup(func() { version = saved })
	version = normaliseVersion("v0.8.2-31-g4439849-dirty")

	rec := httptest.NewRecorder()
	handleHealth(NewDriveCache(&Config{}))(rec, httptest.NewRequest("GET", "/api/health", nil))
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body["version"] != "0.8.2-31-g4439849-dirty" {
		t.Errorf("health version = %v", body["version"])
	}

	txt := buildTXT("host", "0", 1, "", "")
	if !slices.Contains(txt, "version=0.8.2-31-g4439849-dirty") {
		t.Errorf("TXT = %v", txt)
	}
}
