//go:build !windows

package main

import (
	"os"
	"testing"
)

// /dev/null and a pipe are not terminals: --discover must not wait on them.
func TestStdinIsTerminalNotForNullOrPipe(t *testing.T) {
	saved := os.Stdin
	t.Cleanup(func() { os.Stdin = saved })

	null, err := os.Open(os.DevNull)
	if err != nil {
		t.Fatal(err)
	}
	defer null.Close()
	os.Stdin = null
	if stdinIsTerminal() {
		t.Errorf("/dev/null read as a terminal")
	}

	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	defer w.Close()
	os.Stdin = r
	if stdinIsTerminal() {
		t.Errorf("a pipe read as a terminal")
	}
}
