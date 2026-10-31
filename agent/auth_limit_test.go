package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// fakeClock is a settable time source for the limiter.
type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time          { return c.t }
func (c *fakeClock) advance(d time.Duration) { c.t = c.t.Add(d) }

func testLimiter() (*authLimiter, *fakeClock) {
	clock := &fakeClock{t: time.Date(2026, 10, 31, 12, 0, 0, 0, time.UTC)}
	l := newAuthLimiter()
	l.now = clock.now
	return l, clock
}

const testPassword = "sneaky-cipher-otter-4821"

func call(h http.Handler, ip, path, password string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, path, nil)
	req.RemoteAddr = ip + ":51234"
	if password != "" {
		req.Header.Set("Authorization", "Bearer "+password)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func limitedHandler() (http.Handler, *authLimiter, *fakeClock) {
	l, clock := testLimiter()
	ok := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	return authMiddlewareWith(testPassword, l, ok), l, clock
}

func TestFiveWrongPasswordsThen429(t *testing.T) {
	h, _, _ := limitedHandler()
	for i := 1; i <= authFailLimit; i++ {
		if got := call(h, "10.0.0.5", "/api/drives", "wrong").Code; got != http.StatusUnauthorized {
			t.Fatalf("attempt %d: got %d, want 401", i, got)
		}
	}
	rec := call(h, "10.0.0.5", "/api/drives", "wrong")
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("after %d failures: got %d, want 429", authFailLimit, rec.Code)
	}
	if ra := rec.Header().Get("Retry-After"); ra != "1" {
		t.Errorf("Retry-After = %q, want 1", ra)
	}
	if body := rec.Body.String(); body != `{"error":"too_many_attempts"}` {
		t.Errorf("body = %s", body)
	}
}

func TestWhileBlockedEvenTheRightPasswordWaits(t *testing.T) {
	// The block is not an oracle: a correct guess during it cannot be told
	// from a wrong one.
	h, _, _ := limitedHandler()
	for i := 0; i < authFailLimit; i++ {
		call(h, "10.0.0.5", "/api/drives", "wrong")
	}
	if got := call(h, "10.0.0.5", "/api/drives", testPassword).Code; got != http.StatusTooManyRequests {
		t.Fatalf("right password while blocked: got %d, want 429", got)
	}
}

func TestBackoffDoublesUpToTheMax(t *testing.T) {
	l, clock := testLimiter()
	for i := 0; i < authFailLimit; i++ {
		l.fail("10.0.0.5")
	}
	want := authBlockStart
	for step := 0; step < 10; step++ {
		blocked, left := l.blocked("10.0.0.5")
		if !blocked || left != want {
			t.Fatalf("step %d: blocked=%v left=%v, want %v", step, blocked, left, want)
		}
		clock.advance(left) // the block ends
		if blocked, _ := l.blocked("10.0.0.5"); blocked {
			t.Fatalf("step %d: still blocked after the block ended", step)
		}
		l.fail("10.0.0.5") // the next guess is wrong too
		want *= 2
		if want > authBlockMax {
			want = authBlockMax
		}
	}
	if want != authBlockMax {
		t.Fatalf("backoff never reached the max (%v)", want)
	}
}

func TestRetryAfterCountsDown(t *testing.T) {
	h, l, clock := limitedHandler()
	for i := 0; i < authFailLimit; i++ {
		l.fail("10.0.0.5")
	}
	clock.advance(authBlockStart)
	l.fail("10.0.0.5") // 2 s block
	clock.advance(500 * time.Millisecond)
	if ra := call(h, "10.0.0.5", "/api/drives", "x").Header().Get("Retry-After"); ra != "2" {
		t.Errorf("Retry-After = %q, want 2 (1.5 s rounded up)", ra)
	}
}

func TestASuccessResetsTheIP(t *testing.T) {
	h, l, _ := limitedHandler()
	for i := 0; i < authFailLimit-1; i++ {
		call(h, "10.0.0.5", "/api/drives", "wrong")
	}
	if got := call(h, "10.0.0.5", "/api/drives", testPassword).Code; got != http.StatusOK {
		t.Fatalf("right password: got %d", got)
	}
	if l.tracked() != 0 {
		t.Fatalf("a success left %d tracked IPs", l.tracked())
	}
	for i := 0; i < authFailLimit-1; i++ {
		if got := call(h, "10.0.0.5", "/api/drives", "wrong").Code; got != http.StatusUnauthorized {
			t.Fatalf("count did not restart: attempt %d got %d", i+1, got)
		}
	}
}

func TestTheWindowExpires(t *testing.T) {
	l, clock := testLimiter()
	for i := 0; i < authFailLimit-1; i++ {
		l.fail("10.0.0.5")
	}
	clock.advance(authFailWindow)
	l.fail("10.0.0.5") // a fresh count: 1, not 5
	if blocked, _ := l.blocked("10.0.0.5"); blocked {
		t.Fatal("failures older than the window still counted")
	}
	// A blocked IP is forgotten a window after its block ends, not before.
	for i := 0; i < authFailLimit; i++ {
		l.fail("10.0.0.6")
	}
	clock.advance(authBlockStart + authFailWindow - time.Millisecond)
	if l.records["10.0.0.6"] == nil {
		t.Fatal("forgotten before the window after its block ended")
	}
	clock.advance(time.Millisecond)
	if blocked, _ := l.blocked("10.0.0.6"); blocked || l.records["10.0.0.6"] != nil {
		t.Fatal("not forgotten a window after its block ended")
	}
}

func TestOtherIPsAreNotSlowed(t *testing.T) {
	h, _, _ := limitedHandler()
	for i := 0; i < authFailLimit+3; i++ {
		call(h, "10.0.0.5", "/api/drives", "wrong")
	}
	if got := call(h, "10.0.0.9", "/api/drives", testPassword).Code; got != http.StatusOK {
		t.Fatalf("another IP: got %d", got)
	}
	if got := call(h, "10.0.0.9", "/api/drives", "wrong").Code; got != http.StatusUnauthorized {
		t.Fatalf("another IP's first failure: got %d", got)
	}
}

func TestLoopbackIsLikeAnyOtherIP(t *testing.T) {
	h, _, _ := limitedHandler()
	if got := call(h, "127.0.0.1", "/api/drives", testPassword).Code; got != http.StatusOK {
		t.Fatalf("loopback, right password: got %d", got)
	}
	for i := 0; i < authFailLimit; i++ {
		call(h, "127.0.0.1", "/api/drives", "wrong")
	}
	if got := call(h, "127.0.0.1", "/api/drives", "wrong").Code; got != http.StatusTooManyRequests {
		t.Fatalf("loopback after %d failures: got %d", authFailLimit, got)
	}
	if got := call(h, "::1", "/api/drives", testPassword).Code; got != http.StatusOK {
		t.Fatalf("IPv6 loopback is another IP: got %d", got)
	}
}

func TestAMissingPasswordCountsAsAFailure(t *testing.T) {
	h, _, _ := limitedHandler()
	for i := 0; i < authFailLimit; i++ {
		call(h, "10.0.0.5", "/api/drives", "")
	}
	if got := call(h, "10.0.0.5", "/api/drives", "").Code; got != http.StatusTooManyRequests {
		t.Fatalf("got %d, want 429", got)
	}
}

func TestHealthIsNeverLimited(t *testing.T) {
	h, _, _ := limitedHandler()
	for i := 0; i < authFailLimit+2; i++ {
		call(h, "10.0.0.5", "/api/drives", "wrong")
	}
	if got := call(h, "10.0.0.5", "/api/health", "").Code; got != http.StatusOK {
		t.Fatalf("health while blocked: got %d", got)
	}
}

func TestTheTableIsCapped(t *testing.T) {
	l, clock := testLimiter()
	l.max = 8
	for i := 0; i < 50; i++ {
		l.fail(fmt.Sprintf("10.0.1.%d", i))
		clock.advance(time.Millisecond)
		if n := l.tracked(); n > l.max {
			t.Fatalf("tracked %d IPs, cap %d", n, l.max)
		}
	}
	// The newest are kept; the oldest made room.
	if l.records["10.0.1.49"] == nil || l.records["10.0.1.0"] != nil {
		t.Fatal("the cap dropped the wrong entries")
	}
}

func TestTheCapDropsForgottenEntriesFirst(t *testing.T) {
	l, clock := testLimiter()
	l.max = 3
	l.fail("10.0.2.1")
	clock.advance(authFailWindow + time.Second) // 10.0.2.1 is forgotten now
	for i := 0; i < authFailLimit; i++ {
		l.fail("10.0.2.2") // blocked: it lasts longer
	}
	l.fail("10.0.2.3")
	l.fail("10.0.2.4") // full: the forgotten one goes first
	if l.records["10.0.2.1"] != nil || l.records["10.0.2.2"] == nil {
		t.Fatal("a live block was dropped while a forgotten entry was kept")
	}
	if l.tracked() != 3 {
		t.Fatalf("tracked %d", l.tracked())
	}
}

func TestDefaultCap(t *testing.T) {
	if authMaxTracked < 256 || authMaxTracked > 65536 {
		t.Fatalf("authMaxTracked = %d", authMaxTracked)
	}
	if newAuthLimiter().max != authMaxTracked {
		t.Fatal("newAuthLimiter does not use the cap")
	}
}
