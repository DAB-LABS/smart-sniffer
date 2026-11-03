//go:build !windows

package main

import "os"

// stdinIsTerminal reports whether standard input is a terminal someone can
// answer on: a character device that is not /dev/null. A pipe, a file, or
// ssh without a pty is not.
func stdinIsTerminal() bool {
	fi, err := os.Stdin.Stat()
	if err != nil || fi.Mode()&os.ModeCharDevice == 0 {
		return false
	}
	if null, err := os.Stat(os.DevNull); err == nil && os.SameFile(fi, null) {
		return false
	}
	return true
}
