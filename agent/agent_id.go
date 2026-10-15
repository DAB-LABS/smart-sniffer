package main

// Agent ID: a random 128-bit identifier, saved once and reported in
// /api/health and the mDNS TXT record as agent_id. It lets Home Assistant
// tell one agent from another when hostnames or addresses repeat.
//
// It is never derived from serials, MACs or host data. When it cannot be
// saved anywhere, the agent reports no ID at all rather than one that
// changes on every restart.

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"time"
)

// agentID is resolved once in RunAgent before the HTTP server starts.
// Empty means "not reported".
var agentID string

const agentIDFileName = "agent_id.json"

var agentIDRe = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{7,63}$`)

// normAgentID trims and lowercases s and checks it: 8 to 64 characters of
// a-z, 0-9 and -, not starting with -.
func normAgentID(s string) (string, bool) {
	id := strings.ToLower(strings.TrimSpace(s))
	if !agentIDRe.MatchString(id) {
		return "", false
	}
	return id, true
}

// newAgentID returns 32 lowercase hex characters from crypto/rand.
func newAgentID() string {
	var b [16]byte
	_, _ = rand.Read(b[:]) // crypto/rand.Read does not fail on supported platforms
	return hex.EncodeToString(b[:])
}

type agentIDRecord struct {
	AgentID string `json:"agent_id"`
	Created string `json:"created,omitempty"`
}

// agentIDCandidateDirs lists where agent_id.json may live, in order.
func agentIDCandidateDirs(configPath string) []string {
	return agentIDCandidateDirsFor(configPath, runtime.GOOS, os.Getenv, func(p string) bool {
		st, err := os.Stat(p)
		return err == nil && st.IsDir()
	})
}

// agentIDCandidateDirsFor:
//  1. the directory of the config file that was loaded;
//  2. /data in the Home Assistant app (SUPERVISOR_TOKEN set and /data a
//     directory; the app runs the agent without a config file);
//  3. the platform state directory: %ProgramData%\SmartHA-Agent on Windows,
//     /Library/Application Support/SmartHA-Agent on macOS,
//     /var/lib/smartha-agent elsewhere.
func agentIDCandidateDirsFor(configPath, goos string, getenv func(string) string, isDir func(string) bool) []string {
	var dirs []string
	add := func(d string) {
		for _, x := range dirs {
			if x == d {
				return
			}
		}
		dirs = append(dirs, d)
	}
	if configPath != "" {
		if goos == "windows" && runtime.GOOS != "windows" {
			// Tests only: filepath.Dir on a non-Windows build does not split on '\'.
			if i := strings.LastIndexAny(configPath, `\/`); i > 0 {
				add(configPath[:i])
			}
		} else {
			add(filepath.Dir(configPath))
		}
	}
	if getenv("SUPERVISOR_TOKEN") != "" && isDir("/data") {
		add("/data")
	}
	switch goos {
	case "windows":
		base := getenv("ProgramData")
		if base == "" {
			base = `C:\ProgramData`
		}
		add(strings.TrimRight(base, `\`) + `\SmartHA-Agent`)
	case "darwin":
		add("/Library/Application Support/SmartHA-Agent")
	default:
		add("/var/lib/smartha-agent")
	}
	return dirs
}

// resolveAgentID returns the pinned ID, else the ID saved in the first
// candidate directory that has a valid agent_id.json, else a new ID saved
// in the first candidate directory that accepts it. If none does, it logs
// one warning and returns "" so nothing is reported.
func resolveAgentID(pin string, dirs []string, logf func(string, ...any)) string {
	if pin != "" {
		logf("Agent ID: %s (from config)", pin)
		return pin
	}
	var fresh string
	var failures []string
	for _, dir := range dirs {
		path := filepath.Join(dir, agentIDFileName)
		data, err := os.ReadFile(path)
		switch {
		case err == nil:
			var rec agentIDRecord
			if jerr := json.Unmarshal(data, &rec); jerr == nil {
				if id, ok := normAgentID(rec.AgentID); ok {
					logf("Agent ID: %s (saved in %s)", id, path)
					return id
				}
			}
			logf("WARNING: agent ID: %s is not a valid agent ID file; replacing it with a new ID", path)
		case !errors.Is(err, fs.ErrNotExist):
			failures = append(failures, fmt.Sprintf("%s: %v", path, err))
			continue
		}
		if fresh == "" {
			fresh = newAgentID()
		}
		if err := writeAgentIDFile(dir, fresh); err != nil {
			failures = append(failures, fmt.Sprintf("%s: %v", path, err))
			continue
		}
		logf("Agent ID: %s (new, saved in %s)", fresh, path)
		return fresh
	}
	why := "no directory to save it in"
	if len(failures) > 0 {
		why = strings.Join(failures, "; ")
	}
	logf("WARNING: agent ID: not reported, could not save %s (%s); set agent_id in config.yaml to pin one", agentIDFileName, why)
	return ""
}

// writeAgentIDFile writes <dir>/agent_id.json atomically: a temp file in
// the same directory, fsync, then rename over the target.
func writeAgentIDFile(dir, id string) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	data, err := json.Marshal(agentIDRecord{AgentID: id, Created: time.Now().UTC().Format(time.RFC3339)})
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, agentIDFileName+".tmp*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	ok := false
	defer func() {
		if !ok {
			_ = os.Remove(tmp)
		}
	}()
	if _, err := f.Write(append(data, '\n')); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp, 0o644); err != nil {
		return err
	}
	if err := os.Rename(tmp, filepath.Join(dir, agentIDFileName)); err != nil {
		return err
	}
	ok = true
	return nil
}
