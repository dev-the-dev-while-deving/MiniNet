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


# --- T05: routing ---------------------------------------------------------------
def check_ospf(net, lab, r):
    """Every router has a Full OSPF adjacency with each router it shares a link with."""
    import services
    output("== OSPF adjacencies (area 0)\n")
    rid = {name: info["router_id"] for name, info in lab.routers.items()}
    for router, ifaces in lab.router_ifaces.items():
        states = services.ospf_neighbors(net, router)
        for i in ifaces:
            state = states.get(rid[i["peer"]], "missing")
            r.check(state.startswith("Full"), f"{router} <-> {i['peer']}", f"{state} on {i['name']}")


def check_reachability(net, lab, r):
    """Command Center reaches the FOB (r3 and its LAN) and the ISP/internet (r4 and www1)."""
    output("== Routing between zones, the FOB and the internet\n")
    src = net[next(h for h in lab.zones["command"]["hosts"])]
    r3, r4 = lab.router_with_role("fob"), lab.router_with_role("isp")
    targets = [(f"{r3} (FOB gateway)", lab.fob["gateway"]), (f"{r4} (ISP)", lab.internet["gateway"])]
    targets += [(h["name"], h["ip"]) for h in lab.hosts.values() if h["zone"] in ("fob", "internet")]
    for name, ip in targets:
        r.check(ping(src, ip), f"{src.name} -> {name}", ip)
    srv = lab.host_with_role("dhcp-dns")
    fob1 = next(h for h in lab.hosts.values() if h["zone"] == "fob")
    r.check(ping(net[srv["name"]], fob1["ip"]), f"{srv['name']} -> {fob1['name']}", "(Servers zone to the FOB)")


def check_primary_path(net, lab, r):
    """traceroute from Command to the FOB goes r1 -> r3 (the cost-10 primary WAN), not via r2."""
    output("== Primary WAN path (OSPF costs)\n")
    src = net[next(h for h in lab.zones["command"]["hosts"])]
    fob1 = next(h for h in lab.hosts.values() if h["zone"] == "fob")
    primary = next(l for l in lab.links if "primary" in l["purpose"])
    r3_ip = primary["b_ip"] if primary["b"] == lab.router_with_role("fob") else primary["a_ip"]
    out = src.cmd(f"traceroute -n -q 1 -w 1 -m 4 {fob1['ip']}")
    hops = re.findall(r"^\s*\d+\s+(\S+)", out, re.M)
    r.check(len(hops) >= 2 and hops[1] == r3_ip, f"{src.name} -> {fob1['name']} via r1 then r3",
            f"hops: {' '.join(hops) or 'none'} (r3 on the primary link is {r3_ip})")


def check_backup_route(net, lab, r):
    """r2's floating static route to the FOB is installed but not used while OSPF has a route."""
    import services
    output("== Floating static route (backup to the FOB)\n")
    for router, routes in lab.backup_static_routes.items():
        for s in routes:
            entries = services.vtysh_json(net, router, f"show ip route {s['to']} json").get(s["to"], [])
            static = [e for e in entries if e.get("protocol") == "static"]
            chosen = [e["protocol"] for e in entries if e.get("selected")]
            r.check(static and static[0].get("distance") == s["distance"] and chosen == ["ospf"],
                    f"{router}: static {s['to']} via {s['via']} (distance {s['distance']}) is standby",
                    f"selected now: {', '.join(chosen) or 'none'}")


def check_nat(net, lab, r):
    """Barracks reaches the internet, and the gateway's NAT rule translated that traffic."""
    output("== NAT towards the internet\n")
    src = net[next(h for h in lab.zones["barracks"]["hosts"])] if "barracks" in lab.zones else None
    www = next(h for h in lab.hosts.values() if h["zone"] == "internet")
    if src is None:
        r.ok("NAT", "(no Barracks zone in this lab size)")
        return
    r.check(ping(src, www["ip"], count=3), f"{src.name} -> {www['name']}", www["ip"])
    total = 0
    for g in lab.gateway_routers():
        out = net[g].cmd("iptables -t nat -L POSTROUTING -n -v -x")
        total += sum(int(n) for n in re.findall(r"^\s*(\d+)\s+\d+\s+MASQUERADE", out, re.M))
    r.check(total > 0, "MASQUERADE rule translated packets", f"{total} packets on r1/r2")


# --- T06: VRRP ------------------------------------------------------------------
def check_vrrp(net, lab, r):
    """r1 (priority 120) is Master and r2 (100) Backup for every zone, and hosts reach the
    gateway address through the VRRP virtual MAC, not a real router's MAC."""
    import services
    output("== VRRP gateway redundancy\n")
    g1, g2 = lab.gateway_routers()
    s1, s2 = services.vrrp_status(net, g1), services.vrrp_status(net, g2)
    for z in lab.zones.values():
        v = z["vlan"]
        r.check(s1.get(v) == "Master" and s2.get(v) == "Backup", f"VRRP {v} ({z['title']}): {g1} Master, {g2} Backup",
                f"{g1}={s1.get(v, '?')} {g2}={s2.get(v, '?')}")
    for z in lab.zones.values():
        dhcp_hosts = [h for h in z["hosts"] if lab.hosts[h]["dhcp"]]
        if not dhcp_hosts:
            continue
        h = net[dhcp_hosts[0]]
        ping(h, z["vip"])
        neigh = h.cmd(f"ip neigh show {z['vip']}")
        vmac = f"00:00:5e:00:01:{z['vlan']:02x}"
        r.check(vmac in neigh, f"{h.name}'s gateway {z['vip']} is the virtual MAC", vmac)


def check_vrrp_failover(net, lab, r):
    """Disruptive (--check only): kill r1, check r2 takes over every gateway while a host keeps
    pinging its gateway, then bring r1 back and check it takes Master back (preemption)."""
    import services
    output("== VRRP failover test (r1 fails, then recovers)\n")
    g1, g2 = lab.gateway_routers()
    zone = lab.zones["command"]
    host = net[next(iter(zone["hosts"]))]
    fob1 = next(h for h in lab.hosts.values() if h["zone"] == "fob")
    vrids = [z["vlan"] for z in lab.zones.values()]

    def all_master(router):
        return all(services.vrrp_status(net, router).get(v) == "Master" for v in vrids)

    def all_backup(router):
        return all(services.vrrp_status(net, router).get(v) == "Backup" for v in vrids)

    log = f"/var/run/fortress/{host.name}/failover-ping.txt"
    # 10 pings/s for 20 s. Remember its PID: the host's shell also runs udhcpc in the
    # background, so a plain `wait` would wait for that too, forever.
    ping_pid = host.cmd(f"ping -n -i 0.1 -w 20 {zone['vip']} > {log} 2>&1 & echo $!").strip().splitlines()[-1]
    time.sleep(2)
    t0 = time.time()
    services.fail_node(net, g1)
    took = None
    while time.time() - t0 < 10:
        if all_master(g2):
            took = time.time() - t0
            break
        time.sleep(0.1)
    r.check(took is not None, f"{g2} became Master of every gateway after {g1} failed",
            f"{took:.1f} s" if took else "not within 10 s")
    r.check(ping(host, zone["vip"]), f"{host.name} still reaches its gateway {zone['vip']}", f"now via {g2}")
    r.check(ping(host, fob1["ip"], tries=8), f"{host.name} still reaches {fob1['name']}", "(OSPF routes around dead r1)")
    time.sleep(2)
    services.recover_node(net, g1)
    t1 = time.time()
    back = None
    # A cut-off r1 may still believe it is Master (it can't hear r2), so "done" means both:
    # r1 Master again AND r2 back to Backup, i.e. they agree again.
    while time.time() - t1 < 20:
        if all_master(g1) and all_backup(g2):
            back = time.time() - t1
            break
        time.sleep(0.2)
    r.check(back is not None, f"{g1} Master and {g2} Backup again after {g1} recovered (preemption)",
            f"{back:.1f} s" if back is not None else "not within 20 s")
    host.cmd(f"wait {ping_pid}")   # let the 20 s ping finish
    out = host.cmd(f"cat {log}")
    m = re.search(r"(\d+) packets transmitted, (\d+) received", out)
    if m:
        sent, got = int(m.group(1)), int(m.group(2))
        lost = sent - got
        # Each lost ping = 0.1 s without a gateway.
        r.check(lost <= 30, "pings to the gateway lost during failover",
                f"{lost} of {sent} (about {lost * 0.1:.1f} s without a gateway)")
    else:
        r.bad("pings to the gateway lost during failover", "(no ping summary)")


# --- T08: controller ------------------------------------------------------------
def check_controller(net, lab, r):
    """The controller sees every base switch and every switch link, and the switches really
    are in secure mode under it (so it is the controller that forwards, not OVS)."""
    import services
    output("== SDN controller\n")
    s = services.controller_status() or {}
    want = sorted(lab.core_switches + lab.access_switches)
    r.check(s.get("switches") == want, "every base switch is connected", " ".join(s.get("switches", [])) or "none")
    want_links = 1 + 2 * len(lab.access_switches)
    r.check(len(s.get("links", [])) >= want_links, "LLDP found every switch link",
            f"{len(s.get('links', []))} of {want_links}")
    modes = {sw: net[sw].vsctl("get-fail-mode", sw).strip() for sw in want}
    r.check(all(m == "secure" for m in modes.values()), "base switches are in secure mode (controller-only)",
            "" if all(m == "secure" for m in modes.values()) else str(modes))


def check_controller_flows(net, lab, r):
    """After the other checks ran traffic, the controller has learned hosts and installed rules."""
    import services
    output("== SDN controller rules\n")
    s = services.controller_status() or {}
    r.check(s.get("installed", 0) > 0, "controller installed forwarding rules",
            f"{s.get('installed', 0)} destinations, {len(s.get('hosts', []))} hosts learned")


# --- T09: reactive rerouting -----------------------------------------------------
def ping_stream(host, dst_ip, seconds, name):
    """Start a 10 pings/s stream in the background. Returns a function that waits for it and
    returns (sent, received)."""
    log = f"/var/run/fortress/{host.name}/{name}.txt"
    pid = host.cmd(f"ping -n -i 0.1 -w {seconds} {dst_ip} > {log} 2>&1 & echo $!").strip().splitlines()[-1]

    def result():
        host.cmd(f"wait {pid}")
        m = re.search(r"(\d+) packets transmitted, (\d+) received", host.cmd(f"cat {log}"))
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    return result


def check_reroute(net, lab, r):
    """Disruptive (--check only, controller mode): cut the c1-s1 link under a 10 pings/s stream
    from Command to the servers, check the controller rerouted it and logged it, then restore
    the link and check traffic goes back to the shortest path."""
    import services
    output("== Reactive rerouting (link c1-s1 down, then up)\n")
    c1, s1 = lab.core_switches[0], lab.zones["command"]["switch"]
    src = net[next(iter(lab.zones["command"]["hosts"]))]
    dst = next(h for h in lab.hosts.values() if h["role"] == "web-file")
    before = (services.controller_status() or {}).get("reroutes", 0)
    done = ping_stream(src, dst["ip"], 14, "reroute-ping")
    time.sleep(3)
    net.configLinkStatus(c1, s1, "down")
    time.sleep(4)
    s = services.controller_status() or {}
    last = s.get("last_reroute") or {}
    r.check(last.get("event") == "link_down" and sorted(last.get("link", [])) == sorted([c1, s1]),
            f"controller logged the reroute for {c1}-{s1}",
            f"{len(last.get('changed', []))} paths changed, {last.get('controller_ms', '?')} ms in the controller")
    moved = [c for c in last.get("changed", []) if c["switch"] == s1]
    r.check(bool(moved) and all(c1 not in c["new"][:2] for c in moved), f"{s1}'s paths now avoid the {c1}-{s1} link",
            f"e.g. {moved[0]['dst']}: {'-'.join(moved[0]['old'])} -> {'-'.join(moved[0]['new'])}" if moved else "")
    r.check(ping(src, dst["ip"]), f"{src.name} still reaches {dst['name']}", "while the link is down")
    net.configLinkStatus(c1, s1, "up")
    time.sleep(5)       # LLDP needs a few seconds to see the link again
    s = services.controller_status() or {}
    last = s.get("last_reroute") or {}
    r.check(last.get("event") == "link_up", "traffic moved back to the shortest path when the link returned",
            f"{len(last.get('changed', []))} paths changed back")
    sent, got = done()
    r.check(sent > 0 and sent - got <= 10, f"pings lost during the {c1}-{s1} failure",
            f"{sent - got} of {sent} (10 pings/s, so about {(sent - got) * 0.1:.1f} s)")
    r.check(s.get("reroutes", 0) >= before + 2, "reroute log in /var/log/fortress/ryu/reroutes.jsonl",
            f"{s.get('reroutes', 0) - before} new entries")


# --- T10: link aggregation between the cores ----------------------------------------
def lag_groups(net, core):
    """Select groups on a core switch and their bucket counts, from ovs-ofctl dump-groups."""
    out = net[core].cmd(f"ovs-ofctl -O OpenFlow13 dump-groups {core}")
    return [line.count("bucket=") for line in out.splitlines() if "type=select" in line]


def check_lag(net, lab, r):
    """The two c1-c2 cables are one aggregated link in the controller, carried by an
    OpenFlow select group with one bucket per cable on each core."""
    import services
    output("== Link aggregation between the cores (2 cables, select groups)\n")
    c1, c2 = lab.core_switches
    agg = (services.controller_status() or {}).get("aggregated", {})
    n = agg.get(f"{c1}-{c2}", agg.get(f"{c2}-{c1}", 0))
    r.check(n == 2, f"controller sees {c1}-{c2} as one link with 2 cables", str(agg))
    for c in (c1, c2):
        groups = lag_groups(net, c)
        r.check(2 in groups, f"{c} has a select group with 2 buckets (one per cable)", f"buckets per group: {groups}")


def check_lag_failover(net, lab, r):
    """Disruptive (--check only): pull one of the two core cables under a 10 pings/s stream
    between r1 and r2 (they sit on different cores, so their traffic crosses the cores).
    The link must stay up in the controller (no reroute), only a member goes."""
    import services
    output("== Link aggregation failover (one c1-c2 cable down, then up)\n")
    c1, c2 = lab.core_switches
    g1, g2 = lab.gateway_routers()
    target = lab.zone_ip(g2, "command")
    cable = f"{c1}-{c2}a"
    done = ping_stream(net[g1], target, 10, "lag-ping")
    time.sleep(2)
    net[c1].cmd(f"ip link set {cable} down")
    time.sleep(2)
    s = services.controller_status() or {}
    last = s.get("last_reroute") or {}
    links = [tuple(sorted(l)) for l in s.get("links", [])]
    r.check(tuple(sorted([c1, c2])) in links and last.get("event") == "member_down",
            f"{c1}-{c2} stays up in the controller, only a member went", f"last event: {last.get('event')}, "
            f"{len(last.get('changed', []))} paths changed")
    r.check(ping(net[g1], target), f"{g1} still reaches {g2} across the cores", f"{target}")
    net[c1].cmd(f"ip link set {cable} up")
    sent, got = done()
    time.sleep(4)          # LLDP needs a few seconds to see the cable again
    agg = (services.controller_status() or {}).get("aggregated", {})
    r.check(2 in agg.values(), "both cables back in the aggregated link", str(agg))
    r.check(sent > 0 and sent - got <= 5, "pings lost while a core cable was down",
            f"{sent - got} of {sent} (10 pings/s)")


# --- T11: proactive fast-failover ---------------------------------------------------
def check_fast_failover(net, lab, r):
    """Proactive mode: forwarding rules point at OpenFlow fast-failover groups, each with a
    primary and a backup bucket, so switches can fail over without the controller."""
    import services
    output("== Proactive fast-failover groups\n")
    s = services.controller_status() or {}
    r.check(s.get("mode") == "proactive", "controller is in proactive mode", s.get("mode", "?"))
    total = 0
    for sw in lab.access_switches + lab.core_switches:
        out = net[sw].cmd(f"ovs-ofctl -O OpenFlow13 dump-groups {sw}")
        total += sum(1 for l in out.splitlines() if "type=ff" in l and l.count("bucket=") == 2)
    r.check(total > 0, "switches hold fast-failover groups (primary + backup bucket)",
            f"{total} groups on the switches, {s.get('ff_groups', 0)} known to the controller")


# --- T12: mission-priority traffic --------------------------------------------------
def avg_rtt(out):
    m = re.search(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/", out)
    return float(m.group(1)) if m else None


def check_qos(net, lab, r):
    """Disruptive (--check only): flood the links from Barracks (iperf3, UDP 100 Mbit/s towards
    a server, twice the 50 Mbit/s port rate) and compare ping times to srv1 from Command
    (mission queue) and from another Barracks host (bulk queue), before and during the flood.
    Results go to docs/results/qos.csv."""
    import csv
    import json
    from datetime import datetime, timezone
    from labconfig import ROOT
    output("== Mission-priority traffic (Barracks flood vs Command latency)\n")
    cmd = net[next(iter(lab.zones["command"]["hosts"]))]
    bars = [net[h] for h in lab.zones["barracks"]["hosts"]]
    flooder, bulk = bars[0], bars[1] if len(bars) > 1 else None
    srv1 = lab.host_with_role("dhcp-dns")["ip"]
    target = next(h for h in lab.hosts.values() if h["role"] == "web-file")
    rate = lab.qos["port_rate_mbit"]

    def rtts():
        pid_c = cmd.cmd(f"ping -n -q -c 20 -i 0.2 -W 2 {srv1} > /tmp/qos-cmd.txt 2>&1 & echo $!").strip().splitlines()[-1]
        pid_b = bulk.cmd(f"ping -n -q -c 20 -i 0.2 -W 2 {srv1} > /tmp/qos-bulk.txt 2>&1 & echo $!").strip().splitlines()[-1] if bulk else None
        cmd.cmd(f"wait {pid_c}")
        if bulk:
            bulk.cmd(f"wait {pid_b}")
        return avg_rtt(cmd.cmd("cat /tmp/qos-cmd.txt")), (avg_rtt(bulk.cmd("cat /tmp/qos-bulk.txt")) if bulk else None)

    quiet_cmd, quiet_bulk = rtts()
    net[target["name"]].cmd("iperf3 -s -1 -D")
    time.sleep(1)
    pid = flooder.cmd(f"iperf3 -c {target['ip']} -u -b {2 * rate}M -t 8 -J > /tmp/qos-iperf.json 2>&1 & echo $!").strip().splitlines()[-1]
    time.sleep(2)
    busy_cmd, busy_bulk = rtts()
    flooder.cmd(f"wait {pid}")
    try:
        got = json.loads(flooder.cmd("cat /tmp/qos-iperf.json"))["end"]["sum"]["bits_per_second"] / 1e6
    except (ValueError, KeyError):
        got = None
    fmt = lambda v: f"{v:.2f} ms" if v is not None else "no reply"
    r.check(got is not None and got < 1.1 * rate, f"Barracks flood capped by the {rate} Mbit/s ports",
            f"iperf3 got {got:.1f} Mbit/s of {2 * rate} asked" if got else "iperf3 gave no result")
    r.check(busy_cmd is not None and busy_cmd < 10, f"Command ({cmd.name}) latency stays low during the flood",
            f"{fmt(quiet_cmd)} quiet -> {fmt(busy_cmd)} during the flood")
    if bulk:
        r.check(busy_bulk is None or (busy_cmd is not None and busy_bulk > 3 * busy_cmd),
                f"Barracks ({bulk.name}) traffic waits behind mission traffic",
                f"{fmt(quiet_bulk)} quiet -> {fmt(busy_bulk)} during the flood")
    out = ROOT / "docs" / "results" / "qos.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    new = not out.exists()
    with open(out, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "port_rate_mbit", "flood_mbit_asked", "flood_mbit_got", "command_rtt_quiet_ms",
                        "command_rtt_flood_ms", "barracks_rtt_quiet_ms", "barracks_rtt_flood_ms"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), rate, 2 * rate,
                    round(got, 1) if got else "", quiet_cmd, busy_cmd, quiet_bulk, busy_bulk])


# --- T13: zone isolation and rogue control -------------------------------------------
def check_isolation(net, lab, r):
    """The "Allowed to reach" rules: blocked pairs never talk (dropped by the controller),
    and where only one side may start, the routers' firewall blocks the other direction."""
    output("== Zone isolation (who may start connections to whom)\n")
    first = lambda z: net[next(iter(lab.zones[z]["hosts"]))] if z in lab.zones else None
    cases = [("barracks", "intelligence", False, "controller drops it"),
             ("command", "intelligence", True, "Command may reach every zone"),
             ("intelligence", "command", True, "Intelligence may reach Command"),
             ("barracks", "command", False, "router firewall: Barracks may not start connections to Command"),
             ("command", "barracks", True, "Command may reach Barracks"),
             ("communications", "barracks", False, "controller drops it"),
             ("barracks", "servers", True, "Barracks may reach Servers")]
    for a, b, allowed, why in cases:
        src, dst = first(a), first(b)
        if src is None or dst is None:
            continue
        ok = ping(src, lab.hosts[dst.name]["ip"], tries=2)
        r.check(ok == allowed, f"{src.name} ({a}) -> {dst.name} ({b}) {'works' if allowed else 'blocked'}", why)


def check_rogue(net, lab, r):
    """Disruptive (--check only): an unknown device plugged into a Barracks switch gets nothing,
    and a fake DHCP server on a Barracks host can't hand out addresses."""
    output("== Rogue devices (unknown host, fake DHCP server)\n")
    zone = lab.zones["barracks"]
    sw = net[zone["switch"]]
    hosts = [net[h] for h in zone["hosts"]]
    # 1. An unknown device on a free port of s4.
    rogue = net.addHost("rogue1", ip=None, mac="02:66:66:66:66:01")
    net.addLink(rogue, sw, intfName1="rogue1-eth0", intfName2=f"{sw.name}-rogue1")
    sw.attach(f"{sw.name}-rogue1")
    rogue.cmd("ip addr add 192.168.40.200/24 dev rogue1-eth0; ip route add default via " + zone["vip"])
    time.sleep(1)
    r.check(not ping(rogue, lab.hosts[hosts[0].name]["ip"], tries=2), "unknown device on s4 can't reach anyone",
            f"rogue1 -> {hosts[0].name}")
    out = rogue.cmd("busybox udhcpc -n -q -t 3 -T 1 -i rogue1-eth0 -s /bin/true 2>&1")
    r.check("obtained" not in out, "unknown device gets no DHCP lease", "no answer from srv1")
    # 2. A fake DHCP server on the first Barracks host, offering a wrong range.
    if len(hosts) >= 2:
        fake, victim = hosts[0], hosts[1]
        d = f"/var/run/fortress/{fake.name}"
        fake.cmd(f"printf 'interface {fake.name}-eth0\\nstart 192.168.40.150\\nend 192.168.40.160\\n"
                 f"lease_file {d}/udhcpd.leases\\npidfile {d}/udhcpd.pid\\nopt router 192.168.40.66\\n' > {d}/udhcpd.conf;"
                 f" touch {d}/udhcpd.leases; busybox udhcpd -f {d}/udhcpd.conf > {d}/udhcpd.txt 2>&1 &")
        time.sleep(1)
        out = victim.cmd(f"busybox udhcpc -n -q -t 4 -T 1 -i {victim.name}-eth0 -s /bin/true 2>&1")
        m = re.search(r"lease of (\S+) obtained", out)
        got = m.group(1) if m else "nothing"
        r.check(got == lab.hosts[victim.name]["ip"], f"{victim.name} still gets its address from srv1, not the fake server",
                f"offered {got} (fake server hands out 192.168.40.150-160)")
        time.sleep(1.5)   # OVS updates rule counters periodically, not instantly
        offered = "sending OFFER" in fake.cmd(f"cat {d}/udhcpd.txt")
        fake.cmd(f"kill $(cat {d}/udhcpd.pid) 2>/dev/null")
        r.check(offered, "the fake DHCP server really answered", f"(its log shows an OFFER, so the test above means something)")
        flows = sw.cmd(f"ovs-ofctl -O OpenFlow13 dump-flows {sw.name} table=0 | grep 'tp_src=67\\|udp_src=67'")
        dropped = sum(int(n) for n in re.findall(r"n_packets=(\d+)", flows))
        r.check(dropped > 0, "controller dropped the fake server's DHCP replies", f"{dropped} packets dropped on {sw.name}")


def run_all(net, lab, args):
    """Run every check that fits what the lab currently has. Returns the number of failures."""
    r = Results()
    output("\n*** Fortress-Net health checks\n")
    if args.standalone:
        check_rstp(net, lab, r)
    else:
        check_controller(net, lab, r)
    check_same_zone(net, lab, r)
    check_vlan_isolation(net, lab, r)
    check_no_storm(net, lab, r)
    check_dhcp(net, lab, r)
    check_dns(net, lab, r)
    check_ospf(net, lab, r)
    check_reachability(net, lab, r)
    check_primary_path(net, lab, r)
    check_backup_route(net, lab, r)
    check_nat(net, lab, r)
    check_vrrp(net, lab, r)
    check_isolation(net, lab, r)
    if not args.standalone:
        check_lag(net, lab, r)
        check_controller_flows(net, lab, r)
        if args.mode == "proactive":
            check_fast_failover(net, lab, r)
    if args.check:
        # Disruptive tests only run in --check mode, never when start_all.sh brings up the lab.
        check_vrrp_failover(net, lab, r)
        if not args.standalone:
            check_lag_failover(net, lab, r)
            check_reroute(net, lab, r)
            if "barracks" in lab.zones:
                check_qos(net, lab, r)
                check_rogue(net, lab, r)
    output(f"*** {r.passed} passed, {r.failed} failed\n\n")
    return r.failed
