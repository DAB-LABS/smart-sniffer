package main

import (
	"encoding/json"
	"strings"
	"testing"
)

// v1.0 (J11): an unavailable filesystem has null sizes, not 0; a read one
// keeps its numbers, 0 included.
func TestUnavailableFilesystemSizesAreNull(t *testing.T) {
	down := FilesystemInfo{ID: "fs-1", Mountpoint: "/srv", FSType: "ext4", Status: "unavailable"}
	b, err := json.Marshal(down)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	json.Unmarshal(b, &got)
	for _, k := range []string{"total_bytes", "used_bytes", "available_bytes", "use_percent"} {
		v, ok := got[k]
		if !ok || v != nil {
			t.Errorf("%s = %#v (present %v), want null", k, v, ok)
		}
	}
	if got["status"] != "unavailable" || got["fstype"] != "ext4" || got["id"] != "fs-1" {
		t.Errorf("other fields: %s", b)
	}
	if strings.Count(string(b), `"total_bytes"`) != 1 {
		t.Errorf("total_bytes twice: %s", b)
	}

	up := FilesystemInfo{ID: "fs-2", Status: "ok", TotalBytes: 100, UsedBytes: 0, AvailableBytes: 100}
	b, _ = json.Marshal(up)
	if !strings.Contains(string(b), `"total_bytes":100,"used_bytes":0,"available_bytes":100,"use_percent":0,`) {
		t.Errorf("ok filesystem: %s", b)
	}
	// In a list, as /api/filesystems sends it.
	b, _ = json.Marshal([]FilesystemInfo{down, up})
	if !strings.Contains(string(b), `"use_percent":null`) || !strings.Contains(string(b), `"use_percent":0`) {
		t.Errorf("list: %s", b)
	}
}

// v1.0 (J10): colliding ids. The first in the list keeps the plain id (the
// one Home Assistant has always followed); later ones get a stable suffix
// from their mountpoint.
func TestFilesystemIDCollisions(t *testing.T) {
	mk := func() []FilesystemInfo {
		return []FilesystemInfo{
			{ID: makeFilesystemID("1d000004-aaaa", "/"), Mountpoint: "/"},
			{ID: makeFilesystemID("1d000004-aaaa", "/home"), Mountpoint: "/home"},
			{ID: makeFilesystemID("2e000005-bbbb", "/data"), Mountpoint: "/data"},
			{ID: makeFilesystemID("1d000004-aaaa", "/srv/bind"), Mountpoint: "/srv/bind"},
			{ID: makeFilesystemID("1d000004-aaaa", "/home"), Mountpoint: "/home"},
		}
	}
	a := mk()
	uniqueFilesystemIDs(a)
	if a[0].ID != "fs-1d000004" || a[2].ID != "fs-2e000005" {
		t.Fatalf("plain ids changed: %v %v", a[0].ID, a[2].ID)
	}
	seen := map[string]bool{}
	for _, f := range a {
		if seen[f.ID] {
			t.Fatalf("duplicate id %s in %+v", f.ID, a)
		}
		seen[f.ID] = true
	}
	if !strings.HasPrefix(a[1].ID, "fs-1d000004-") || len(a[1].ID) != len("fs-1d000004-")+6 {
		t.Errorf("suffixed id %q", a[1].ID)
	}
	if a[4].ID != a[1].ID+"-2" {
		t.Errorf("same mountpoint twice: %q then %q", a[1].ID, a[4].ID)
	}
	// Stable: the same every run.
	b := mk()
	uniqueFilesystemIDs(b)
	for i := range a {
		if a[i].ID != b[i].ID {
			t.Errorf("run to run: %q != %q", a[i].ID, b[i].ID)
		}
	}
	// No collision: nothing changes.
	c := []FilesystemInfo{{ID: "fs-1"}, {ID: "fs-2"}}
	uniqueFilesystemIDs(c)
	if c[0].ID != "fs-1" || c[1].ID != "fs-2" {
		t.Errorf("%+v", c)
	}
}
