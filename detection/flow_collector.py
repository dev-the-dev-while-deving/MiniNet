#!/usr/bin/env python3
"""flow_collector.py: per-host traffic features from ids1's mirror ports (T18).

This is the "anomaly detection" half of the input to the threat engine (the other half is
Suricata's signature alerts). Every 5 seconds it summarises what each host did, which is what
the Isolation Forest model (T19) is trained on and scores:

    packets_per_s         how much the host is sending
    bytes_per_s           how big that traffic is
    new_conns_per_s       TCP connection attempts (SYNs) per second
    distinct_dst_ports    how many different ports it touched  <- the giveaway for a port sweep
    distinct_dst_hosts    how many different hosts it touched
    failed_handshakes     SYNs nobody answered, plus resets it got back

Why from mirrored packets and not from the controller's flow counters: see decisions D13.

It runs inside ids1's namespace (so it only ever sees the mirrored copies) and writes
  - one CSV row per host per window, for training and for the report
  - latest.json, the newest window, which the threat engine (T20) reads

Usage (topology/services.py starts it; this is for running it by hand):
    ~/venvs/fortress/bin/python detection/flow_collector.py --interfaces ids1-c1 ids1-c2 \
        --out /var/log/fortress/collector/features.csv [--label normal] [--seconds 120]
"""
import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "topology"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scapy.all import IP, TCP, UDP, Dot1Q, sniff

from features import FEATURES, FIELDS      # noqa: F401  (FEATURES is re-exported for callers)
from labconfig import Lab

SYN, RST, ACK = 0x02, 0x04, 0x10


class Window:
    """Counters for one time window, keyed by the sending host's IP."""

    def __init__(self):
        self.packets = defaultdict(int)
        self.bytes = defaultdict(int)
        self.syns = defaultdict(int)
        self.ports = defaultdict(set)
        self.dsts = defaultdict(set)
        self.resets = defaultdict(int)      # resets sent BACK to this host
        self.pending = defaultdict(set)     # host -> {(dst, port)} SYNs not yet answered
        self.answered = set()               # (server, port, client) that did send a SYN-ACK
        # Each packet crossing a core is mirrored twice (in and out), and a packet that crosses
        # both cores is mirrored on each, so the same packet can arrive up to four times.
        # This set remembers what we have already counted this window (decisions D31).
        self.seen = set()


def handle(pkt, w, by_ip):
    """Fold one mirrored packet into the current window."""
    if IP not in pkt:
        return
    ip = pkt[IP]
    tcp = pkt[TCP] if TCP in pkt else None
    flags = int(tcp.flags) if tcp is not None else 0

    # Handshake bookkeeping first, in any direction and on any VLAN: a SYN-ACK proves the
    # service answered, and a reset is a refusal we want to count against whoever receives it.
    if tcp is not None:
        if flags & SYN and flags & ACK:
            w.answered.add((ip.src, int(tcp.sport), ip.dst))
        elif flags & RST:
            w.resets[ip.dst] += 1

    host = by_ip.get(ip.src)
    if host is None:
        return                       # not one of our hosts (a router's own traffic, or a rogue)
    vlan = pkt[Dot1Q].vlan if Dot1Q in pkt else None
    if vlan is not None and vlan != host["vlan"]:
        return                       # the same packet on its way through another zone (D13)

    sport = int(tcp.sport) if tcp is not None else (int(pkt[UDP].sport) if UDP in pkt else 0)
    dport = int(tcp.dport) if tcp is not None else (int(pkt[UDP].dport) if UDP in pkt else 0)
    key = (vlan, ip.src, ip.dst, ip.id, ip.proto, sport, dport, ip.len, flags)
    if key in w.seen:
        return
    w.seen.add(key)

    w.packets[ip.src] += 1
    w.bytes[ip.src] += len(pkt)
    w.dsts[ip.src].add(ip.dst)
    if dport:
        w.ports[ip.src].add(dport)
    if tcp is not None and flags & SYN and not flags & ACK:
        w.syns[ip.src] += 1
        w.pending[ip.src].add((ip.dst, dport))


def rows(w, by_ip, seconds, label):
    """Turn a finished window into one row per host that sent something."""
    now = time.time()
    out = []
    for src, packets in sorted(w.packets.items()):
        host = by_ip[src]
        unanswered = sum(1 for dst, port in w.pending[src] if (dst, port, src) not in w.answered)
        out.append({
            "time": round(now, 1), "host": host["name"], "ip": src, "zone": host["zone"], "label": label,
            "packets_per_s": round(packets / seconds, 2),
            "bytes_per_s": round(w.bytes[src] / seconds, 1),
            "new_conns_per_s": round(w.syns[src] / seconds, 2),
            "distinct_dst_ports": len(w.ports[src]),
            "distinct_dst_hosts": len(w.dsts[src]),
            "failed_handshakes": unanswered + w.resets[src],
        })
    return out


def collect(interfaces, out_csv, latest_json, window, label, seconds, lab=None):
    """Sniff the mirror ports and write one CSV row per host per window until `seconds` is up
    (or forever if seconds is 0). Also keeps latest.json for the threat engine."""
    lab = lab or Lab()
    by_ip = {h["ip"]: h for h in lab.hosts.values()}
    out_csv, latest_json = Path(out_csv), Path(latest_json)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    new_file = not out_csv.exists()
    deadline = time.time() + seconds if seconds else None

    with open(out_csv, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        while deadline is None or time.time() < deadline:
            w = Window()
            started = time.time()
            # store=False: count packets as they arrive, never keep them (memory stays flat).
            sniff(iface=interfaces, prn=lambda p: handle(p, w, by_ip), store=False, timeout=window)
            elapsed = max(time.time() - started, 0.1)
            batch = rows(w, by_ip, elapsed, label)
            writer.writerows(batch)
            f.flush()
            latest_json.write_text(json.dumps({"time": time.time(), "window": round(elapsed, 2),
                                               "hosts": batch}, indent=1))


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--interfaces", nargs="+", required=True, help="mirror interfaces to listen on")
    p.add_argument("--out", required=True, help="CSV to append feature rows to")
    p.add_argument("--latest", help="JSON file with the newest window (default: next to --out)")
    p.add_argument("--window", type=float, default=5.0, help="window length in seconds (default 5)")
    p.add_argument("--label", default="normal", help="label written into every row (normal/attack)")
    p.add_argument("--seconds", type=float, default=0, help="stop after this long (0 = run forever)")
    a = p.parse_args()
    collect(a.interfaces, a.out, a.latest or Path(a.out).with_name("latest.json"),
            a.window, a.label, a.seconds)


if __name__ == "__main__":
    main()
