#!/usr/bin/env python3
"""baseline_traffic.py: generate normal, everyday traffic across the base (T18).

The anomaly model (T19) learns what normal looks like, so it is only as good as this script.
It therefore does what real users do, and nothing an attacker would do:

  * pings between zones that are allowed to talk (fortress.yaml "reach")
  * DNS lookups on srv1 (every zone may use the name server)
  * web requests and file downloads from srv3
  * an admin logging into r1 over SSH from the Management workstation

Each host talks to a handful of destinations on one or two ports, which is exactly what makes
a port sweep stand out later: many ports, many hosts, unanswered connections.

Run a full baseline (starts and stops the lab itself, like scripts/measure.py):
    ./scripts/vm/vm.sh sudo python3 detection/baseline_traffic.py --minutes 30
The features land in /var/log/fortress/collector/baseline.csv for train_model.py (T19).
"""
import argparse
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "topology"))

import fortress_topo
import services
from labconfig import Lab

BASELINE_CSV = services.LOG / "collector" / "baseline.csv"


def targets(lab):
    """What normal traffic exists in this lab: a list of (source host, description, command).

    Only pairs fortress.yaml allows are used, so the baseline never contains traffic the
    firewall would drop (that would teach the model that blocked traffic is normal).
    """
    srv1 = lab.host_with_role("dhcp-dns")
    web = next((h for h in lab.hosts.values() if h["role"] == "web-file"), None)
    jobs = []

    def zone_hosts(zone):
        if zone == "fob":
            return [h for h in lab.hosts.values() if h["zone"] == "fob"]
        return [lab.hosts[n] for n in lab.zones[zone]["hosts"]] if zone in lab.zones else []

    for src_zone in list(lab.zones) + ["fob"]:
        for src in zone_hosts(src_zone):
            # Everyone resolves names, which is the most common traffic in any network.
            jobs.append((src, "dns", f"timeout 3 dig +short +tries=1 {srv1['name']}.{lab.domain} @{srv1['ip']}"))
            for dst_zone in (lab.zones[src_zone].get("reach", []) if src_zone in lab.zones
                             else lab.fob.get("reach", [])):
                if dst_zone in ("internet",):
                    continue                      # NAT works, but the ISP host runs no services
                # Two hosts per zone is enough variety, but the web server is always included:
                # without it the baseline has no file transfers, and a user downloading
                # something would later look anomalous to the model.
                chosen = zone_hosts(dst_zone)[:2]
                if web and web["zone"] == dst_zone and web["name"] not in {h["name"] for h in chosen}:
                    chosen.append(web)
                for dst in chosen:
                    if dst["name"] == src["name"]:
                        continue
                    jobs.append((src, "ping", f"timeout 5 ping -n -c 3 -i 0.3 {dst['ip']}"))
                    if web and dst["name"] == web["name"]:
                        jobs.append((src, "web", f"timeout 5 curl -s -o /dev/null http://{web['ip']}/"))
                        jobs.append((src, "file", f"timeout 20 curl -s -o /dev/null http://{web['ip']}/file.bin"))
    # An admin checking a router: normal management traffic, and the "good login" counterpart
    # to the failed logins the threat engine looks for (T20).
    if "management" in lab.zones:
        admin = lab.hosts[next(iter(lab.zones["management"]["hosts"]))]
        r1 = lab.gateway_routers()[0]
        jobs.append((admin, "ssh", f"timeout 8 ssh -o BatchMode=yes -o StrictHostKeyChecking=no "
                                   f"-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "
                                   f"-i {services.SSH_DIR / 'admin_key'} "
                                   f"{services.lab_user()}@{lab.zone_ip(r1, 'management')} uptime"))
    return jobs


def run(net, lab, seconds, at_once=4, pause=2.0, verbose=False):
    """Generate normal traffic for `seconds`. Returns how many actions were started."""
    jobs = targets(lab)
    if not jobs:
        return 0
    deadline = time.time() + seconds
    started = 0
    while time.time() < deadline:
        for src, kind, cmd in random.sample(jobs, min(at_once, len(jobs))):
            net[src["name"]].cmd(f"{cmd} > /dev/null 2>&1 &")
            started += 1
            if verbose:
                print(f"  {src['name']}: {kind}")
        time.sleep(pause)
    return started


def summary(csv_file):
    """Print how usable the collected baseline is (the T18 acceptance check)."""
    import csv as csvmod
    rows = list(csvmod.DictReader(open(csv_file)))
    if not rows:
        return 0, 0, ["no rows"]
    from features import FEATURES
    missing = [f"{f} empty in {sum(1 for r in rows if r[f] in ('', None))} rows"
               for f in FEATURES if any(r[f] in ("", None) for r in rows)]
    hosts = sorted({r["host"] for r in rows})
    print(f"{len(rows)} feature rows for {len(hosts)} hosts: {' '.join(hosts)}")
    for f in FEATURES:
        values = [float(r[f]) for r in rows]
        print(f"  {f:20} min {min(values):8.2f}  mean {sum(values) / len(values):8.2f}  max {max(values):8.2f}")
    return len(rows), len(hosts), missing


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--minutes", type=float, default=30, help="how long to generate traffic (default 30)")
    p.add_argument("--out", default=str(BASELINE_CSV), help=f"feature CSV (default {BASELINE_CSV})")
    p.add_argument("--label", default="normal")
    p.add_argument("--small", action="store_true")
    a = p.parse_args()

    from mininet.log import setLogLevel
    setLogLevel("warning")
    lab = Lab(small=a.small)
    net = fortress_topo.start_lab(lab, fortress_topo.parse_args(["--small"] if a.small else []))
    try:
        services.start_collector(net, lab, a.out, label=a.label)
        print(f"generating normal traffic for {a.minutes:g} minute(s)...")
        actions = run(net, lab, a.minutes * 60)
        time.sleep(6)                      # let the last window close and be written
        print(f"{actions} traffic actions done")
    finally:
        fortress_topo.stop_lab(net, lab)
    rows, hosts, missing = summary(a.out)
    for m in missing:
        print(f"MISSING VALUES: {m}")
    print(f"\n{a.out}")
    sys.exit(1 if missing or rows == 0 else 0)


if __name__ == "__main__":
    main()
