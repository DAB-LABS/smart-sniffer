package main

import (
	"strings"
	"testing"
)

// shouldSkip mirrors the prefix-matching logic used by ResolveAdvertiseInterfaces
// and PreferredIP. It returns true if the interface name matches any entry in
// defaultSkipPrefixes.
func shouldSkip(ifaceName string) bool {
	nameLower := strings.ToLower(ifaceName)
	for _, prefix := range defaultSkipPrefixes {
		if strings.HasPrefix(nameLower, prefix) {
			return true
		}
	}
	return false
}

// TestSkipPrefixes_Filtered confirms that known virtual/container/VPN interface
// names are correctly identified for skipping.
func TestSkipPrefixes_Filtered(t *testing.T) {
	cases := []struct {
		name   string
		reason string
	}{
		// Loopback
		{"lo", "loopback"},

		// Container / Docker
		{"docker0", "Docker bridge"},
		{"docker_gwbridge", "Docker Swarm gateway"},
		{"br-2a721a667966", "Docker custom network"},
		{"veth1234abc", "container veth pair"},
		{"podman0", "Podman bridge"},
		{"hassio", "HA OS supervisor bridge"},

		// LXC / LXD
		{"lxcbr0", "LXC container bridge (caused #19)"},
		{"lxdbr0", "LXD container bridge"},

		// Kubernetes CNI
		{"flannel.1", "Flannel overlay"},
		{"cni0", "generic CNI bridge"},
		{"calico-tunnel", "Calico overlay"},
		{"cali12345", "Calico veth pair"},
		{"cilium_host", "Cilium overlay"},
		{"weave", "Weave Net overlay"},
		{"crc", "CodeReady Containers"},

		// VPN / Tunnel
		{"zt0", "ZeroTier"},
		{"tailscale0", "Tailscale long form"},
		{"ts0", "Tailscale short form"},
		{"wg0", "WireGuard"},
		{"tun0", "OpenVPN tunnel"},
		{"tap0", "generic TAP"},
		{"utun3", "macOS userspace tunnel"},
		{"ipsec0", "IPsec tunnel"},
		{"gre0", "GRE tunnel"},
		{"geneve0", "Geneve encapsulation"},
		{"vxlan0", "VXLAN overlay"},
		{"erspan0", "ERSPAN monitoring"},

		// Hypervisor / VM
		{"virbr0", "libvirt/KVM bridge"},
		{"vbox0", "VirtualBox host-only"},
		{"vmnet1", "VMware host-only"},
		{"vmbr0", "Proxmox virtual bridge"},
		{"xenbr0", "Xen bridge"},
		{"qemu0", "QEMU virtual NIC"},
		{"vmk0", "ESXi VMkernel"},
		{"hv_kvp", "Hyper-V virtual"},
		{"fwbr100i0", "Proxmox firewall bridge"},
		{"fwpr100p0", "Proxmox firewall proxy"},
		{"fwln100i0", "Proxmox firewall link"},

		// macOS virtual
		{"ap1", "Apple access point"},
		{"awdl0", "Apple Wireless Direct Link"},
		{"llw0", "Low Latency WLAN"},
		{"bridge0", "macOS VM bridging"},
		{"gif0", "macOS/BSD generic tunnel"},
		{"stf0", "macOS/BSD 6to4 tunnel"},
		{"anpi0", "Apple Network Privacy"},
		{"qlf0", "Apple internal"},

		// Windows virtual
		{"vEthernet", "Hyper-V virtual Ethernet"},
		{"isatap0", "ISATAP tunnel"},

		// Linux misc
		{"dummy0", "dummy interface"},
		{"ifb0", "Intermediate Functional Block"},
		{"ovs-system", "Open vSwitch"},
		{"ham0", "FreeBSD HAST mirror"},
		{"epair0a", "FreeBSD jail virtual pair"},
		{"vnet0", "FreeBSD bhyve/jail virtual NIC"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if !shouldSkip(tc.name) {
				t.Errorf("%s (%s) should be filtered but was not", tc.name, tc.reason)
			}
		})
	}
}

// TestSkipPrefixes_NotFiltered confirms that real LAN interfaces and
// intentionally excluded ambiguous prefixes are NOT filtered. These are
// the dangerous cases -- filtering any of these would break real setups.
func TestSkipPrefixes_NotFiltered(t *testing.T) {
	cases := []struct {
		name   string
		reason string
	}{
		// Standard physical interfaces
		{"eth0", "Linux physical NIC"},
		{"eth1", "Linux physical NIC"},
		{"ens18", "systemd predictable name"},
		{"enp3s0", "systemd predictable name"},
		{"eno1", "systemd onboard NIC"},
		{"en0", "macOS primary interface"},
		{"en1", "macOS secondary interface"},
		{"wlan0", "wireless LAN"},
		{"wlp2s0", "systemd wireless"},

		// Intentionally excluded -- ambiguous / primary LAN on NAS
		{"bond0", "Synology bonded NIC / LACP"},
		{"bond1", "bonded NIC"},
		{"br0", "Unraid host bridge (bare br, no dash)"},
		{"vlan10", "VLAN -- may be only routable IP"},
		{"vlan100", "VLAN"},
		{"qvs0", "QNAP primary management interface"},
		{"qbr0", "QNAP/OpenStack ambiguous"},
		{"qvo0", "QNAP/OpenStack ambiguous"},
		{"qvb0", "QNAP/OpenStack ambiguous"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if shouldSkip(tc.name) {
				t.Errorf("%s (%s) should NOT be filtered but was", tc.name, tc.reason)
			}
		})
	}
}

// TestSkipPrefixes_CaseInsensitive confirms that prefix matching is
// case-insensitive (important for Windows interface names like vEthernet).
func TestSkipPrefixes_CaseInsensitive(t *testing.T) {
	cases := []string{
		"Docker0",
		"DOCKER0",
		"VEthernet (Default Switch)",
		"VETHERNET",
		"Tailscale0",
		"TAILSCALE0",
	}

	for _, name := range cases {
		t.Run(name, func(t *testing.T) {
			if !shouldSkip(name) {
				t.Errorf("%s should be filtered (case-insensitive) but was not", name)
			}
		})
	}
}
