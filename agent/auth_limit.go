package main

import (
	"log"
	"net"
	"net/http"
	"strconv"
	"sync"
	"time"
)

// ---------------------------------------------------------------------------
// Slowing down repeated wrong passwords (v1.0)
// ---------------------------------------------------------------------------
//
// Per client IP: after authFailLimit failed attempts within authFailWindow,
// the agent answers 429 with a Retry-After header instead of checking the
// password. Each further failure after a block doubles the block, from
// authBlockStart up to authBlockMax. A correct password resets that IP; an IP
// with no failure for authFailWindow (counted from the end of its block) is
// forgotten.
//
// While an IP is blocked its requests are not checked at all, so a correct
// guess cannot be told from a wrong one during the block. A client that only
// ever sends the correct password never collects a failure and is never
// slowed down. Loopback gets no exemption.
//
// Memory is bounded: at most authMaxTracked IPs are kept. When the table is
// full, forgotten entries are dropped first, then the entry closest to being
// forgotten.
//
// Each new block logs one line: the address, how many wrong or missing
// passwords it has sent, and how long it is blocked. Requests answered 429
// during a block log nothing, and nothing about the password sent is ever
// logged.
//
// The client IP is the TCP peer address. X-Forwarded-For and similar headers
// are ignored: the agent is not meant to sit behind a proxy, and a header
// would let a client pick its own IP.

const (
	authFailLimit  = 5
	authFailWindow = 60 * time.Second
	authBlockStart = 1 * time.Second
	authBlockMax   = 60 * time.Second
	authMaxTracked = 1024
)

type authRecord struct {
	failures     int
	lastFailure  time.Time
	blockedUntil time.Time
	block        time.Duration // the length of the last block, 0 before the first
}

// expires is when the record is forgotten: authFailWindow after the last
// failure or after the end of the block, whichever is later.
func (r *authRecord) expires() time.Time {
	end := r.lastFailure
	if r.blockedUntil.After(end) {
		end = r.blockedUntil
	}
	return end.Add(authFailWindow)
}

type authLimiter struct {
	mu      sync.Mutex
	now     func() time.Time
	records map[string]*authRecord
	max     int
	logf    func(format string, args ...any) // one line per new block; log.Printf in the agent
}

func newAuthLimiter() *authLimiter {
	return &authLimiter{now: time.Now, records: make(map[string]*authRecord), max: authMaxTracked, logf: log.Printf}
}

// blocked reports whether ip must wait, and for how long.
func (l *authLimiter) blocked(ip string) (bool, time.Duration) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	r, ok := l.records[ip]
	if !ok {
		return false, 0
	}
	if !now.Before(r.expires()) {
		delete(l.records, ip)
		return false, 0
	}
	if now.Before(r.blockedUntil) {
		return true, r.blockedUntil.Sub(now)
	}
	return false, 0
}

// fail records a wrong or missing password from ip, and logs one line when
// that starts a block.
func (l *authLimiter) fail(ip string) {
	failures, block := l.record(ip)
	if block > 0 && l.logf != nil {
		l.logf("Auth: blocking %s for %s after %d wrong or missing passwords", ip, block, failures)
	}
}

// record counts the failure and returns the failure count and, when this
// failure started a block, its length (0 otherwise).
func (l *authLimiter) record(ip string) (int, time.Duration) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	r, ok := l.records[ip]
	if ok && !now.Before(r.expires()) {
		delete(l.records, ip)
		ok = false
	}
	if !ok {
		l.makeRoom(now)
		r = &authRecord{}
		l.records[ip] = r
	}
	r.failures++
	r.lastFailure = now
	if r.failures >= authFailLimit {
		if r.block == 0 {
			r.block = authBlockStart
		} else {
			r.block *= 2
			if r.block > authBlockMax {
				r.block = authBlockMax
			}
		}
		r.blockedUntil = now.Add(r.block)
		return r.failures, r.block
	}
	return r.failures, 0
}

// succeed forgets ip: a correct password resets it.
func (l *authLimiter) succeed(ip string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.records, ip)
}

// makeRoom keeps the table under its cap before a new IP is added. Called
// with the lock held.
func (l *authLimiter) makeRoom(now time.Time) {
	if len(l.records) < l.max {
		return
	}
	for ip, r := range l.records {
		if !now.Before(r.expires()) {
			delete(l.records, ip)
		}
	}
	for len(l.records) >= l.max {
		var oldest string
		var at time.Time
		for ip, r := range l.records {
			if oldest == "" || r.expires().Before(at) {
				oldest, at = ip, r.expires()
			}
		}
		delete(l.records, oldest)
	}
}

func (l *authLimiter) tracked() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.records)
}

// clientIP is the request's TCP peer address without its port.
func clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

// retryAfter is a Retry-After value in whole seconds, rounded up, at least 1.
func retryAfter(d time.Duration) string {
	secs := int((d + time.Second - 1) / time.Second)
	if secs < 1 {
		secs = 1
	}
	return strconv.Itoa(secs)
}
