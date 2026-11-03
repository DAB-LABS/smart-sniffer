//go:build windows

package main

import (
	"os"

	"golang.org/x/sys/windows"
)

// stdinIsTerminal reports whether standard input is a console someone can
// answer on. Redirected input (a pipe, a file, NUL) is not.
func stdinIsTerminal() bool {
	var mode uint32
	return windows.GetConsoleMode(windows.Handle(os.Stdin.Fd()), &mode) == nil
}
