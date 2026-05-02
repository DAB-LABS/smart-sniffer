//go:build !windows

package main

import (
	"errors"
	"os"
	"strings"
	"testing"
)

// Real `btrfs filesystem usage --raw` output captured from David's
// ZimaOS box (Brookdale NAS, /dev/md0). Kept inline rather than read
// from disk so the test is hermetic. Mirrors
// docs/internal/research/test-fixtures/zimaos-btrfs-usage.txt.
const fixtureBtrfsUsageRaw = `Overall:
    Device size:                2000263643136
    Device allocated:           1968050339840
    Device unallocated:           32213303296
    Device missing:                        0
    Device slack:                          0
    Used:                       1225996673024
    Free (estimated):            769020321792      (min: 752913670144)
    Free (statfs, df):           769019273216
    Data ratio:                         1.00
    Metadata ratio:                     2.00
    Global reserve:                536870912      (used: 0)
    Multiple profiles:                    no

Data,single: Size:1959599865856, Used:1222792847360 (62.40%)
   /dev/md0      1959599865856

Metadata,DUP: Size:4216848384, Used:1601634304 (37.98%)
   /dev/md0         8433696768

System,DUP: Size:8388608, Used:278528 (3.32%)
   /dev/md0           16777216

Unallocated:
   /dev/md0         32213303296
`

func TestParseBtrfsUsageRaw_RealFixture(t *testing.T) {
	usage, err := parseBtrfsUsageRaw([]byte(fixtureBtrfsUsageRaw))
	if err != nil {
		t.Fatalf("expected success, got: %v", err)
	}

	const (
		wantTotal     = uint64(2000263643136)
		wantUsed      = uint64(1225996673024)
		wantAvailable = uint64(769020321792)
	)
	if usage.Total != wantTotal {
		t.Errorf("Total = %d, want %d", usage.Total, wantTotal)
	}
	if usage.Used != wantUsed {
		t.Errorf("Used = %d, want %d", usage.Used, wantUsed)
	}
	if usage.Available != wantAvailable {
		t.Errorf("Available = %d, want %d", usage.Available, wantAvailable)
	}
}

// Regression: the per-block-group lines have "Used:" mid-line (e.g.
// "Data,single: Size:N, Used:N (62.40%)"). The Overall: parser must
// only match the bare-line Used:, not these.
func TestParseBtrfsUsageRaw_InlineUsedRegression(t *testing.T) {
	// Strip the Overall: block to verify the parser does NOT pick up
	// the inline Used field as a substitute.
	overallEnd := strings.Index(fixtureBtrfsUsageRaw, "\nData,single:")
	if overallEnd < 0 {
		t.Fatal("test fixture malformed: missing Data,single section marker")
	}
	withoutOverall := fixtureBtrfsUsageRaw[overallEnd:]

	_, err := parseBtrfsUsageRaw([]byte(withoutOverall))
	if err == nil {
		t.Fatal("expected parse error when Overall: block is missing, got nil")
	}
	if !errors.Is(err, errBtrfsParse) {
		t.Errorf("expected errBtrfsParse, got %v", err)
	}
}

func TestParseBtrfsUsageRaw_MissingDeviceSize(t *testing.T) {
	input := `Overall:
    Used:                       1225996673024
`
	_, err := parseBtrfsUsageRaw([]byte(input))
	if !errors.Is(err, errBtrfsParse) {
		t.Errorf("expected errBtrfsParse, got %v", err)
	}
}

func TestParseBtrfsUsageRaw_MissingUsed(t *testing.T) {
	input := `Overall:
    Device size:                2000263643136
`
	_, err := parseBtrfsUsageRaw([]byte(input))
	if !errors.Is(err, errBtrfsParse) {
		t.Errorf("expected errBtrfsParse, got %v", err)
	}
}

func TestParseBtrfsUsageRaw_EmptyInput(t *testing.T) {
	_, err := parseBtrfsUsageRaw([]byte(""))
	if !errors.Is(err, errBtrfsParse) {
		t.Errorf("expected errBtrfsParse, got %v", err)
	}
}

func TestParseBtrfsUsageRaw_NonNumericTotal(t *testing.T) {
	input := `Overall:
    Device size:                NOTANUMBER
    Used:                       1225996673024
`
	// Regex requires \d+, so a non-numeric value won't even match the
	// capture group -- this tests that path through the error.
	_, err := parseBtrfsUsageRaw([]byte(input))
	if !errors.Is(err, errBtrfsParse) {
		t.Errorf("expected errBtrfsParse, got %v", err)
	}
}

// Available falls back to Total - Used when Free (estimated) is missing.
func TestParseBtrfsUsageRaw_AvailableFallback(t *testing.T) {
	input := `Overall:
    Device size:                1000
    Used:                       300
`
	usage, err := parseBtrfsUsageRaw([]byte(input))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if usage.Total != 1000 {
		t.Errorf("Total = %d, want 1000", usage.Total)
	}
	if usage.Used != 300 {
		t.Errorf("Used = %d, want 300", usage.Used)
	}
	if usage.Available != 700 {
		t.Errorf("Available = %d, want 700 (Total - Used fallback)", usage.Available)
	}
}

// errBtrfsProgsMissing is returned when btrfs is not on PATH. We
// simulate this by setting PATH to a directory we know doesn't have
// btrfs. Skip if the test environment doesn't allow PATH manipulation
// (very rare but possible).
func TestTryBtrfsFallback_BinaryMissing(t *testing.T) {
	origPath := os.Getenv("PATH")
	t.Cleanup(func() { os.Setenv("PATH", origPath) })

	// Empty PATH guarantees exec.LookPath fails for "btrfs". We don't
	// need /tmp to be free of a btrfs binary -- empty PATH is enough.
	if err := os.Setenv("PATH", ""); err != nil {
		t.Skipf("cannot set PATH for test: %v", err)
	}

	_, err := tryBtrfsFallback("/")
	if !errors.Is(err, errBtrfsProgsMissing) {
		t.Errorf("expected errBtrfsProgsMissing, got %v", err)
	}
}

// Note on timeout testing: the timeout path requires a btrfs binary
// that hangs longer than 5s. Constructing this hermetically would
// require a test double that injects a fake runner via a package-level
// hook. The current implementation uses exec.LookPath + exec.CommandContext
// directly for clarity; if timeout flakiness is reported in production
// we can refactor to inject a runner. For now the timeout sentinel is
// covered by code review of the ctx.Err() == context.DeadlineExceeded
// branch in tryBtrfsFallback.
