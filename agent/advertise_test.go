package main

import (
	"net"
	"reflect"
	"strings"
	"testing"
)

// Fixtures use RFC 5737 documentation addresses for public IPs and
// textbook private ranges. No addresses are taken from user reports.

// fx builds an up, multicast interface with the given IPv4 addresses.
func fx(index int, name string, ips ...string) netIface {
	n := netIface{Iface: net.Interface{Index: index, Name: name, Flags: net.FlagUp | net.FlagMulticast}}
	for _, s := range ips {
		n.IPv4 = append(n.IPv4, net.ParseIP(s).To4())
	}
	return n
}

func downIface(n netIface) netIface {
	n.Iface.Flags &^= net.FlagUp
	return n
}

func loIface() netIface {
	return netIface{Iface: net.Interface{Index: 1, Name: "lo", Flags: net.FlagUp | net.FlagLoopback}}
}

func envOf(ifaces []netIface, routes ...string) netEnv {
	return netEnv{
		list: func() ([]netIface, error) { return ifaces, nil },
		defaultRoute: func() map[string]bool {
			m := map[string]bool{}
			for _, r := range routes {
				m[r] = true
			}
			return m
		},
	}
}

// v082 is the v0.8.2 behaviour (ResolveAdvertiseInterfaces + PreferredIP),
// kept here only as the reference the new code is compared against.
func v082(all []netIface, explicit string) (names []string, ip string) {
	var list []netIface
	if explicit != "" {
		for _, n := range all {
			if n.Iface.Name == explicit {
				list = []netIface{n}
			}
		}
	} else {
		for _, n := range all {
			if !n.up() || n.loopback() || isSkippedInterface(n.Iface.Name) {
				continue
			}
			list = append(list, n)
		}
	}
	scoreFrom := list
	if len(list) == 0 {
		scoreFrom = all
	}
	ip, _ = legacyPick(scoreFrom)
	for _, n := range list {
		names = append(names, n.Iface.Name)
	}
	return names, ip
}

func planNames(p advertisePlan) []string {
	if p.Ifaces == nil {
		return nil
	}
	out := []string{}
	for _, i := range p.Ifaces {
		out = append(out, i.Name)
	}
	return out
}

type advRow struct {
	row      string // the plan's setup table row, or the review item
	ifaces   []netIface
	routes   []string
	explicit string
	today    string // ip= in v0.8.2 (checked against v082 too)
	wantIP   string
	rule     string
	chosen   string
	names    []string // nil = every multicast interface
}

func advRows() []advRow {
	return []advRow{
		{row: "1 plain Linux",
			ifaces: []netIface{loIface(), fx(2, "enp3s0", "192.168.1.10")},
			today:  "192.168.1.10", wantIP: "192.168.1.10", rule: "default", chosen: "enp3s0",
			names: []string{"enp3s0"}},
		{row: "2 Docker host",
			ifaces: []netIface{loIface(), fx(2, "enp3s0", "192.168.1.10"), fx(3, "docker0", "172.17.0.1"), fx(4, "br-1a2b3c4d5e6f", "172.18.0.1"), fx(5, "veth1a2b3c")},
			today:  "192.168.1.10", wantIP: "192.168.1.10", rule: "default", chosen: "enp3s0",
			names: []string{"enp3s0"}},
		{row: "3 Docker, LAN 10.x, named bridge br-media",
			ifaces: []netIface{fx(2, "eth0", "10.0.0.5"), fx(3, "br-media", "192.168.100.1")},
			today:  "10.0.0.5", wantIP: "10.0.0.5", rule: "default", names: []string{"eth0"}},
		{row: "4 Mailcow, public LAN",
			ifaces: []netIface{fx(2, "eth0", "203.0.113.10"), fx(3, "br-mailcow", "172.22.1.1")},
			today:  "203.0.113.10", wantIP: "203.0.113.10", rule: "default", names: []string{"eth0"}},
		{row: "5 Mailcow on a 172.16/12 LAN",
			ifaces: []netIface{fx(2, "eth0", "172.16.5.10"), fx(3, "br-mailcow", "172.22.1.1")},
			today:  "172.16.5.10", wantIP: "172.16.5.10", rule: "default", names: []string{"eth0"}},
		{row: "6 Proxmox default",
			ifaces: []netIface{loIface(), fx(2, "enp3s0"), fx(3, "vmbr0", "192.168.1.5"), fx(4, "tap100i0"), fx(5, "fwbr100i0"), fx(6, "veth101i0")},
			today:  "", wantIP: "192.168.1.5", rule: "rule1", chosen: "vmbr0",
			names: []string{"enp3s0", "vmbr0"}},
		{row: "7 Proxmox, advertise_interface vmbr0",
			ifaces:   []netIface{fx(2, "enp3s0"), fx(3, "vmbr0", "192.168.1.5")},
			explicit: "vmbr0",
			today:    "192.168.1.5", wantIP: "192.168.1.5", rule: "explicit", chosen: "vmbr0",
			names: []string{"vmbr0"}},
		{row: "8 Proxmox vmbr0 192.168 + vmbr1 NAT",
			ifaces: []netIface{fx(2, "enp3s0"), fx(3, "vmbr0", "192.168.1.5"), fx(4, "vmbr1", "10.10.10.1")},
			today:  "", wantIP: "192.168.1.5", rule: "rule1", chosen: "vmbr0",
			names: []string{"enp3s0", "vmbr0"}},
		{row: "9 Proxmox vmbr0 10.x LAN + vmbr1 192.168 lab",
			ifaces: []netIface{fx(2, "enp3s0"), fx(3, "vmbr0", "10.0.0.5"), fx(4, "vmbr1", "192.168.100.1")},
			today:  "", wantIP: "10.0.0.5", rule: "rule1", chosen: "vmbr0",
			names: []string{"enp3s0", "vmbr0"}},
		{row: "10 Proxmox vmbr0 172.16 LAN + vmbr1 10.x lab",
			ifaces: []netIface{fx(2, "enp3s0"), fx(3, "vmbr0", "172.16.0.5"), fx(4, "vmbr1", "10.10.10.1")},
			today:  "", wantIP: "172.16.0.5", rule: "rule1", chosen: "vmbr0",
			names: []string{"enp3s0", "vmbr0"}},
		{row: "11 Proxmox mgmt on vmbr0.20",
			ifaces: []netIface{fx(2, "enp3s0"), fx(3, "vmbr0"), fx(4, "vmbr0.20", "192.168.20.5")},
			today:  "", wantIP: "192.168.20.5", rule: "rule1", chosen: "vmbr0.20",
			names: []string{"enp3s0", "vmbr0.20"}},
		{row: "12/S3 hosted Proxmox, public vmbr0 carries the default route",
			ifaces: []netIface{fx(2, "eno1"), fx(3, "vmbr0", "198.51.100.5"), fx(4, "vmbr1", "10.10.10.1")},
			routes: []string{"vmbr0"},
			today:  "", wantIP: "198.51.100.5", rule: "rule2", chosen: "vmbr0",
			names: []string{"eno1", "vmbr0"}},
		{row: "12 hosted Proxmox, no default route seen",
			ifaces: []netIface{fx(2, "eno1"), fx(3, "vmbr0", "198.51.100.5"), fx(4, "vmbr1", "10.10.10.1")},
			today:  "", wantIP: "10.10.10.1", rule: "rule2", chosen: "vmbr1",
			names: []string{"eno1", "vmbr1"}},
		{row: "13 Proxmox LAN on vmbr1, vmbr0 internal (known limit)",
			ifaces: []netIface{fx(2, "eno1"), fx(3, "vmbr0", "10.10.10.1"), fx(4, "vmbr1", "192.168.1.5")},
			today:  "", wantIP: "10.10.10.1", rule: "rule1", chosen: "vmbr0",
			names: []string{"eno1", "vmbr0"}},
		{row: "14 Proxmox storage NIC 10.x, LAN vmbr0 (1b keeps today's)",
			ifaces: []netIface{fx(2, "eno1"), fx(3, "eno2", "10.10.10.5"), fx(4, "vmbr0", "192.168.1.5")},
			today:  "10.10.10.5", wantIP: "10.10.10.5", rule: "default", names: []string{"eno1", "eno2"}},
		{row: "15 Proxmox NAT, public eno1",
			ifaces: []netIface{fx(2, "eno1", "203.0.113.5"), fx(3, "vmbr0", "10.10.10.1")},
			today:  "203.0.113.5", wantIP: "10.10.10.1", rule: "rule1", chosen: "vmbr0",
			names: []string{"eno1", "vmbr0"}},
		{row: "16 Proxmox NAT, LAN on eno1 (1b keeps today's)",
			ifaces: []netIface{fx(2, "eno1", "192.168.1.5"), fx(3, "vmbr0", "10.10.10.1")},
			today:  "192.168.1.5", wantIP: "192.168.1.5", rule: "default", names: []string{"eno1"}},
		{row: "18 Synology OVS",
			ifaces: []netIface{fx(2, "eth0"), fx(3, "ovs-system"), fx(4, "ovs_eth0", "192.168.1.20"), fx(5, "docker0", "172.17.0.1")},
			today:  "", wantIP: "192.168.1.20", rule: "rule2", chosen: "ovs_eth0",
			names: []string{"eth0", "ovs_eth0"}},
		{row: "19 Synology OVS bond",
			ifaces: []netIface{fx(2, "eth0"), fx(3, "eth1"), fx(4, "bond0"), fx(5, "ovs-system"), fx(6, "ovs_bond0", "192.168.1.20")},
			today:  "", wantIP: "192.168.1.20", rule: "rule2", chosen: "ovs_bond0",
			names: []string{"eth0", "eth1", "bond0", "ovs_bond0"}},
		{row: "20 Synology OVS plus plain eth1",
			ifaces: []netIface{fx(2, "eth0"), fx(3, "eth1", "10.0.0.20"), fx(4, "ovs_eth0", "192.168.1.20")},
			today:  "10.0.0.20", wantIP: "10.0.0.20", rule: "default", names: []string{"eth0", "eth1"}},
		{row: "21 Synology plain bond0",
			ifaces: []netIface{fx(2, "eth0"), fx(3, "bond0", "192.168.1.20"), fx(4, "docker0", "172.17.0.1")},
			today:  "192.168.1.20", wantIP: "192.168.1.20", rule: "default", names: []string{"eth0", "bond0"}},
		{row: "22 QNAP qvs0 + lxcbr0",
			ifaces: []netIface{fx(2, "eth0"), fx(3, "qvs0", "192.168.1.2"), fx(4, "lxcbr0", "10.0.3.1")},
			today:  "192.168.1.2", wantIP: "192.168.1.2", rule: "default", names: []string{"eth0", "qvs0"}},
		{row: "23 Unraid br0",
			ifaces: []netIface{fx(2, "eth0"), fx(3, "bond0"), fx(4, "br0", "192.168.1.30"), fx(5, "virbr0", "192.168.122.1"), fx(6, "shim-br0")},
			today:  "192.168.1.30", wantIP: "192.168.1.30", rule: "default", names: []string{"eth0", "bond0", "br0", "shim-br0"}},
		{row: "26 libvirt virbr0",
			ifaces: []netIface{fx(2, "eno1", "192.168.1.10"), fx(3, "virbr0", "192.168.122.1")},
			today:  "192.168.1.10", wantIP: "192.168.1.10", rule: "default", names: []string{"eno1"}},
		{row: "27 k3s",
			ifaces: []netIface{fx(2, "eth0", "192.168.1.40"), fx(3, "cni0", "10.42.0.1"), fx(4, "flannel.1", "10.42.0.0"), fx(5, "nodelocaldns", "169.254.20.10")},
			today:  "192.168.1.40", wantIP: "192.168.1.40", rule: "default", names: []string{"eth0", "nodelocaldns"}},
		{row: "29 OpenWrt #12: br-lan, guest and IoT bridges, public WAN",
			ifaces: []netIface{loIface(), fx(2, "eth0"), fx(3, "eth1"), fx(4, "eth3", "203.0.113.7"), fx(5, "br-guest", "192.168.3.1"), fx(6, "br-iot", "192.168.4.1"), fx(7, "br-lan", "192.168.1.1"), fx(8, "eth0.1"), fx(9, "eth0.3")},
			today:  "203.0.113.7", wantIP: "192.168.1.1", rule: "rule1", chosen: "br-lan",
			names: []string{"eth0", "eth1", "eth3", "br-lan", "eth0.1", "eth0.3"}},
		{row: "30 OpenWrt DSA",
			ifaces: []netIface{fx(2, "eth0"), fx(3, "lan1"), fx(4, "lan2"), fx(5, "wan", "203.0.113.7"), fx(6, "br-lan", "192.168.1.1")},
			today:  "203.0.113.7", wantIP: "192.168.1.1", rule: "rule1", chosen: "br-lan",
			names: []string{"eth0", "lan1", "lan2", "wan", "br-lan"}},
		{row: "31 OpenWrt DSA, VLAN filtering, IP on br-lan.1",
			ifaces: []netIface{fx(2, "lan1"), fx(3, "wan", "203.0.113.7"), fx(4, "br-lan"), fx(5, "br-lan.1", "192.168.1.1")},
			today:  "203.0.113.7", wantIP: "192.168.1.1", rule: "rule1", chosen: "br-lan.1",
			names: []string{"lan1", "wan", "br-lan.1"}},
		{row: "B1 OpenWrt: br-lan.1 10.x LAN, br-lan.3 192.168 guest",
			ifaces: []netIface{fx(2, "lan1"), fx(3, "wan", "203.0.113.7"), fx(4, "br-lan"), fx(5, "br-lan.3", "192.168.3.1"), fx(6, "br-lan.1", "10.0.0.1")},
			today:  "203.0.113.7", wantIP: "10.0.0.1", rule: "rule1", chosen: "br-lan.1",
			names: []string{"lan1", "wan", "br-lan.1"}},
		{row: "B1 OpenWrt: bare br-lan 10.x beats br-lan.3 192.168",
			ifaces: []netIface{fx(2, "lan1"), fx(3, "wan", "203.0.113.7"), fx(4, "br-lan.3", "192.168.3.1"), fx(5, "br-lan", "10.0.0.1")},
			today:  "203.0.113.7", wantIP: "10.0.0.1", rule: "rule1", chosen: "br-lan",
			names: []string{"lan1", "wan", "br-lan"}},
		{row: "B1 Proxmox: vmbr0 10.x LAN, vmbr0.30 192.168 IoT",
			ifaces: []netIface{fx(2, "eno1"), fx(3, "vmbr0.30", "192.168.30.5"), fx(4, "vmbr0", "10.0.0.5")},
			today:  "", wantIP: "10.0.0.5", rule: "rule1", chosen: "vmbr0",
			names: []string{"eno1", "vmbr0"}},
		{row: "32 OpenWrt PPPoE",
			ifaces: []netIface{fx(2, "eth0"), fx(3, "pppoe-wan", "203.0.113.7"), fx(4, "br-lan", "192.168.1.1")},
			today:  "203.0.113.7", wantIP: "192.168.1.1", rule: "rule1", chosen: "br-lan",
			names: []string{"eth0", "pppoe-wan", "br-lan"}},
		{row: "33/34 OpenWrt double NAT or downstream router (1b keeps today's)",
			ifaces: []netIface{fx(2, "wan", "192.168.0.2"), fx(3, "br-lan", "192.168.1.1")},
			today:  "192.168.0.2", wantIP: "192.168.0.2", rule: "default", names: []string{"wan"}},
		{row: "35 OpenWrt LAN 10.x, guest 192.168",
			ifaces: []netIface{fx(2, "wan", "203.0.113.7"), fx(3, "br-guest", "192.168.3.1"), fx(4, "br-lan", "10.0.0.1")},
			today:  "203.0.113.7", wantIP: "10.0.0.1", rule: "rule1", chosen: "br-lan",
			names: []string{"wan", "br-lan"}},
		{row: "36 OpenWrt dumb AP",
			ifaces: []netIface{fx(2, "lan1"), fx(3, "lan2"), fx(4, "br-lan", "192.168.1.2")},
			today:  "", wantIP: "192.168.1.2", rule: "rule1", chosen: "br-lan",
			names: []string{"lan1", "lan2", "br-lan"}},
		{row: "37 OpenWrt + dockerd",
			ifaces: []netIface{fx(2, "wan", "203.0.113.7"), fx(3, "br-lan", "192.168.1.1"), fx(4, "docker0", "172.17.0.1")},
			today:  "203.0.113.7", wantIP: "192.168.1.1", rule: "rule1", chosen: "br-lan",
			names: []string{"wan", "br-lan"}},
		{row: "39 Debian bridge named br-lan plus a NIC with an IP (1b keeps today's)",
			ifaces: []netIface{fx(2, "enp2s0", "10.0.0.9"), fx(3, "br-lan", "192.168.1.9")},
			today:  "10.0.0.9", wantIP: "10.0.0.9", rule: "default", names: []string{"enp2s0"}},
		{row: "40 generic router br0 + public enp1s0",
			ifaces: []netIface{fx(2, "enp1s0", "203.0.113.7"), fx(3, "br0", "192.168.1.1")},
			today:  "192.168.1.1", wantIP: "192.168.1.1", rule: "default", names: []string{"enp1s0", "br0"}},
		{row: "41 macOS",
			ifaces: []netIface{loIface(), fx(4, "en0", "192.168.1.50"), fx(5, "bridge100", "192.168.64.1"), fx(6, "utun0"), fx(7, "awdl0")},
			today:  "192.168.1.50", wantIP: "192.168.1.50", rule: "default", names: []string{"en0"}},
		{row: "42 Windows with WSL switch",
			ifaces: []netIface{fx(5, "Ethernet", "192.168.1.60"), fx(9, "vEthernet (WSL)", "172.20.0.1")},
			today:  "192.168.1.60", wantIP: "192.168.1.60", rule: "default", names: []string{"Ethernet"}},
		{row: "46 public only",
			ifaces: []netIface{fx(2, "eth0", "203.0.113.9")},
			today:  "203.0.113.9", wantIP: "203.0.113.9", rule: "default", names: []string{"eth0"}},
		{row: "47 IPv6 only",
			ifaces: []netIface{loIface(), fx(2, "eth0")},
			today:  "", wantIP: "", rule: "none", names: []string{"eth0"}},
		{row: "48 HAOS app container",
			ifaces: []netIface{loIface(), fx(2, "eth0", "172.30.33.3")},
			today:  "172.30.33.3", wantIP: "172.30.33.3", rule: "default", names: []string{"eth0"}},
		{row: "49 several VLANs",
			ifaces: []netIface{fx(2, "eth0", "192.168.1.10"), fx(3, "eth0.20", "10.20.0.5")},
			today:  "192.168.1.10", wantIP: "192.168.1.10", rule: "default", names: []string{"eth0", "eth0.20"}},
		{row: "50 link-local T1, vmbr0 private",
			ifaces: []netIface{fx(2, "eth0", "169.254.10.10"), fx(3, "vmbr0", "192.168.1.5")},
			today:  "169.254.10.10", wantIP: "192.168.1.5", rule: "rule1", chosen: "vmbr0",
			names: []string{"eth0", "vmbr0"}},
		{row: "51 link-local T1, ovs_eth0 private (rule 2 gated)",
			ifaces: []netIface{fx(2, "eth0", "169.254.10.10"), fx(3, "ovs_eth0", "192.168.1.20")},
			today:  "169.254.10.10", wantIP: "169.254.10.10", rule: "default", names: []string{"eth0"}},
		{row: "52/S7 every up interface skipped",
			ifaces: []netIface{loIface(), fx(2, "docker0", "172.17.0.1"), fx(3, "vmbr0", "192.168.1.5")},
			today:  "172.17.0.1", wantIP: "192.168.1.5", rule: "rule1", chosen: "vmbr0", names: nil},
		{row: "S7 every interface skipped, no address anywhere",
			ifaces: []netIface{loIface(), fx(2, "vmbr0"), fx(3, "tap100i0")},
			today:  "", wantIP: "", rule: "none", names: nil},
		{row: "S7 every up interface skipped, only Docker addresses",
			ifaces: []netIface{loIface(), fx(2, "docker0", "172.17.0.1"), fx(3, "br-1a2b3c4d5e6f", "172.18.0.1")},
			today:  "172.17.0.1", wantIP: "172.17.0.1", rule: "default", chosen: "docker0", names: nil},
		{row: "54 advertise_interface missing",
			ifaces:   []netIface{fx(2, "eth0", "192.168.1.10"), fx(3, "docker0", "172.17.0.1")},
			explicit: "eth9",
			today:    "192.168.1.10", wantIP: "192.168.1.10", rule: "explicit", names: nil},
		{row: "explicit skipped name (score 90 path)",
			ifaces:   []netIface{fx(2, "eth0", "192.168.1.10"), fx(3, "docker0", "172.17.0.1")},
			explicit: "docker0",
			today:    "172.17.0.1", wantIP: "172.17.0.1", rule: "explicit", names: []string{"docker0"}},
		{row: "down br-lan is ignored",
			ifaces: []netIface{fx(2, "wan", "203.0.113.7"), downIface(fx(3, "br-lan", "192.168.1.1"))},
			today:  "203.0.113.7", wantIP: "203.0.113.7", rule: "default", names: []string{"wan"}},
		{row: "br-lan with only CGNAT does not fire rule 1",
			ifaces: []netIface{fx(2, "wan", "203.0.113.7"), fx(3, "br-lan", "100.64.1.1")},
			today:  "203.0.113.7", wantIP: "203.0.113.7", rule: "default", names: []string{"wan"}},
		{row: "public 172.32 on T1 opens the gate",
			ifaces: []netIface{fx(2, "wan", "172.32.0.5"), fx(3, "br-lan", "192.168.1.1")},
			today:  "172.32.0.5", wantIP: "192.168.1.1", rule: "rule1", chosen: "br-lan",
			names: []string{"wan", "br-lan"}},
		{row: "CGNAT 100.64 on T1 opens the gate",
			ifaces: []netIface{fx(2, "wan", "100.72.1.5"), fx(3, "br-lan", "192.168.1.1")},
			today:  "100.72.1.5", wantIP: "192.168.1.1", rule: "rule1", chosen: "br-lan",
			names: []string{"wan", "br-lan"}},
		{row: "vmbr0v20 is not a LAN bridge",
			ifaces: []netIface{fx(2, "eno1", "203.0.113.5"), fx(3, "vmbr0v20", "192.168.20.5")},
			today:  "203.0.113.5", wantIP: "203.0.113.5", rule: "default", names: []string{"eno1"}},
		{row: "ovs-system alone gives nothing",
			ifaces: []netIface{fx(2, "eth0"), fx(3, "ovs-system")},
			today:  "", wantIP: "", rule: "none", names: []string{"eth0"}},
		{row: "rule 2 never takes a Docker network",
			ifaces: []netIface{fx(2, "eth0"), fx(3, "br-1a2b3c4d5e6f", "172.18.0.1"), fx(4, "docker0", "172.17.0.1")},
			today:  "", wantIP: "", rule: "none", names: []string{"eth0"}},
		{row: "rule 2 never takes link-local",
			ifaces: []netIface{fx(2, "eth0"), fx(3, "vmbr1", "169.254.1.1")},
			today:  "", wantIP: "", rule: "none", names: []string{"eth0"}},
	}
}

func TestSelectAdvertiseRows(t *testing.T) {
	for _, tc := range advRows() {
		t.Run(tc.row, func(t *testing.T) {
			p := selectAdvertise(envOf(tc.ifaces, tc.routes...), tc.explicit)
			_, oldIP := v082(tc.ifaces, tc.explicit)
			if oldIP != tc.today {
				t.Fatalf("fixture: v0.8.2 gives %q, row says %q", oldIP, tc.today)
			}
			if p.IP != tc.wantIP || p.Rule != tc.rule {
				t.Errorf("got ip=%q rule=%s, want ip=%q rule=%s (%s)", p.IP, p.Rule, tc.wantIP, tc.rule, p.LogLine())
			}
			if tc.chosen != "" && p.Chosen != tc.chosen {
				t.Errorf("chosen = %q, want %q", p.Chosen, tc.chosen)
			}
			if got := planNames(p); !reflect.DeepEqual(got, tc.names) {
				t.Errorf("interfaces = %#v, want %#v", got, tc.names)
			}
		})
	}
}

// Variant 1b's promise: a host whose ip= today is an RFC1918 address keeps
// the same ip= and the same interface list. The one deliberate exception
// is review S7: when every up interface is skipped, today's address comes
// from a skipped interface at score 90 (often docker0), and rule 1 may
// replace it.
func TestSelectAdvertiseKeepsRFC1918Today(t *testing.T) {
	for _, tc := range advRows() {
		oldNames, oldIP := v082(tc.ifaces, tc.explicit)
		if oldIP == "" || !isRFC1918(net.ParseIP(oldIP)) {
			continue
		}
		if tc.explicit == "" && oldNames == nil {
			continue // S7, checked in TestSelectAdvertiseRows
		}
		p := selectAdvertise(envOf(tc.ifaces, tc.routes...), tc.explicit)
		if p.IP != oldIP || !reflect.DeepEqual(planNames(p), oldNames) {
			t.Errorf("%s: changed from %q %v to %q %v", tc.row, oldIP, oldNames, p.IP, planNames(p))
		}
	}
}

// Whenever rule 1 and rule 2 do not fire, the output equals v0.8.2's, and
// rule 2 only ever fires where v0.8.2 had no ip=.
func TestSelectAdvertiseInvariant(t *testing.T) {
	for _, tc := range advRows() {
		oldNames, oldIP := v082(tc.ifaces, tc.explicit)
		p := selectAdvertise(envOf(tc.ifaces, tc.routes...), tc.explicit)
		switch p.Rule {
		case "default", "explicit", "none":
			if p.IP != oldIP || !reflect.DeepEqual(planNames(p), oldNames) {
				t.Errorf("%s: rule %s changed %q %v to %q %v", tc.row, p.Rule, oldIP, oldNames, p.IP, planNames(p))
			}
		case "rule2":
			if oldIP != "" {
				t.Errorf("%s: rule 2 replaced %q", tc.row, oldIP)
			}
		}
	}
}

// The log lines for OpenWrt #12: br-lan picked, br-guest (lower ifindex)
// stays skipped, the WAN stays in the list.
func TestSelectAdvertiseOpenWrtLogShape(t *testing.T) {
	var row advRow
	for _, r := range advRows() {
		if strings.HasPrefix(r.row, "29 ") {
			row = r
		}
	}
	p := selectAdvertise(envOf(row.ifaces), "")
	wantDesc := "eth0, eth1, eth3, br-lan [rule 1], eth0.1, eth0.3 (skipped: br-guest, br-iot)"
	if p.Desc != wantDesc {
		t.Errorf("desc = %q\nwant   %q", p.Desc, wantDesc)
	}
	wantLog := "mDNS: preferred IP: 192.168.1.1 (rule 1: LAN bridge br-lan; default scoring gave 203.0.113.7 on eth3)"
	if p.LogLine() != wantLog {
		t.Errorf("log = %q\nwant  %q", p.LogLine(), wantLog)
	}
}

func TestSelectAdvertiseLogLines(t *testing.T) {
	cases := []struct {
		ifaces   []netIface
		routes   []string
		explicit string
		want     string
	}{
		{[]netIface{fx(2, "enp3s0", "192.168.1.10")}, nil, "",
			"mDNS: preferred IP: 192.168.1.10 (default scoring, enp3s0)"},
		{[]netIface{fx(2, "enp3s0"), fx(3, "vmbr0", "192.168.20.10")}, nil, "vmbr0",
			"mDNS: preferred IP: 192.168.20.10 (advertise_interface vmbr0)"},
		{[]netIface{fx(2, "enp3s0"), fx(3, "vmbr0", "192.168.1.5")}, nil, "",
			"mDNS: preferred IP: 192.168.1.5 (rule 1: LAN bridge vmbr0; the other interfaces have no IPv4)"},
		{[]netIface{fx(2, "vmbr0", "192.168.1.5")}, nil, "",
			"mDNS: preferred IP: 192.168.1.5 (rule 1: LAN bridge vmbr0; every interface is skipped)"},
		{[]netIface{fx(2, "eno1"), fx(3, "vmbr1", "10.10.10.1")}, nil, "",
			"mDNS: preferred IP: 10.10.10.1 (rule 2: vmbr1, no IPv4 on the other interfaces)"},
		{[]netIface{fx(2, "eno1"), fx(3, "vmbr0", "198.51.100.5")}, []string{"vmbr0"}, "",
			"mDNS: preferred IP: 198.51.100.5 (rule 2: vmbr0 (default route), no IPv4 on the other interfaces)"},
		{[]netIface{fx(2, "eth0")}, nil, "",
			"mDNS: no IPv4 address for the TXT record (no usable IPv4 on any interface)"},
	}
	for _, tc := range cases {
		p := selectAdvertise(envOf(tc.ifaces, tc.routes...), tc.explicit)
		if p.LogLine() != tc.want {
			t.Errorf("got  %q\nwant %q", p.LogLine(), tc.want)
		}
	}
}

func TestIsDockerBridgeName(t *testing.T) {
	cases := map[string]bool{
		"br-2a721a667966":  true,
		"BR-2A721A667966":  true,
		"br-2a721a66796":   false,
		"br-2a721a6679667": false,
		"br-lan":           false,
		"br-media":         false,
	}
	for name, want := range cases {
		if got := isDockerBridgeName(name); got != want {
			t.Errorf("%s: got %v, want %v", name, got, want)
		}
	}
}

func TestLANBridgeRank(t *testing.T) {
	yes := map[string]int{"br-lan": -1, "vmbr0": -1, "BR-LAN": -1, "br-lan.1": 1, "vmbr0.20": 20}
	for name, want := range yes {
		got, ok := lanBridgeRank(name)
		if !ok || got != want {
			t.Errorf("%s: got %d %v, want %d true", name, got, ok, want)
		}
	}
	for _, name := range []string{"vmbr0v20", "vmbr1", "br-lan0", "br-guest", "br-lan.", "br-lan.x", "eth0"} {
		if _, ok := lanBridgeRank(name); ok {
			t.Errorf("%s: should not be a LAN bridge", name)
		}
	}
}

func TestIsFallbackInterface(t *testing.T) {
	for _, name := range []string{"vmbr0", "vmbr1", "ovs_eth0", "ovs_bond0", "br-media", "br-lan"} {
		if !isFallbackInterface(name) {
			t.Errorf("%s: should be a fallback interface", name)
		}
	}
	for _, name := range []string{"ovs-system", "br-2a721a667966", "docker0", "eth0", "virbr0", "br0"} {
		if isFallbackInterface(name) {
			t.Errorf("%s: should not be a fallback interface", name)
		}
	}
}

func TestExactScore(t *testing.T) {
	cases := []struct {
		ip    string
		score int
		ok    bool
	}{
		{"192.168.1.1", 10, true},
		{"10.1.2.3", 20, true},
		{"172.16.0.1", 50, true},
		{"172.31.255.1", 50, true},
		{"172.32.0.1", 80, true},
		{"100.64.0.1", 70, true},
		{"100.127.255.1", 70, true},
		{"100.128.0.1", 80, true},
		{"203.0.113.1", 80, true},
		{"169.254.1.1", 0, false},
		{"127.0.0.1", 0, false},
		{"fe80::1", 0, false},
	}
	for _, tc := range cases {
		s, ok := exactScore(net.ParseIP(tc.ip))
		if s != tc.score || ok != tc.ok {
			t.Errorf("%s: got %d %v, want %d %v", tc.ip, s, ok, tc.score, tc.ok)
		}
	}
}

func TestParseProcNetRoute(t *testing.T) {
	route := "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n" +
		"vmbr0\t00000000\t0171A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n" +
		"vmbr0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n" +
		"vmbr1\t000A0A0A\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n" +
		"wg0\t00000000\t00000000\t0000\t0\t0\t0\t00000000\t0\t0\t0\n"
	got := parseProcNetRoute(route)
	if !reflect.DeepEqual(got, map[string]bool{"vmbr0": true}) {
		t.Errorf("got %v", got)
	}
}
