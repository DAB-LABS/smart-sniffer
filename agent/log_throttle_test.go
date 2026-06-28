package main

import (
	"testing"
	"time"
)

// TestLogThrottleSuppression covers the core dedup contract: first occurrence
// logs, an identical discriminator within the interval is suppressed, a changed
// discriminator logs immediately, and once the interval elapses the same
// discriminator logs again as a reminder.
func TestLogThrottleSuppression(t *testing.T) {
	lt := newLogThrottle(time.Hour, false)

	if !lt.shouldLog("dev", "code 4") {
		t.Fatal("first occurrence should log")
	}
	if lt.shouldLog("dev", "code 4") {
		t.Fatal("identical message within interval should be suppressed")
	}
	if !lt.shouldLog("dev", "code 8") {
		t.Fatal("changed message should log immediately")
	}
	if lt.shouldLog("dev", "code 8") {
		t.Fatal("identical message within interval should be suppressed after a change")
	}

	// Force the interval to elapse by backdating the stored timestamp.
	lt.mu.Lock()
	e := lt.states["dev"]
	e.at = time.Now().Add(-2 * time.Hour)
	lt.states["dev"] = e
	lt.mu.Unlock()

	if !lt.shouldLog("dev", "code 8") {
		t.Fatal("same message after the interval should log again as a reminder")
	}
}

// TestLogThrottleVerbose verifies that a verbose throttle never suppresses.
func TestLogThrottleVerbose(t *testing.T) {
	lt := newLogThrottle(time.Hour, true)
	for i := 0; i < 5; i++ {
		if !lt.shouldLog("dev", "unchanging") {
			t.Fatalf("verbose throttle should always log (iteration %d)", i)
		}
	}
}

// TestLogThrottleKeysIndependent verifies that different keys do not share
// suppression state.
func TestLogThrottleKeysIndependent(t *testing.T) {
	lt := newLogThrottle(time.Hour, false)
	if !lt.shouldLog("scan", "boom") {
		t.Fatal("first log for key 'scan' should fire")
	}
	if !lt.shouldLog("scan-json", "boom") {
		t.Fatal("a different key with the same message should log independently")
	}
}

// TestLogThrottleStableDiscriminator reproduces the non-ExitError exec-error
// path: the logged line carries variable error detail, but the discriminator
// passed to shouldLog is a stable token, so repeats are suppressed regardless
// of how the underlying error text changes between polls.
func TestLogThrottleStableDiscriminator(t *testing.T) {
	lt := newLogThrottle(time.Hour, false)

	if !lt.shouldLog("/dev/sda", "exec-error") {
		t.Fatal("first exec-error should log")
	}
	// Same device, same stable token, even though the real error text differs.
	if lt.shouldLog("/dev/sda", "exec-error") {
		t.Fatal("persistent exec-error should be suppressed despite varying detail")
	}

	// A non-zero exit code on the same device uses the rendered message as the
	// discriminator, which differs from the exec-error token, so the transition
	// re-logs.
	if !lt.shouldLog("/dev/sda", "ERROR: smartctl -a /dev/sda failed (exit code 2: ...)") {
		t.Fatal("transition from exec-error to an exit code should re-log")
	}
}
