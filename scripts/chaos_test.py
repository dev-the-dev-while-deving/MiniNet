#!/usr/bin/env python3
"""chaos_test.py: break the network on purpose, many times, and measure how it recovers (T25).

Run inside the VM as root, from the Mac:
    ./scripts/vm/vm.sh sudo python3 scripts/chaos_test.py                 # 5 runs, both modes
    ./scripts/vm/vm.sh sudo python3 scripts/chaos_test.py --runs 3 --modes reactive
    ./scripts/vm/vm.sh sudo python3 scripts/chaos_test.py --runs 1 --only WAN      # one failure

For each controller mode (reactive = T09 rerouting, proactive = T11 fast-failover groups) it
starts the whole lab once, then for every failure below runs a 100 pings/s stream between two
hosts, triggers the failure, restores it, and records packets lost and recovery time
(scripts/measure.py does the measuring). Each failure is repeated `--runs` times, because one
measurement is an anecdote and five are a result.

    failure              mechanism that should recover it      stream
    core-access link     SDN rerouting / fast-failover          cmd1 -> srv3
    one core cable       link aggregation (select group)       r1 -> r2's address
    gateway router r1    VRRP (r2 takes over)                   cmd1 -> srv3 (routed through)
    gateway router r1    VRRP, the gateway address itself       cmd1 -> its gateway .1
    primary WAN r1-r3    OSPF + the VPN's dead-peer detection   cmd1 -> fob1

Output:
    docs/results/results.csv          one row per run
    docs/results/summary.csv          mean / min / max per failure and mode
    docs/results/*.png                charts (scripts/charts.py)
The lab is stopped at the end of each mode.
"""
import argparse
import csv
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "topology"))
sys.path.insert(0, str(ROOT / "scripts"))

from mininet.log import setLogLevel

import fortress_topo
import services
from labconfig import Lab
from measure import measure

RESULTS = ROOT / "docs" / "results"


def failures(net, lab):
    """The failure scenarios: name, mechanism, source node, destination address, fail(),
    restore(), and how long to keep the failure in place before restoring it."""
    c1, c2 = lab.core_switches
    s1 = lab.zones["command"]["switch"]
    g1, g2 = lab.gateway_routers()
    r3 = lab.router_with_role("fob")
    cmd = next(iter(lab.zones["command"]["hosts"]))
    web = next(h for h in lab.hosts.values() if h["role"] == "web-file")
    fob = next(h for h in lab.hosts.values() if h["zone"] == "fob")
    cable = f"{c1}-{c2}a"
    return [
        {"name": f"link {c1}-{s1} down", "mechanism": "SDN reroute", "src": cmd, "dst": web["ip"],
         "fail": lambda: net.configLinkStatus(c1, s1, "down"),
         "restore": lambda: net.configLinkStatus(c1, s1, "up"), "during": 4},
        {"name": f"one {c1}-{c2} cable down", "mechanism": "link aggregation", "src": g1,
         "dst": lab.zone_ip(g2, "command"),
         "fail": lambda: net[c1].cmd(f"ip link set {cable} down"),
         "restore": lambda: net[c1].cmd(f"ip link set {cable} up"), "during": 4},
        {"name": f"router {g1} power loss", "mechanism": "VRRP", "src": cmd, "dst": web["ip"],
         "fail": lambda: services.fail_node(net, g1),
         "restore": lambda: services.recover_node(net, g1), "during": 5},
        # The same failure, pinging the gateway address itself: only the VRRP Master answers it,
        # so this shows the VRRP takeover time (D39). Routed traffic (above) recovers sooner,
        # because r2 forwards frames sent to the virtual MAC before it formally becomes Master.
        {"name": f"router {g1} power loss (gateway .1)", "mechanism": "VRRP takeover", "src": cmd,
         "dst": lab.zones["command"]["vip"],
         "fail": lambda: services.fail_node(net, g1),
         "restore": lambda: services.recover_node(net, g1), "during": 5},
        # Held for longer, so a slow recovery (OSPF's 4 s dead interval) would still show up.
        # The VPN is anchored on loopbacks, so it follows OSPF's detour through r2 (D39).
        {"name": f"primary WAN {g1}-{r3} down", "mechanism": "OSPF (VPN on loopbacks)", "src": cmd, "dst": fob["ip"],
         "fail": lambda: net.configLinkStatus(g1, r3, "down"),
         "restore": lambda: net.configLinkStatus(g1, r3, "up"), "during": 12,
         # Measure "WAN cut with the VPN up": after r1's power loss the tunnel may still be
         # coming back (D28), and that wait is recorded separately as vpn_wait_s.
         "ready": lambda: services.wait_for_vpn(net, lab)},
    ]


def run_mode(mode, runs, settle, small, only=None):
    """Start the lab in one controller mode, run every failure `runs` times, stop the lab."""
    lab = Lab(small=small)
    args = fortress_topo.parse_args(["--mode", mode] + (["--small"] if small else []))
    net = fortress_topo.start_lab(lab, args)
    rows = []
    try:
        # Let routing, VRRP and the VPN settle, and let every host be learned once.
        time.sleep(5)
        for f in failures(net, lab):
            if only and only not in f["name"]:
                continue
            for run in range(1, runs + 1):
                waited = f["ready"]() if "ready" in f else None
                res = measure(net, net[f["src"]], f["dst"], f["fail"], f["restore"],
                              before=2.0, during=f["during"], after=6.0, name=f"chaos-{run}")
                rows.append({"date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "failure": f["name"], "mechanism": f["mechanism"], "mode": mode,
                             "run": run, "src": f["src"], "dst": f["dst"], **res,
                             "vpn_wait_s": "" if waited is None else round(waited, 2)})
                print(f"[{mode:9}] {f['name']:36} run {run}: lost {res['lost']:4d} of {res['sent']:4d}, "
                      f"recovery {res['recovery_s'] * 1000:6.0f} ms, "
                      f"{'healed' if res['healed'] else 'NOT healed (back only after repair)'}", flush=True)
                time.sleep(settle)
    finally:
        fortress_topo.stop_lab(net, lab)
    return rows


def summarise(rows):
    """Mean, min and max recovery and loss per failure and mode."""
    groups = {}
    for r in rows:
        groups.setdefault((r["failure"], r["mechanism"], r["mode"]), []).append(r)
    out = []
    for (failure, mechanism, mode), rs in groups.items():
        rec = [r["recovery_s"] * 1000 for r in rs]
        lost = [r["lost"] for r in rs]
        out.append({"failure": failure, "mechanism": mechanism, "mode": mode, "runs": len(rs),
                    "recovery_ms_median": round(statistics.median(rec), 1),
                    "recovery_ms_mean": round(statistics.mean(rec), 1),
                    "recovery_ms_min": round(min(rec), 1), "recovery_ms_max": round(max(rec), 1),
                    "lost_mean": round(statistics.mean(lost), 1), "lost_max": max(lost),
                    "healed_runs": sum(bool(r["healed"]) for r in rs)})
    return out


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--runs", type=int, default=5, help="repeats per failure (default 5)")
    p.add_argument("--modes", nargs="+", default=["reactive", "proactive"],
                   choices=["reactive", "proactive"])
    p.add_argument("--settle", type=float, default=6, help="seconds between runs (default 6)")
    p.add_argument("--small", action="store_true")
    p.add_argument("--only", help="run only failures whose name contains this text, e.g. WAN")
    a = p.parse_args()
    setLogLevel("warning")

    rows = []
    for mode in a.modes:
        print(f"\n=== {mode} mode: starting the lab ===", flush=True)
        rows += run_mode(mode, a.runs, a.settle, a.small, a.only)
        services.stop()                      # belt and braces between modes

    write_csv(RESULTS / "results.csv", rows)
    summary = summarise(rows)
    write_csv(RESULTS / "summary.csv", summary)
    print("\n=== summary (recovery in ms, over all runs) ===")
    for s in summary:
        print(f"{s['failure']:36} {s['mode']:9} median {s['recovery_ms_median']:7.1f}  mean {s['recovery_ms_mean']:7.1f}  "
              f"min {s['recovery_ms_min']:7.1f}  max {s['recovery_ms_max']:7.1f}  "
              f"lost mean {s['lost_mean']:6.1f}  healed {s['healed_runs']}/{s['runs']}")
    # Charts need matplotlib, which lives in the fortress venv, not the system Python.
    charts = subprocess.run([services.fortress_python(), str(ROOT / "scripts" / "charts.py")],
                            capture_output=True, text=True)
    print(charts.stdout.strip() or charts.stderr.strip())
    print(f"\nwrote {RESULTS / 'results.csv'} and {RESULTS / 'summary.csv'}")


if __name__ == "__main__":
    main()
