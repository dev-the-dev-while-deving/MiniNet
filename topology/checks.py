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
    if not args.standalone:
        check_controller_flows(net, lab, r)
    if args.check:
        # Disruptive tests only run in --check mode, never when start_all.sh brings up the lab.
        check_vrrp_failover(net, lab, r)
        if not args.standalone:
            check_reroute(net, lab, r)
    output(f"*** {r.passed} passed, {r.failed} failed\n\n")
    return r.failed
