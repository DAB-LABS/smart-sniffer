package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// A real server around the agent's handler chain (HEAD bodies are dropped by
// net/http's server, not by the handlers, so a recorder would not show it).
func chainServer(t *testing.T, token string) *httptest.Server {
	t.Helper()
	dc := NewDriveCache(&Config{ScanInterval: time.Minute})
	dc.drives["sda-1"] = DriveInfo{ID: "sda-1", Model: "FAKE"}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/health", handleHealth(dc))
	mux.HandleFunc("/api/drives", dc.HandleDrives)
	mux.HandleFunc("/api/drives/", dc.HandleDrive)
	srv := httptest.NewServer(apiHandlerChain(token, mux))
	t.Cleanup(srv.Close)
	return srv
}

func do(t *testing.T, method, url, password string) (*http.Response, []byte) {
	t.Helper()
	req, _ := http.NewRequest(method, url, nil)
	if password != "" {
		req.Header.Set("Authorization", "Bearer "+password)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp, body
}

// errorBody checks the one error shape and returns its code.
func errorBody(t *testing.T, resp *http.Response, body []byte) string {
	t.Helper()
	if ct := resp.Header.Get("Content-Type"); ct != "application/json" {
		t.Errorf("%d: Content-Type %q", resp.StatusCode, ct)
	}
	var e map[string]any
	if err := json.Unmarshal(body, &e); err != nil {
		t.Fatalf("%d: body is not JSON: %q", resp.StatusCode, body)
	}
	code, _ := e["error"].(string)
	msg, _ := e["message"].(string)
	if code == "" || msg == "" || len(e) != 2 {
		t.Errorf("%d: body %s, want exactly error and message", resp.StatusCode, body)
	}
	if strings.ToLower(code) != code || strings.Contains(code, " ") {
		t.Errorf("code %q is not snake_case", code)
	}
	return code
}

func TestEveryErrorHasTheOneShape(t *testing.T) {
	const pw = "sneaky-cipher-otter-4821"
	srv := chainServer(t, pw)
	cases := []struct {
		method, path, password string
		status                 int
		code                   string
	}{
		{"GET", "/api/drives", "", 401, "unauthorized"},
		{"GET", "/api/drives", "wrong", 401, "unauthorized"},
		{"GET", "/api/drives/nope", pw, 404, "not_found"},
		{"GET", "/api/nothing", pw, 404, "not_found"},
		{"GET", "/", pw, 404, "not_found"},
		{"POST", "/api/drives", pw, 405, "method_not_allowed"},
		{"DELETE", "/api/drives/sda-1", pw, 405, "method_not_allowed"},
		{"PUT", "/api/health", "", 405, "method_not_allowed"},
		{"OPTIONS", "/api/health", "", 405, "method_not_allowed"},
	}
	for _, tc := range cases {
		resp, body := do(t, tc.method, srv.URL+tc.path, tc.password)
		if resp.StatusCode != tc.status {
			t.Errorf("%s %s: %d, want %d", tc.method, tc.path, resp.StatusCode, tc.status)
			continue
		}
		if code := errorBody(t, resp, body); code != tc.code {
			t.Errorf("%s %s: code %q, want %q", tc.method, tc.path, code, tc.code)
		}
		if tc.status == 405 && resp.Header.Get("Allow") != "GET, HEAD" {
			t.Errorf("%s %s: Allow %q", tc.method, tc.path, resp.Header.Get("Allow"))
		}
	}
}

// Without the password an unknown path is 401, not 404: no listing of what
// exists to an address that cannot read it.
func TestUnknownPathsNeedThePasswordFirst(t *testing.T) {
	srv := chainServer(t, "pw-1234")
	resp, _ := do(t, "GET", srv.URL+"/api/nothing", "")
	if resp.StatusCode != 401 {
		t.Errorf("unknown path without password: %d, want 401", resp.StatusCode)
	}
}

func TestNoPasswordStillHasTheShape(t *testing.T) {
	srv := chainServer(t, "")
	resp, body := do(t, "GET", srv.URL+"/api/nothing", "")
	if resp.StatusCode != 404 || errorBody(t, resp, body) != "not_found" {
		t.Errorf("%d %s", resp.StatusCode, body)
	}
	resp, body = do(t, "POST", srv.URL+"/api/health", "")
	if resp.StatusCode != 405 || errorBody(t, resp, body) != "method_not_allowed" {
		t.Errorf("%d %s", resp.StatusCode, body)
	}
}

// HEAD answers as GET would, with no body.
func TestHeadAnswersLikeGetWithoutABody(t *testing.T) {
	const pw = "pw-1234"
	srv := chainServer(t, pw)
	for _, tc := range []struct {
		path, password string
		status         int
	}{
		{"/api/health", "", 200},
		{"/api/drives", pw, 200},
		{"/api/drives/sda-1", pw, 200},
		{"/api/drives", "", 401},
		{"/api/drives/nope", pw, 404},
	} {
		get, getBody := do(t, "GET", srv.URL+tc.path, tc.password)
		head, headBody := do(t, "HEAD", srv.URL+tc.path, tc.password)
		if get.StatusCode != tc.status || head.StatusCode != tc.status {
			t.Errorf("%s: GET %d HEAD %d, want %d", tc.path, get.StatusCode, head.StatusCode, tc.status)
		}
		if len(getBody) == 0 || len(headBody) != 0 {
			t.Errorf("%s: GET body %d bytes, HEAD body %d bytes", tc.path, len(getBody), len(headBody))
		}
		if head.Header.Get("Content-Type") != get.Header.Get("Content-Type") {
			t.Errorf("%s: Content-Type HEAD %q GET %q", tc.path, head.Header.Get("Content-Type"), get.Header.Get("Content-Type"))
		}
	}
}

// The integration keys on these exactly (panel_net._wants_password reads
// "unauthorized").
func TestUnauthorizedKeepsItsCode(t *testing.T) {
	srv := chainServer(t, "pw-1234")
	resp, body := do(t, "GET", srv.URL+"/api/drives", "")
	var e apiError
	json.Unmarshal(body, &e)
	if resp.StatusCode != 401 || e.Error != "unauthorized" {
		t.Errorf("%d %s", resp.StatusCode, body)
	}
}
