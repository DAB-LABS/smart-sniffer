//go:build windows

// eventlog_windows.go — Windows Event Log wiring for SCM-mode failures.
//
// When the agent runs under the Service Control Manager there is no
// console attached, so writes to os.Stderr go to the void. Operators
// debugging a failed service start see only "Error 1053" in the
// Services panel with no context. This file opens a handle to the
// Windows Event Log under source name ServiceName so startup,
// shutdown, and runtime errors surface in Event Viewer.
//
// If opening the real Event Log fails (typically because install.ps1
// has not yet registered the source via New-EventLog), we fall back
// to svc/debug's stderr logger so the console path still works and
// we never panic on a missing source.
//
// Event ID scheme (keep in sync with docs/internal/plans/
// plan-v0.5.1-consolidated-changes.md §Change 4):
//
//	  1  Service started (informational)
//	  2  Service stopped (informational)
//	100  Startup failure (config or preflight error)
//	101  Runtime failure (RunAgent returned error mid-flight)
//	102  Shutdown error
package main

import (
	"golang.org/x/sys/windows/svc/debug"
	"golang.org/x/sys/windows/svc/eventlog"
)

// Event IDs — exported as typed constants so handlers log consistent
// numbers. uint32 matches the debug.Log interface signature.
const (
	evtStarted         uint32 = 1
	evtStopped         uint32 = 2
	evtStartupFailure  uint32 = 100
	evtRuntimeFailure  uint32 = 101
	evtShutdownError   uint32 = 102
)

// openEventLog returns a debug.Log bound to the Windows Event Log under
// the given source name. If the source is not registered (install.ps1
// did not run New-EventLog, or the binary is being invoked directly by
// a developer), we fall back to debug.New which writes to stderr and
// is safe to use unconditionally.
//
// The caller is responsible for invoking Close on the returned handle
// when the service is stopping; closing a debug.ConsoleLog is a no-op
// so the same defer works for both code paths.
func openEventLog(source string) debug.Log {
	el, err := eventlog.Open(source)
	if err != nil {
		// Source is not registered with the Event Log — fall back to
		// the svc/debug console logger. This keeps the interactive
		// developer experience (running the .exe directly from a
		// terminal) identical to what it used to be, and degrades
		// gracefully if an upgrade left the service registered but
		// the Event Log source missing.
		return debug.New(source)
	}
	return el
}
