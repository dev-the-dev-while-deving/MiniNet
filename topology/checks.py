"""checks.py: health checks for the running Fortress-Net lab.

Each check prints ✅ or ❌ with a short reason, so the output doubles as evidence for the
report. run_all() runs every check that applies to what the lab currently has and returns
the number of failures. fortress_topo.py runs it after start-up (and exits with it in
--check mode); start_all.sh shows it before opening the CLI.
"""
import re
import time

from mininet.log import output


class Results:
    """Collects check results and prints them as they come in."""

    def __init__(self):
        self.failed = 0
        self.passed = 0

    def ok(self, label, detail=""):
        self.passed += 1
        output(f"  ✅ {label}  {detail}\n")

    def bad(self, label, detail=""):
        self.failed += 1
        output(f"  ❌ {label}  {detail}\n")

    def check(self, condition, label, detail=""):
        (self.ok if condition else self.bad)(label, detail)
        return condition


def ping(src, dst_ip, count=1, wait=1, tries=3):
    """True if src can ping dst_ip. Retries a few times, because right after start-up
    the first packet can be lost while ARP and the switches' MAC tables fill in."""
    for _ in range(tries):
        out = src.cmd(f"ping -n -c {count} -W {wait} {dst_ip}")
        if re.search(r" 0% packet loss", out):
            return True
    return False


def arping(src, dst_ip, intf=None):
    """True if dst_ip answers an ARP request from src, i.e. both are in the same layer-2 VLAN.
    ARP never crosses a router, so this tests the VLAN itself, not routing."""
    intf = intf or src.defaultIntf()
    out = src.cmd(f"arping -c 2 -w 2 -I {intf} {dst_ip}")
    return bool(re.search(r"Received [1-9]", out))


# --- T03: switching, VLANs and loops -----------------------------------------
def check_same_zone(net, lab, r):
    """Every pair of hosts in the same zone can reach each other (layer 2)."""
    output("== Same-zone reachability (VLAN switching)\n")
    for z in lab.zones.values():
        names = list(z["hosts"])
        if len(names) < 2:
            r.ok(f"{z['title']}", "(only one host in this zone, nothing to pair)")
            continue
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                dst = lab.hosts[b]["ip"]
                r.check(arping(net[a], dst) and ping(net[a], dst), f"{a} -> {b}", dst)
    fob = [h for h in lab.hosts.values() if h["zone"] == "fob"]
    if len(fob) >= 2:
        a, b = fob[0], fob[1]
        r.check(ping(net[a["name"]], b["ip"]), f"{a['name']} -> {b['name']}", "(FOB LAN)")


def check_vlan_isolation(net, lab, r):
    """Two hosts on the SAME access switch but in DIFFERENT VLANs must not see each other's ARP.
    This is what 802.1Q tagging buys us: one physical switch, separate broadcast domains."""
    output("== VLAN isolation on a shared switch\n")
    by_switch = {}
    for h in lab.hosts.values():
        if h["vlan"] is not None:
            by_switch.setdefault(h["switch"], {}).setdefault(h["vlan"], h)
    tested = False
    for sw, per_vlan in by_switch.items():
        if len(per_vlan) < 2:
            continue
        a, b = list(per_vlan.values())[:2]
        r.check(not arping(net[a["name"]], b["ip"]),
                f"{a['name']} (VLAN {a['vlan']}) cannot ARP {b['name']} (VLAN {b['vlan']})", f"both on {sw}")
        tested = True
    if not tested:
        r.ok("VLAN isolation", "(no switch has two VLANs in this lab size)")


def check_no_storm(net, lab, r, seconds=3, limit=300):
    """Loops without spanning tree cause a broadcast storm: the cores would receive packets
    non-stop even with nobody talking. Count packets on the cores while the lab is idle."""
    output("== No broadcast storm (RSTP or controller is breaking the loops)\n")

    def rx_total():
        total = 0
        for c in lab.core_switches:
            out = net[c].cmd(f"ovs-ofctl -O OpenFlow13 dump-ports {c}")
            total += sum(int(n) for n in re.findall(r"rx pkts=(\d+)", out))
        return total

    before = rx_total()
    time.sleep(seconds)
    received = rx_total() - before
    r.check(received < limit, "core switches are quiet",
            f"{received} packets in {seconds} s while idle (limit {limit})")


def check_rstp(net, lab, r):
    """Standalone mode only: RSTP has elected c1 as the root and is blocking some ports."""
    output("== RSTP\n")
    c1 = lab.core_switches[0]
    out = net[c1].cmd(f"ovs-appctl rstp/show {c1}")
    r.check("This bridge is the root" in out, f"{c1} is the RSTP root bridge")
    blocked = 0
    for s in lab.access_switches:
        out = net[s].cmd(f"ovs-appctl rstp/show {s}")
        blocked += len(re.findall(r"Alternate|Backup", out))
    r.check(blocked >= 1, "redundant uplinks are blocked", f"{blocked} alternate/backup ports")


# --- T04: DHCP and DNS ----------------------------------------------------------
def check_dhcp(net, lab, r):
    """Every DHCP host got its reserved address, in its own zone's subnet."""
    output("== DHCP (dnsmasq on srv1, relayed by r1/r2)\n")
    for h in lab.hosts.values():
        if not h["dhcp"]:
            continue
        out = net[h["name"]].cmd(f"ip -4 -o addr show dev {h['name']}-eth0")
        m = re.search(r"inet (\S+)", out)
        got = m.group(1) if m else "no address"
        want = f"{h['ip']}/{h['prefixlen']}"
        r.check(got == want, f"{h['name']} got {got}", f"(expected {want}, zone {h['zone']})")


def check_dns(net, lab, r):
    """Every DHCP host resolves srv1.<domain> using the DNS server its lease told it about."""
    output(f"== DNS (srv1.{lab.domain} via the DNS server from each lease)\n")
    srv = lab.host_with_role("dhcp-dns")
    name = f"{srv['name']}.{lab.domain}"
    for h in lab.hosts.values():
        if not h["dhcp"]:
            continue
        resolv = net[h["name"]].cmd(f"cat /var/run/fortress/{h['name']}/resolv.conf 2>/dev/null")
        m = re.search(r"nameserver (\S+)", resolv)
        if not m:
            r.bad(f"{h['name']} resolves {name}", "(no DNS server in its lease)")
            continue
        answer = net[h["name"]].cmd(f"dig +short +time=2 +tries=2 {name} @{m.group(1)}").strip()
        r.check(answer == srv["ip"], f"{h['name']} resolves {name}", f"-> {answer or 'nothing'} via {m.group(1)}")


def run_all(net, lab, args):
    """Run every check that fits what the lab currently has. Returns the number of failures."""
    r = Results()
    output("\n*** Fortress-Net health checks\n")
    if args.standalone:
        check_rstp(net, lab, r)
    check_same_zone(net, lab, r)
    check_vlan_isolation(net, lab, r)
    check_no_storm(net, lab, r)
    check_dhcp(net, lab, r)
    check_dns(net, lab, r)
    output(f"*** {r.passed} passed, {r.failed} failed\n\n")
    return r.failed
