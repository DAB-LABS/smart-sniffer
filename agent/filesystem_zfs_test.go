//go:build !windows

package main

import (
	"errors"
	"testing"
)

// TestParseZFSList_PoolRoot uses nsleigh's zfs-pool figures from Issue #31
// (used 2364680580, available 5302507132). This is the clean validation
// anchor: /zfs-pool resolves to the pool root, and pvesm reports 30.84% for
// it. Total is derived as used + available, and the agent's percent block
// (used/(used+available)) must then land on ~30.8%.
func TestParseZFSList_PoolRoot(t *testing.T) {
	// `zfs list -Hp -o used,available /zfs-pool` output (note trailing newline).
	out := []byte("2364680580\t5302507132\n")

	got, err := parseZFSList(out)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := fsUsage{Total: 7667187712, Used: 2364680580, Available: 5302507132}
	if got != want {
		t.Fatalf("parseZFSList = %+v, want %+v", got, want)
	}

	// Sanity-check the percentage the dispatcher's percent block will compute.
	pct := float64(got.Used) / float64(got.Total) * 100
	if pct < 30.7 || pct > 31.0 {
		t.Errorf("derived percent = %.2f, expected ~30.84 (Issue #31)", pct)
	}
}

// TestParseZFSList_ChildDataset checks a second realistic line (nsleigh's
// local-zfs figures: used 130719428, available 818639392 -> ~13.77%).
func TestParseZFSList_ChildDataset(t *testing.T) {
	out := []byte("130719428\t818639392\n")

	got, err := parseZFSList(out)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := fsUsage{Total: 949358820, Used: 130719428, Available: 818639392}
	if got != want {
		t.Fatalf("parseZFSList = %+v, want %+v", got, want)
	}
}

// TestParseZFSList_NoTrailingNewline verifies output without a trailing
// newline parses identically.
func TestParseZFSList_NoTrailingNewline(t *testing.T) {
	got, err := parseZFSList([]byte("100\t200"))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != (fsUsage{Total: 300, Used: 100, Available: 200}) {
		t.Fatalf("parseZFSList = %+v, want Total 300/Used 100/Available 200", got)
	}
}

// TestParseZFSList_Empty maps empty output to errFSParse.
func TestParseZFSList_Empty(t *testing.T) {
	if _, err := parseZFSList([]byte("  \n")); !errors.Is(err, errFSParse) {
		t.Errorf("expected errFSParse for empty output, got %v", err)
	}
}

// TestParseZFSList_MissingField maps a single-column line to errFSParse.
func TestParseZFSList_MissingField(t *testing.T) {
	if _, err := parseZFSList([]byte("12345\n")); !errors.Is(err, errFSParse) {
		t.Errorf("expected errFSParse for missing available field, got %v", err)
	}
}

// TestParseZFSList_NonNumeric maps a non-numeric value to errFSParse.
func TestParseZFSList_NonNumeric(t *testing.T) {
	if _, err := parseZFSList([]byte("12345\tnope\n")); !errors.Is(err, errFSParse) {
		t.Errorf("expected errFSParse for non-numeric available, got %v", err)
	}
}
