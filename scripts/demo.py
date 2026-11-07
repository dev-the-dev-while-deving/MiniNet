#!/usr/bin/env python3
"""demo.py: the 10-minute viva demo, step by step (T26). Normally run through scripts/demo.sh.

    ./scripts/vm/vm.sh sudo ./scripts/demo.sh            # live: pauses for Enter between steps
    ./scripts/vm/vm.sh sudo ./scripts/demo.sh --auto     # rehearsal: no pauses, timed

It follows the demo script in FORTRESS-NET-ROADMAP.md section 6. Each step prints what is about
to happen, does it, and then prints WHAT TO POINT AT. A Command -> FOB ping stream runs through
the whole demo, and every step reports how it fared, so the audience can see traffic surviving
each failure. Nothing here is special demo code: every step calls the same functions as the
health checks (topology/checks.py) and the chaos test, so what you show is what was tested.

At the end (live mode) it opens the Mininet CLI, so you can take questions on the running lab.
"""
import argparse
import csv
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "topology"))

from mininet.log import setLogLevel

import checks
import fortress_topo
import services
from labconfig import Lab

LOG = Path("/var/log/fortress")
BOLD, DIM, GREEN, RED, END = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[0m"


class Demo:
    def __init__(self, net, lab, auto):
        self.net, self.lab, self.auto = net, lab, auto
        self.t0 = time.time()
        self.step_no = 0
        self.cmd = next(iter(lab.zones["command"]["hosts"]))                 # cmd1
        self.fob = next(h for h in lab.hosts.values() if h["zone"] == "fob")  # fob1
        self.web = lab.host_with_role("web-file")                              # srv3

    # --- presentation helpers ---------------------------------------------------------
    def step(self, title, say):
        """Start a step: its number, the clock, and what the presenter should say."""
        self.step_no += 1
        mins, secs = divmod(int(time.time() - self.t0), 60)
        print(f"\n{BOLD}=== Step {self.step_no} [{mins}:{secs:02d}] {title}{END}")
        print(f"{DIM}Say: {say}{END}")
        if not self.auto:
            input(f"{DIM}(Enter to run it){END}")

    def point(self, *lines):
        """What to point at on screen once the step has run."""
        for line in lines:
            print(f"  {GREEN}>{END} {line}")

    # --- the ping streams that run through the whole demo -----------------------------
    def start_streams(self):
        """10 pings/s from Command to the FOB (across the WAN and VPN) and to the web server
        (inside the base), for the whole demo. -D timestamps every reply."""
        for name, ip in (("fob", self.fob["ip"]), ("servers", self.web["ip"])):
            self.net[self.cmd].cmd(f"ping -n -D -i 0.1 {ip} > /var/run/fortress/{self.cmd}/demo-{name}.txt 2>&1 &")

    def stream_report(self, since, which="fob"):
        """How one stream did since `since`: replies, and the longest time without one. The
        start and the end of the period count as edges, so a stream that went silent shows
        as paused even if no reply came at all."""
        out = self.net[self.cmd].cmd(f"cat /var/run/fortress/{self.cmd}/demo-{which}.txt")
        stamps = [float(t) for t in re.findall(r"^\[(\d+\.\d+)\] \d+ bytes", out, re.M)]
        recent = [t for t in stamps if t >= since]
        edges = [since] + recent + [time.time()]
        worst = max(b - a for a, b in zip(edges, edges[1:]))
        label = "Command -> FOB" if which == "fob" else "Command -> Servers"
        verdict = f"{GREEN}kept flowing{END}" if worst < 1.0 else f"{RED}paused {worst:.1f} s{END}"
        return f"{label} ping stream: {len(recent)} replies, longest gap {worst * 1000:.0f} ms, {verdict}"

    # --- the steps (roadmap section 6) ------------------------------------------------
    def intro(self):
        self.step("The base and its architecture",
                  "Six zones on their own VLANs, two core switches run by an SDN controller, two gateway "
                  "routers sharing each zone's gateway with VRRP, and a VPN to a forward operating base.")
        for z in self.lab.zones.values():
            print(f"    VLAN {z['vlan']:<3} {z['title']:<16} {str(z['net']):<17} gateway {z['vip']}")
        print(f"    FOB      over the IPsec VPN   {self.lab.fob['net']}")
        # The VM's own address (its first one), for the browser on the Mac.
        vm_ip = (subprocess.run(["hostname", "-I"], capture_output=True, text=True).stdout.split() or ["<VM IP>"])[0]
        self.point(f"Grafana dashboard: http://{vm_ip}:3000 (admin/admin), everything green",
                   f"The SDN controller manages {len(self.lab.dpids())} switches "
                   f"(curl -s localhost:8080/fortress/status in the VM)")

    def traffic(self):
        self.step("Start traffic: Command Center to the FOB and to the servers",
                  "Two pings run for the whole demo, ten a second each: one out to the FOB over the VPN, "
                  "one to the web server inside the base. Watch whether they ever stop.")
        self.mark = time.time()
        self.start_streams()
        time.sleep(3)
        self.point(self.stream_report(self.mark), self.stream_report(self.mark, "servers"),
                   "The FOB stream crosses r1, the primary WAN and the encrypted tunnel")

    def link_sabotage(self):
        c1, s1 = self.lab.core_switches[0], self.lab.zones["command"]["switch"]
        self.step(f"Scenario 1, link sabotage: cut {c1}-{s1}",
                  f"Someone cuts the cable between core switch {c1} and the Command Center switch.")
        since = time.time()
        before = len(self.reroutes())
        self.net.configLinkStatus(c1, s1, "down")
        time.sleep(3)
        new = self.reroutes()[before:]
        moved = sum(len(e["changed"]) for e in new)
        ms = ", ".join(f"{e['controller_ms']} ms" for e in new)
        self.point(self.stream_report(since),
                   f"The controller logged {len(new)} reroute event(s), {moved} paths moved, computed in {ms or '-'}"
                   " (/var/log/fortress/ryu/reroutes.jsonl)",
                   "Measured over 5 runs: about 20-30 ms of interruption (docs/results/summary.csv)")
        self.net.configLinkStatus(c1, s1, "up")

    def reroutes(self):
        try:
            return [json.loads(l) for l in (LOG / "ryu" / "reroutes.jsonl").read_text().splitlines() if l.strip()]
        except (OSError, ValueError):
            return []

    def equipment_failure(self):
        g1, g2 = self.lab.gateway_routers()
        self.step(f"Scenario 2, equipment failure: {g1} loses power",
                  f"The main gateway router {g1} dies. Its partner {g2} must take over every zone's gateway.")
        since = time.time()
        services.fail_node(self.net, g1)
        took = None
        while time.time() - since < 10:
            if all(v == "Master" for v in services.vrrp_status(self.net, g2).values()):
                took = time.time() - since
                break
            time.sleep(0.1)
        time.sleep(2)
        self.point(f"VRRP: {g2} is Master of every gateway after {took:.1f} s" if took else f"{g2} did not take over!",
                   self.stream_report(since, "servers"),
                   f"Inside the base, hosts never noticed: {g2} forwards for the shared MAC even before "
                   "VRRP finishes (D39)",
                   self.stream_report(since),
                   "The FOB pauses a few seconds: the VPN lives on r1, so r3 first has to notice r1 is "
                   "dead (dead-peer detection) before traffic can go round via r2. Known limit (D28); "
                   "the fix would be a second tunnel on r2")
        services.recover_node(self.net, g1)
        back = services.wait_for_vpn(self.net, self.lab)
        self.point(f"{g1} is back and Master again (preemption); the VPN rebuilt itself in "
                   f"{back:.1f} s" if back is not None else "the VPN did not come back!")

    def wan_failure(self):
        g1 = self.lab.gateway_routers()[0]
        r3 = self.lab.router_with_role("fob")
        self.step(f"Scenario 2b: the primary WAN {g1}-{r3} goes down",
                  "The main link to the FOB is cut. OSPF finds the way round through r2, and because the VPN "
                  "runs between loopback addresses, the tunnel follows it.")
        since = time.time()
        self.net.configLinkStatus(g1, r3, "down")
        time.sleep(3)
        route = self.net[g1].cmd(f"ip route get {self.fob['ip']}").split(" src")[0].strip()
        self.point(f"{g1} now routes the FOB: {route}",
                   f"VPN still up: {services.vpn_up(self.net, self.lab)}",
                   self.stream_report(since))
        self.net.configLinkStatus(g1, r3, "up")
        time.sleep(3)

    def insider(self):
        self.step("Scenario 3, insider attack: a Barracks host sweeps ports",
                  "A soldier's laptop in the Barracks starts scanning the web server. Inside the lab only.")
        self.res = checks.insider_run(self.net, self.lab)
        sc = self.res["scored"] or {}
        self.point(f"Suricata's first alert after {self.res['alert_s']} s"
                   + (f': "{self.res["alerts"][0]["signature"]}"' if self.res["alerts"] else ""),
                   f"Threat score {sc.get('score', '?')}/100 for {self.res['attacker']['name']}, because:")
        for reason in sc.get("reasons", []):
            print(f"      - {reason}")
        self.point("A breach always needs two independent signals: one alert alone is only 30 points")

    def response(self):
        self.step("Automatic response",
                  "Nobody pressed anything. The engine asked the controller to cut the host off at its own port.")
        a = self.res["attacker"]
        blocked = self.res["blocked"]
        srv1 = self.lab.host_with_role("dhcp-dns")["ip"]
        cut = not checks.ping(self.net[a["name"]], srv1, tries=2)
        neighbour = next(n for n in self.lab.zones["barracks"]["hosts"] if n != a["name"])
        fine = checks.ping(self.net[neighbour], srv1, tries=2)
        self.point(f"Blocked {self.res['block_s']} s after the scan began, at "
                   + (f"{blocked[0]['port']} on {blocked[0]['switch']}" if blocked else "?"),
                   f"{a['name']} can reach nothing: {cut};  {neighbour} next to it is unaffected: {fine}",
                   "Phone: the Telegram alert with the same reasons (if a bot is configured in .env)")

    def evidence(self):
        g1 = self.lab.gateway_routers()[0]
        r3 = self.lab.router_with_role("fob")
        self.step("Evidence on the wire: the VPN really encrypts",
                  "Capture what leaves r1 on the WAN cable while Command talks to the FOB.")
        pcap = ROOT / "docs" / "evidence" / "demo-vpn.pcap"
        self.net[g1].cmd(f"timeout 4 tcpdump -ni {g1}-{r3} -Q out -w {pcap} 'esp or icmp' >/dev/null 2>&1 &")
        time.sleep(4.5)
        lines = self.net[g1].cmd(f"tcpdump -nr {pcap} 2>/dev/null | head -4").strip().splitlines()
        esp = int(self.net[g1].cmd(f"tcpdump -nr {pcap} esp 2>/dev/null | wc -l").strip() or 0)
        icmp = int(self.net[g1].cmd(f"tcpdump -nr {pcap} icmp 2>/dev/null | wc -l").strip() or 0)
        for l in lines:
            print(f"      {l[:110]}")
        self.point(f"{esp} ESP packets, {icmp} readable ICMP packets on the cable",
                   f"Open {pcap.relative_to(ROOT)} in Wireshark on the Mac to show it live")

    def report(self):
        self.step("Incident report and the failover table",
                  "Every breach is written up, and every recovery number was measured, five times each.")
        reports = sorted((ROOT / "docs" / "incidents").glob("*.md"))
        if reports:
            print(f"    {reports[-1].relative_to(ROOT)}:")
            for line in reports[-1].read_text().splitlines()[:12]:
                print(f"      {line}")
        summary = ROOT / "docs" / "results" / "summary.csv"
        if summary.exists():
            print(f"\n    {'failure':36} {'mode':9} {'median ms':>9} {'healed':>7}")
            for r in csv.DictReader(open(summary)):
                print(f"    {r['failure']:36} {r['mode']:9} {r['recovery_ms_median']:>9} "
                      f"{r['healed_runs'] + '/' + r['runs']:>7}")
        self.point("Charts: docs/results/recovery_by_failure.png and detection_to_block.png",
                   self.stream_report(self.mark) + " (the whole demo)",
                   self.stream_report(self.mark, "servers") + " (the whole demo)")

    def finish(self):
        checks.insider_reset(self.net, self.lab, quiet=0)    # let the attacker back on
        self.net[self.cmd].cmd("pkill -f '[p]ing -n -D -i 0.1'")
        total = time.time() - self.t0
        mins, secs = divmod(int(total), 60)
        print(f"\n{BOLD}Demo finished in {mins}:{secs:02d}{END}"
              + ("  (rehearsal: under 10 minutes)" if total < 600 else f"  {RED}over 10 minutes{END}"))
        return total


def main():
    p = argparse.ArgumentParser(description="Fortress-Net viva demo (T26)")
    p.add_argument("--auto", action="store_true", help="rehearsal: no pauses, report the total time")
    p.add_argument("--mode", choices=["reactive", "proactive"], default="proactive",
                   help="controller failover mode (default proactive, the faster one)")
    p.add_argument("--small", action="store_true")
    a = p.parse_args()
    setLogLevel("warning")
    lab = Lab(small=a.small)
    print(f"{BOLD}Starting the lab ({a.mode} mode). This takes about a minute.{END}")
    net = fortress_topo.start_lab(lab, fortress_topo.parse_args(["--mode", a.mode] + (["--small"] if a.small else [])))
    try:
        time.sleep(3)
        demo = Demo(net, lab, a.auto)
        for step in (demo.intro, demo.traffic, demo.link_sabotage, demo.equipment_failure, demo.wan_failure,
                     demo.insider, demo.response, demo.evidence, demo.report):
            step()
        total = demo.finish()
        if not a.auto:
            print("Opening the Mininet CLI for questions (type exit to stop the lab).")
            fortress_topo.FortressCLI(net)
    finally:
        fortress_topo.stop_lab(net, lab)
    if a.auto and total >= 600:
        sys.exit(1)


if __name__ == "__main__":
    main()
