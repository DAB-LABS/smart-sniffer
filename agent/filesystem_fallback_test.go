//go:build !windows

package main

import (
	"testing"
	"time"
)

// TestApplyFilesystemFallback verifies the dispatcher: a registered fallback
// whose fstype matches (case-insensitively) and whose trigger fires overrides
// the byte fields, and a non-matching fstype is left untouched.
func TestApplyFilesystemFallback(t *testing.T) {
	saved := fsFallbacks
	defer func() { fsFallbacks = saved }()

	called := false
	fsFallbacks = []fsFallback{
		{
			fstype:  "faketest",
			trigger: triggerAlways,
			resolve: func(string) (fsUsage, error) {
				called = true
				return fsUsage{Total: 300, Used: 100, Available: 200}, nil
			},
		},
	}

	fc := &FilesystemCache{logs: newLogThrottle(time.Hour, false)}

	// Matching fstype (case-insensitive): bytes overridden in place.
	info := FilesystemInfo{TotalBytes: 1, UsedBytes: 1, AvailableBytes: 0}
	fc.applyFilesystemFallback(&info, "FakeTest", "/fake")
	if !called {
		t.Fatal("resolve was not called for a matching fstype")
	}
	if info.TotalBytes != 300 || info.UsedBytes != 100 || info.AvailableBytes != 200 {
		t.Fatalf("bytes not overridden by fallback: %+v", info)
	}

	// Non-matching fstype: resolve must not run and info is unchanged.
	called = false
	other := FilesystemInfo{TotalBytes: 42, UsedBytes: 7, AvailableBytes: 35}
	fc.applyFilesystemFallback(&other, "ext4", "/x")
	if called {
		t.Fatal("resolve should not be called for a non-matching fstype")
	}
	if other.TotalBytes != 42 || other.UsedBytes != 7 || other.AvailableBytes != 35 {
		t.Fatalf("non-matching fstype altered info: %+v", other)
	}
}

// TestTriggerHelpers covers the two trigger styles.
func TestTriggerHelpers(t *testing.T) {
	if !triggerTotalZero(FilesystemInfo{TotalBytes: 0}) {
		t.Error("triggerTotalZero should fire when total == 0")
	}
	if triggerTotalZero(FilesystemInfo{TotalBytes: 1}) {
		t.Error("triggerTotalZero should not fire when total != 0")
	}
	if !triggerAlways(FilesystemInfo{TotalBytes: 999}) {
		t.Error("triggerAlways should always fire")
	}
}

// TestRegistryWiring is a guard so the real registry keeps the expected
// fstypes wired to resolvers (catches an accidental drop during refactors).
func TestRegistryWiring(t *testing.T) {
	want := map[string]bool{"btrfs": false, "zfs": false}
	for _, fb := range fsFallbacks {
		if _, ok := want[fb.fstype]; ok {
			if fb.resolve == nil || fb.trigger == nil {
				t.Errorf("registry entry %q missing trigger or resolve", fb.fstype)
			}
			want[fb.fstype] = true
		}
	}
	for fstype, seen := range want {
		if !seen {
			t.Errorf("registry missing expected fstype %q", fstype)
		}
	}
}
