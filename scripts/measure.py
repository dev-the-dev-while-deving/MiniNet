#!/usr/bin/env python3
"""measure.py: measure packet loss and recovery time for one failure (T09b; T25 builds on it).

Run inside the VM as root, from the Mac:
    ./scripts/vm/vm.sh sudo python3 scripts/measure.py link c1 s1              # cut the c1-s1 link
    ./scripts/vm/vm.sh sudo python3 scripts/measure.py node r1                 # power-fail router r1
    ./scripts/vm/vm.sh sudo python3 scripts/measure.py link c1 s1 --runs 3 --mode proactive

It starts the whole lab, runs a 100 pings/s stream between two hosts, triggers the failure,
restores it, and reports:
    lost      packets that never came back
    recovery  the longest time without a reply, i.e. how long traffic stopped
    healed    whether traffic was flowing again BEFORE the failure was repaired
Results are printed and appended to docs/results/early.csv (one row per run).
The lab is stopped afterwards.
"""
import argparse
import csv
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "topology"))

from mininet.log import setLogLevel

import fortress_topo
import services
from labconfig import Lab

INTERVAL = 0.01          # ask for 100 pings/s (Mininet manages ~55/s): accurate to ~20 ms
CSV_FILE = ROOT / "docs" / "results" / "early.csv"


def measure(net, src, dst_ip, fail, restore, before=2.0, during=4.0, after=4.0, name="measure"):
    """Ping dst_ip from src as fast as -i INTERVAL allows, call fail() after `before` s and
    restore() `during` s later. Returns:
        sent, received, lost   ping's own counts
        recovery_s             the longest time with no reply, minus the normal spacing between
                               replies: how long traffic was cut. The end of the run counts too,
                               so traffic that never comes back shows as a long cut, not as 0.
        healed                 True if replies were flowing again in the last second BEFORE
                               restore(): the network healed itself while the failure was still
                               in place. False means traffic only came back once we repaired it.
    """
    total = before + during + after
    log = f"/var/run/fortress/{src.name}/{name}.txt"
    # -D prints a timestamp on every reply, so gaps can be measured exactly.
    pid = src.cmd(f"ping -n -D -i {INTERVAL} -w {total:.0f} {dst_ip} > {log} 2>&1 & echo $!").strip().splitlines()[-1]
    t_start = time.time()
    time.sleep(before)
    fail()
    time.sleep(during)
    t_restore = time.time()
    restore()
    src.cmd(f"wait {pid}")
    out = src.cmd(f"cat {log}")
    stamps = [float(t) for t in re.findall(r"^\[(\d+\.\d+)\] \d+ bytes from", out, re.M)]
    m = re.search(r"(\d+) packets transmitted, (\d+) received", out)
    sent, received = (int(m.group(1)), int(m.group(2))) if m else (0, len(stamps))
    # Mininet hosts can't always keep up 100 pings/s, so the normal spacing is measured, not assumed.
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    spacing = sorted(gaps)[len(gaps) // 2] if gaps else INTERVAL
    # Bracket the replies with the start and end of the run, so silence at either edge counts.
    edges = [t_start + 0.5] + stamps + [t_start + total]
    longest = max(b - a for a, b in zip(edges, edges[1:]))
    healed = any(t_restore - 1.0 <= t < t_restore for t in stamps)
    return {"sent": sent, "received": received, "lost": sent - received,
            "recovery_s": round(max(0.0, longest - spacing), 3), "healed": healed}


def scenario(net, args):
    """(description, fail, restore) for the failure named on the command line."""
    if args.kind == "link":
        a, b = args.target
        return (f"link {a}-{b} down", lambda: net.configLinkStatus(a, b, "down"),
                lambda: net.configLinkStatus(a, b, "up"))
    node = args.target[0]
    return (f"node {node} power loss", lambda: services.fail_node(net, node),
            lambda: services.recover_node(net, node))


def main():
    p = argparse.ArgumentParser(description="Measure loss and recovery time for one failure")
    p.add_argument("kind", choices=["link", "node"])
    p.add_argument("target", nargs="+", help="link: two node names (c1 s1); node: one name (r1)")
    p.add_argument("--src", default="cmd1", help="host that pings (default cmd1)")
    p.add_argument("--dst", default="srv3", help="host it pings (default srv3)")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--mode", choices=["reactive", "proactive"], default="reactive")
    p.add_argument("--standalone", action="store_true", help="measure the RSTP setup instead of the controller")
    p.add_argument("--settle", type=float, default=6, help="seconds to wait between runs")
    args = p.parse_args()
    if (args.kind == "link") != (len(args.target) == 2):
        p.error("link needs two node names, node needs one")

    setLogLevel("warning")
    lab = Lab()
    topo_args = fortress_topo.parse_args(["--mode", args.mode] + (["--standalone"] if args.standalone else []))
    net = fortress_topo.start_lab(lab, topo_args)
    rows = []
    try:
        src, dst_ip = net[args.src], lab.hosts[args.dst]["ip"]
        what, fail, restore = scenario(net, args)
        mode = "standalone" if args.standalone else args.mode
        for run in range(1, args.runs + 1):
            res = measure(net, src, dst_ip, fail, restore, name=f"measure-{run}")
            rows.append({"date": datetime.now(timezone.utc).isoformat(timespec="seconds"), "scenario": what,
                         "mode": mode, "run": run, "src": args.src, "dst": args.dst, **res})
            print(f"{what} [{mode}] run {run}: lost {res['lost']} of {res['sent']} pings, "
                  f"recovery {res['recovery_s'] * 1000:.0f} ms")
            time.sleep(args.settle)
    finally:
        fortress_topo.stop_lab(net, lab)
    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
    new = not CSV_FILE.exists()
    with open(CSV_FILE, "a", newline="") as f:
        # Keep the existing file's columns (rows from older runs may lack newer fields).
        fields = list(rows[0]) if new else next(csv.reader(open(CSV_FILE)))
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"appended {len(rows)} row(s) to {CSV_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
