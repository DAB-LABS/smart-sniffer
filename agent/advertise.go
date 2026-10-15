package main

// mDNS address selection.
//
// The agent advertises _smartha._tcp on a list of interfaces and puts one
// IPv4 address in the TXT record as ip=. Home Assistant connects to that
// address, so picking the wrong one (or none) breaks discovery.
//
// Order of selection (see selectAdvertise):
//
//  1. advertise_interface / --interface, when set, always wins. Same result
//     as v0.8.2.
//  2. Default scoring over the interfaces that are up, not loopback and not
//     on the skip list (T1). Unchanged from v0.8.2, and it decides whenever
//     it picks an RFC1918 address.
//  3. Rule 1: when default scoring picks a non-RFC1918 address (public,
//     CGNAT, link-local, 172.32+ and so on) or nothing, and a LAN bridge
//     named br-lan or vmbr0 has an RFC1918 address, that bridge is used.
//     The bare name wins; otherwise the VLAN subinterface (br-lan.N,
//     vmbr0.N) with the lowest N.
//  4. Default scoring, when rule 1 did not fire and it found something.
//  5. Rule 2: when T1 has no IPv4 address at all, a skipped bridge that is
//     not Docker's (vmbr*, ovs_*, br-<name>) supplies the address. The one
//     carrying the default route wins, then exact scoring. Never link-local.

import (
	"fmt"
	"net"
	"os"
	"regexp"
	"strconv"
	"strings"
)

// netIface is one interface as address selection sees it. Tests build these
// by hand.
type netIface struct {
	Iface net.Interface // handed to zeroconf unchanged
	IPv4  []net.IP      // IPv4 addresses in Addrs() order, loopback addresses excluded
}

func (n netIface) up() bool       { return n.Iface.Flags&net.FlagUp != 0 }
func (n netIface) loopback() bool { return n.Iface.Flags&net.FlagLoopback != 0 }

// netEnv is what selection reads from the system. Replaced in tests.
type netEnv struct {
	list         func() ([]netIface, error)
	defaultRoute func() map[string]bool // interface names carrying an IPv4 default route
}

var systemNetEnv = netEnv{list: systemInterfaces, defaultRoute: systemDefaultRouteIfaces}

// systemInterfaces enumerates every interface, including down ones, the
// way v0.8.2's PreferredIP(nil) saw them.
func systemInterfaces() ([]netIface, error) {
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil, err
	}
	out := make([]netIface, 0, len(ifaces))
	for _, iface := range ifaces {
		n := netIface{Iface: iface}
		if addrs, err := iface.Addrs(); err == nil {
			for _, addr := range addrs {
				var ip net.IP
				switch v := addr.(type) {
				case *net.IPNet:
					ip = v.IP
				case *net.IPAddr:
					ip = v.IP
				}
				if ip == nil || ip.IsLoopback() || ip.To4() == nil {
					continue
				}
				n.IPv4 = append(n.IPv4, ip.To4())
			}
		}
		out = append(out, n)
	}
	return out, nil
}

// systemDefaultRouteIfaces reads /proc/net/route (Linux only; elsewhere the
// file is missing and the result is empty). Rule 2 only ever considers
// Linux bridge names, so no other platform needs it.
func systemDefaultRouteIfaces() map[string]bool {
	data, err := os.ReadFile("/proc/net/route")
	if err != nil {
		return nil
	}
	return parseProcNetRoute(string(data))
}

// parseProcNetRoute returns the interfaces with an up IPv4 route whose
// destination and mask are both 0.0.0.0.
func parseProcNetRoute(s string) map[string]bool {
	out := map[string]bool{}
	for i, line := range strings.Split(s, "\n") {
		f := strings.Fields(line)
		if i == 0 || len(f) < 8 {
			continue
		}
		flags, err := strconv.ParseUint(f[3], 16, 32)
		if err != nil || flags&0x1 == 0 {
			continue
		}
		if f[1] == "00000000" && f[7] == "00000000" {
			out[f[0]] = true
		}
	}
	return out
}

// isSkippedInterface is the skip-list test: a case-insensitive prefix match
// against defaultSkipPrefixes. It is the only copy.
func isSkippedInterface(name string) bool {
	lower := strings.ToLower(name)
	for _, p := range defaultSkipPrefixes {
		if strings.HasPrefix(lower, p) {
			return true
		}
	}
	return false
}

var dockerBridgeRe = regexp.MustCompile(`^br-[0-9a-f]{12}$`)

// isDockerBridgeName reports a Docker user network bridge: br- plus exactly
// 12 hex characters.
func isDockerBridgeName(name string) bool {
	return dockerBridgeRe.MatchString(strings.ToLower(name))
}

var lanVLANRe = regexp.MustCompile(`^(br-lan|vmbr0)\.([0-9]+)$`)

// lanBridgeRank classifies a name for rule 1. ok is false for anything that
// is not br-lan, vmbr0 or one of their VLAN subinterfaces. vlan is -1 for
// the bare bridge, otherwise the VLAN number N.
func lanBridgeRank(name string) (vlan int, ok bool) {
	lower := strings.ToLower(name)
	if lower == "br-lan" || lower == "vmbr0" {
		return -1, true
	}
	m := lanVLANRe.FindStringSubmatch(lower)
	if m == nil {
		return 0, false
	}
	n, err := strconv.Atoi(m[2])
	if err != nil {
		return 0, false
	}
	return n, true
}

// isFallbackInterface: a skipped interface that rule 2 may take the address
// from. vmbr*, ovs_* (not ovs-system), and br-* that is not a Docker
// network.
func isFallbackInterface(name string) bool {
	if !isSkippedInterface(name) {
		return false
	}
	lower := strings.ToLower(name)
	switch {
	case strings.HasPrefix(lower, "vmbr"), strings.HasPrefix(lower, "ovs_"):
		return true
	case strings.HasPrefix(lower, "br-"):
		return !isDockerBridgeName(lower)
	}
	return false
}

func mustCIDR(s string) *net.IPNet {
	_, n, err := net.ParseCIDR(s)
	if err != nil {
		panic(err)
	}
	return n
}

var (
	net10       = mustCIDR("10.0.0.0/8")
	net172      = mustCIDR("172.16.0.0/12")
	net192      = mustCIDR("192.168.0.0/16")
	netCGNAT    = mustCIDR("100.64.0.0/10")
	netLinkLoc4 = mustCIDR("169.254.0.0/16")
)

// isRFC1918 reports 10/8, 172.16/12 and 192.168/16, by exact range.
func isRFC1918(ip net.IP) bool {
	ip4 := ip.To4()
	if ip4 == nil {
		return false
	}
	return net10.Contains(ip4) || net172.Contains(ip4) || net192.Contains(ip4)
}

// exactScore ranks an address for rules 1 and 2. Lower is better.
// Link-local, loopback and non-IPv4 are never candidates.
func exactScore(ip net.IP) (int, bool) {
	ip4 := ip.To4()
	if ip4 == nil || ip4.IsLoopback() || netLinkLoc4.Contains(ip4) {
		return 0, false
	}
	switch {
	case net192.Contains(ip4):
		return 10, true
	case net10.Contains(ip4):
		return 20, true
	case net172.Contains(ip4):
		return 50, true
	case netCGNAT.Contains(ip4):
		return 70, true
	}
	return 80, true
}

// legacyPick is v0.8.2's PreferredIP scoring, unchanged: string prefixes,
// skipped names at 90, first candidate wins a tie. It returns the address
// and the interface it came from.
func legacyPick(ifaces []netIface) (ip, from string) {
	best := -1
	for _, n := range ifaces {
		virtual := isSkippedInterface(n.Iface.Name)
		for _, a := range n.IPv4 {
			s := a.String()
			score := 80
			switch {
			case virtual:
				score = 90
			case strings.HasPrefix(s, "192.168."):
				score = 10
			case strings.HasPrefix(s, "10."):
				score = 20
			case strings.HasPrefix(s, "172."):
				score = 50
			case strings.HasPrefix(s, "100."):
				score = 70
			}
			if best < 0 || score < best {
				best, ip, from = score, s, n.Iface.Name
			}
		}
	}
	return ip, from
}

// advertisePlan is the outcome of address selection.
type advertisePlan struct {
	Ifaces []net.Interface // nil = zeroconf uses every multicast interface
	IP     string          // "" = no ip= TXT field
	Rule   string          // explicit | rule1 | default | rule2 | none
	Chosen string          // interface that supplied IP, if known
	Desc   string          // text for the "mDNS: interfaces:" log line
	Reason string          // text for the "mDNS: preferred IP:" log line
}

// LogLine is the single line saying which rule chose the address.
func (p advertisePlan) LogLine() string {
	if p.IP == "" {
		return "mDNS: no IPv4 address for the TXT record (" + p.Reason + ")"
	}
	return "mDNS: preferred IP: " + p.IP + " (" + p.Reason + ")"
}

// ResolveAdvertise picks the interfaces and the ip= address for mDNS.
func (c *Config) ResolveAdvertise() advertisePlan {
	return selectAdvertise(systemNetEnv, c.AdvertiseInterface)
}

func selectAdvertise(env netEnv, explicit string) advertisePlan {
	all, err := env.list()

	// --- Explicit interface: same as v0.8.2. ---
	if explicit != "" {
		for _, n := range all {
			if n.Iface.Name == explicit {
				ip, _ := legacyPick([]netIface{n})
				return advertisePlan{
					Ifaces: []net.Interface{n.Iface},
					IP:     ip,
					Rule:   "explicit",
					Chosen: explicit,
					Desc:   "interface " + explicit,
					Reason: "advertise_interface " + explicit,
				}
			}
		}
		ip, from := legacyPick(all)
		return advertisePlan{
			IP:     ip,
			Rule:   "explicit",
			Chosen: from,
			Desc:   fmt.Sprintf("WARNING: interface %q not found, advertising on all", explicit),
			Reason: fmt.Sprintf("advertise_interface %s not found, default scoring over all interfaces, %s", explicit, from),
		}
	}

	if err != nil {
		return advertisePlan{Rule: "none", Desc: "all interfaces (could not enumerate)", Reason: "could not enumerate interfaces"}
	}

	// T1: today's filtered list.
	var t1 []netIface
	var skipped []string
	for _, n := range all {
		if !n.up() || n.loopback() {
			continue
		}
		if isSkippedInterface(n.Iface.Name) {
			skipped = append(skipped, n.Iface.Name)
			continue
		}
		t1 = append(t1, n)
	}

	// Today's scoring over T1. An empty T1 counts as "nothing" here (the
	// v0.8.2 fallback to all interfaces is applied later, in step 4), so
	// rule 1 may fire on a host where every up interface is skipped.
	var gateIP, gateFrom string
	if len(t1) > 0 {
		gateIP, gateFrom = legacyPick(t1)
	}

	// --- Rule 1: only when today's pick is not RFC1918 (or is nothing).
	// Legacy scoring classifies by string prefix, but this gate tests the
	// chosen address by exact range, so public 172.x, 172.32+ and 100.x all
	// count as not RFC1918 and open the gate.
	if gateIP == "" || !isRFC1918(net.ParseIP(gateIP)) {
		if c, ip := pickLANBridge(all); ip != "" {
			why := "the other interfaces have no IPv4"
			if gateIP != "" {
				why = "default scoring gave " + gateIP + " on " + gateFrom
			} else if len(t1) == 0 {
				why = "every interface is skipped"
			}
			return finishPlan(t1, skipped, all, c, ip, "rule1",
				"rule 1: LAN bridge "+c+"; "+why)
		}
	}

	// --- Default: v0.8.2's result. ---
	ip, from, scope := gateIP, gateFrom, ""
	if len(t1) == 0 {
		ip, from = legacyPick(all)
		scope = " over all interfaces"
	}
	if ip != "" {
		p := finishPlan(t1, skipped, all, "", ip, "default", "default scoring"+scope+", "+from)
		p.Chosen = from
		return p
	}

	// --- Rule 2: the non-skipped interfaces have no IPv4 at all. ---
	if c, ip, viaRoute := pickFallbackBridge(all, env.defaultRoute); ip != "" {
		route := ""
		if viaRoute {
			route = " (default route)"
		}
		return finishPlan(t1, skipped, all, c, ip, "rule2",
			"rule 2: "+c+route+", no IPv4 on the other interfaces")
	}

	return finishPlan(t1, skipped, all, "", "", "none", "no usable IPv4 on any interface")
}

// pickLANBridge implements rule 1's candidate choice: the bare br-lan or
// vmbr0 with an RFC1918 address (enumeration order between the two),
// otherwise the br-lan.N / vmbr0.N with the lowest N. Score order never
// ranks one bridge against another; it only picks among one interface's
// own addresses.
func pickLANBridge(all []netIface) (name, ip string) {
	bestVLAN := 0
	for _, n := range all {
		if !n.up() || n.loopback() {
			continue
		}
		vlan, ok := lanBridgeRank(n.Iface.Name)
		if !ok {
			continue
		}
		addr := bestAddr(n, true)
		if addr == "" {
			continue
		}
		if name == "" || vlan < bestVLAN {
			name, ip, bestVLAN = n.Iface.Name, addr, vlan
		}
	}
	return name, ip
}

// pickFallbackBridge implements rule 2: among up fallback interfaces with a
// usable IPv4, the ones carrying the default route first, then exact
// score, then enumeration order.
func pickFallbackBridge(all []netIface, defaultRoute func() map[string]bool) (name, ip string, viaRoute bool) {
	var routes map[string]bool
	if defaultRoute != nil {
		routes = defaultRoute()
	}
	bestRoute, bestScore := false, 0
	for _, n := range all {
		if !n.up() || n.loopback() || !isFallbackInterface(n.Iface.Name) {
			continue
		}
		addr := bestAddr(n, false)
		if addr == "" {
			continue
		}
		score, _ := exactScore(net.ParseIP(addr))
		route := routes[n.Iface.Name]
		better := name == "" ||
			(route && !bestRoute) ||
			(route == bestRoute && score < bestScore)
		if better {
			name, ip, bestRoute, bestScore = n.Iface.Name, addr, route, score
		}
	}
	return name, ip, bestRoute
}

// bestAddr returns the interface's lowest exactScore address (first wins a
// tie). With rfc1918Only, other addresses are ignored.
func bestAddr(n netIface, rfc1918Only bool) string {
	best, out := 0, ""
	for _, a := range n.IPv4 {
		if rfc1918Only && !isRFC1918(a) {
			continue
		}
		s, ok := exactScore(a)
		if !ok {
			continue
		}
		if out == "" || s < best {
			best, out = s, a.String()
		}
	}
	return out
}

// finishPlan builds the interface list handed to zeroconf: T1 plus the
// chosen interface (if any), in enumeration order. When T1 is empty the
// list stays nil, which is v0.8.2's "every multicast interface" fallback.
//
// What zeroconf (grandcat v1.0.0) then announces: it joins 224.0.0.251 on
// each listed interface, and it collects the addresses of ALL listed
// interfaces into one A/AAAA set that it sends in every response and
// announcement on every listed interface. So adding br-lan also announces
// br-lan's addresses on the other listed interfaces (on OpenWrt, the WAN),
// just as the WAN address already went out on the LAN in v0.8.2. The list
// is not a containment boundary either: the socket listens on 0.0.0.0:5353.
func finishPlan(t1 []netIface, skipped []string, all []netIface, chosen, ip, rule, reason string) advertisePlan {
	p := advertisePlan{IP: ip, Rule: rule, Chosen: chosen, Reason: reason}
	if len(t1) == 0 {
		p.Desc = "all interfaces (auto-filter found none)"
		return p
	}
	tag := map[string]string{"rule1": " [rule 1]", "rule2": " [rule 2]"}[rule]
	var names []string
	for _, n := range all {
		inT1 := false
		for _, t := range t1 {
			if t.Iface.Name == n.Iface.Name {
				inT1 = true
				break
			}
		}
		switch {
		case inT1:
			p.Ifaces = append(p.Ifaces, n.Iface)
			names = append(names, n.Iface.Name)
		case chosen != "" && n.Iface.Name == chosen:
			p.Ifaces = append(p.Ifaces, n.Iface)
			names = append(names, n.Iface.Name+tag)
		}
	}
	p.Desc = strings.Join(names, ", ")
	var rest []string
	for _, s := range skipped {
		if s != chosen {
			rest = append(rest, s)
		}
	}
	if len(rest) > 0 {
		p.Desc += " (skipped: " + strings.Join(rest, ", ") + ")"
	}
	return p
}
