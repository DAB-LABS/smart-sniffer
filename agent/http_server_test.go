package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestHTTPServerLimits(t *testing.T) {
	srv := newHTTPServer(":9099", http.NotFoundHandler())
	if srv.ReadHeaderTimeout != 5*time.Second {
		t.Errorf("ReadHeaderTimeout = %v, want 5s", srv.ReadHeaderTimeout)
	}
	if srv.IdleTimeout != 60*time.Second {
		t.Errorf("IdleTimeout = %v, want 60s", srv.IdleTimeout)
	}
	if srv.MaxHeaderBytes != 64*1024 {
		t.Errorf("MaxHeaderBytes = %d, want 65536", srv.MaxHeaderBytes)
	}
	// No limit on reading a body or writing a response (see newHTTPServer).
	if srv.ReadTimeout != 0 || srv.WriteTimeout != 0 {
		t.Errorf("ReadTimeout = %v, WriteTimeout = %v, want none", srv.ReadTimeout, srv.WriteTimeout)
	}
}

func TestHTTPServerRefusesOversizedHeaders(t *testing.T) {
	ts := httptest.NewUnstartedServer(nil)
	ts.Config = newHTTPServer("", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	ts.Start()
	defer ts.Close()

	get := func(header string) int {
		req, err := http.NewRequest(http.MethodGet, ts.URL+"/api/health", nil)
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("X-Filler", header)
		resp, err := ts.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if code := get("small"); code != http.StatusOK {
		t.Errorf("small headers: status %d, want 200", code)
	}
	if code := get(strings.Repeat("a", 80*1024)); code != http.StatusRequestHeaderFieldsTooLarge {
		t.Errorf("80 KiB of headers: status %d, want 431", code)
	}
}
