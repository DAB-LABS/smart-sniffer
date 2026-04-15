//go:build !windows

// main_unix.go — process entry point for Linux, macOS, and BSD.
// Wires SIGINT / SIGTERM into a cancelable context and hands off to
// RunAgent. Windows uses main_windows.go, which adds Service Control
// Manager integration on top of RunAgent.
//
// See docs/internal/plans/plan-v0.5.1-consolidated-changes.md §Change 3.
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(),
		syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// ready is unused in the Unix path; RunAgent handles a nil channel.
	if err := RunAgent(ctx, nil); err != nil {
		fmt.Fprintf(os.Stderr, "ERROR: %v\n", err)
		os.Exit(1)
	}
}
