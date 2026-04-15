//go:build windows

// main_windows.go — Windows process entry point with Service Control
// Manager (SCM) integration.
//
// Two launch modes:
//
//  1. SCM-launched (the normal install.ps1 flow): svc.IsWindowsService()
//     returns true, we hand off to svc.Run which invokes our Execute
//     handler. Execute is responsible for reporting StartPending /
//     Running / StopPending / Stopped status transitions to the SCM on
//     time, or the service will be killed with Error 1053.
//
//  2. Console-launched (developer / manual run): svc.IsWindowsService()
//     returns false and we fall through to a SIGINT/SIGTERM path that
//     mirrors main_unix.go exactly.
//
// See docs/internal/plans/plan-v0.5.1-consolidated-changes.md §Change 2.
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/debug"
)

// readyWatchdog is the maximum time we wait for RunAgent to close its
// ready channel before reporting Running to the SCM anyway. The preflight
// smartctl --scan can take a while on boxes with many disks; reporting
// Running after this cap prevents SCM from timing us out with Error 1053
// while the scan continues in the background.
//
// Chosen well under the SCM default 30s start timeout to leave headroom
// for svc.Run wiring and the StartPending → Running status write itself.
const readyWatchdog = 20 * time.Second

// startPendingHint is the WaitHint we report to the SCM during
// StartPending. The SCM expects us to either finish starting or bump the
// CheckPoint before this elapses. We set it equal to the watchdog plus a
// small margin so the SCM's own timer and ours agree.
const startPendingHint = 25 * time.Second

// elog is the package-level Event Log handle. Assigned by Execute at
// the top of the SCM path so all failure paths can log consistently.
// Nil on the console path; callers must check before use (or use the
// logf helper below which tolerates a nil elog).
var elog debug.Log

// logLevel selects which Event Log category a message lands under. The
// numeric values are local — they map to debug.Log.Info / Warning /
// Error when elog is non-nil, and to stderr otherwise.
type logLevel int

const (
	logInfo logLevel = iota
	logWarn
	logError
)

// logf writes to the Event Log if elog is set and falls back to stderr
// otherwise. Keeps the error-surfacing code readable without every
// call site having to branch on elog == nil. We take the level as an
// enum rather than a method value so the helper is safe to call before
// elog has been assigned (method values on a nil interface panic).
func logf(level logLevel, id uint32, format string, args ...interface{}) {
	msg := fmt.Sprintf(format, args...)
	if elog == nil {
		fmt.Fprintln(os.Stderr, msg)
		return
	}
	switch level {
	case logError:
		_ = elog.Error(id, msg)
	case logWarn:
		_ = elog.Warning(id, msg)
	default:
		_ = elog.Info(id, msg)
	}
}

// service implements svc.Handler. It is stateless aside from the ctx
// cancel function that the control loop uses to signal RunAgent to exit.
type service struct{}

func main() {
	inSvc, err := svc.IsWindowsService()
	if err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: svc.IsWindowsService failed: %v\n", err)
		os.Exit(1)
	}

	if inSvc {
		// SCM path — svc.Run blocks until Execute returns. Any error
		// here means the SCM plumbing itself failed; actual agent
		// errors are reported via the service status and Event Log.
		if err := svc.Run(ServiceName, &service{}); err != nil {
			fmt.Fprintf(os.Stderr, "ERROR: svc.Run(%s): %v\n", ServiceName, err)
			os.Exit(1)
		}
		return
	}

	// Console path — identical semantics to main_unix.go.
	ctx, stop := signal.NotifyContext(context.Background(),
		syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	if err := RunAgent(ctx, nil); err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: %v\n", err)
		os.Exit(1)
	}
}

// Execute is the SCM handler. It must:
//   - Report StartPending promptly (before the SCM's 30s timeout).
//   - Report Running once the agent is actually accepting connections,
//     or after the watchdog fires, whichever comes first.
//   - Handle Interrogate / Stop / Shutdown control codes.
//   - Report Stopped before returning.
//
// The args and r/changes parameters are defined by the svc.Handler
// interface; we use ctx cancellation to drive RunAgent's shutdown
// instead of propagating the control codes further.
func (s *service) Execute(args []string, r <-chan svc.ChangeRequest, changes chan<- svc.Status) (ssec bool, errno uint32) {
	const accepted = svc.AcceptStop | svc.AcceptShutdown

	// Open the Event Log as early as possible so any failure below
	// surfaces in Event Viewer instead of the void. openEventLog
	// tolerates a missing source by falling back to stderr.
	elog = openEventLog(ServiceName)
	defer func() {
		if elog != nil {
			_ = elog.Close()
		}
	}()

	// Step 1: acknowledge the start request immediately.
	changes <- svc.Status{
		State:      svc.StartPending,
		WaitHint:   uint32(startPendingHint / time.Millisecond),
		CheckPoint: 1,
	}

	// Step 2: launch RunAgent in a goroutine. The ready channel closes
	// when the HTTP listener is bound; runErr receives RunAgent's
	// return value so we can distinguish normal shutdown from crash.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	ready := make(chan struct{})
	runErr := make(chan error, 1)

	go func() {
		runErr <- RunAgent(ctx, ready)
	}()

	// Step 3: wait for ready or watchdog, then report Running.
	select {
	case <-ready:
		// Normal path — listener bound, agent accepting connections.
	case <-time.After(readyWatchdog):
		// Watchdog path — preflight is still running but we need to
		// report Running so the SCM doesn't 1053 us. RunAgent will
		// either finish preflight and bind the listener, or return an
		// error which we'll catch in the control loop below.
	case err := <-runErr:
		// RunAgent failed before binding the listener (config or
		// preflight error). Report Stopped with a non-zero exit code
		// so the SCM and Event Log see the failure.
		changes <- svc.Status{State: svc.Stopped}
		logf(logError, evtStartupFailure, "agent failed during startup: %v", err)
		return false, 1
	}

	changes <- svc.Status{State: svc.Running, Accepts: accepted}
	logf(logInfo, evtStarted, "%s started; accepting connections", ServiceName)

	// Step 4: control loop — service keeps running until SCM asks us to
	// stop or RunAgent returns on its own (which would indicate an
	// unexpected exit from the run loop).
	for {
		select {
		case c := <-r:
			switch c.Cmd {
			case svc.Interrogate:
				// Echo current status back; SCM uses this to probe.
				changes <- c.CurrentStatus
			case svc.Stop, svc.Shutdown:
				// Tell the SCM we're stopping, then cancel the ctx
				// so RunAgent runs its bounded shutdown path.
				changes <- svc.Status{
					State:    svc.StopPending,
					WaitHint: uint32(10 * time.Second / time.Millisecond),
				}
				cancel()
				// Wait for RunAgent to finish its shutdown budget
				// before reporting Stopped. If it hangs, the SCM's
				// own timer will eventually kill us.
				err := <-runErr
				changes <- svc.Status{State: svc.Stopped}
				if err != nil {
					logf(logError, evtShutdownError, "agent shutdown: %v", err)
					return false, 1
				}
				logf(logInfo, evtStopped, "%s stopped", ServiceName)
				return false, 0
			default:
				// Unknown control code — ignore per SCM convention.
			}
		case err := <-runErr:
			// RunAgent exited without an SCM stop request. This is
			// unexpected (crash or internal ctx cancel) — report
			// Stopped and surface the error.
			changes <- svc.Status{State: svc.Stopped}
			if err != nil {
				logf(logError, evtRuntimeFailure, "agent exited unexpectedly: %v", err)
				return false, 1
			}
			logf(logInfo, evtStopped, "%s stopped", ServiceName)
			return false, 0
		}
	}
}
