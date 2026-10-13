//go:build darwin

package main

import "syscall"

// mountSource is the device mounted at the path statfs looked at, e.g.
// /dev/disk3s5.
func mountSource(stat *syscall.Statfs_t) string {
	b := make([]byte, 0, len(stat.Mntfromname))
	for _, c := range stat.Mntfromname {
		if c == 0 {
			break
		}
		b = append(b, byte(c))
	}
	return string(b)
}
