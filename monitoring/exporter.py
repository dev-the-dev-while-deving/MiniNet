#!/usr/bin/env python3
"""exporter.py: publish the lab's state as Prometheus metrics (T23).

Everything the dashboard shows already exists somewhere: the controller knows the topology and
who is blocked, the collector knows how much each host is sending, the threat engine knows the
scores, Suricata knows the alerts. This turns those four into one /metrics page.

It is a *pull* exporter: nothing is stored here, every scrape re-reads the sources, so a stale
dashboard is impossible and the exporter can be restarted at any time without losing history
(Prometheus keeps that).

    ~/venvs/fortress/bin/python monitoring/exporter.py [--port 9101]
    curl -s localhost:9101/metrics | grep fortress_
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "detection"))
sys.path.insert(0, str(ROOT / "topology"))

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

import sources
from features import FEATURES
from labconfig import Lab

CONTROLLER_STATUS = "http://127.0.0.1:8080/fortress/status"


def controller_status(url=CONTROLLER_STATUS):
    import json
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


class FortressCollector:
    """Rebuilds every metric from the live sources on each scrape."""

    def __init__(self, lab):
        self.lab = lab
        self.threat_state = sources.LOG / "threat" / "state.json"

    def collect(self):
        yield from self.network_metrics()
        yield from self.traffic_metrics()
        yield from self.threat_metrics()

    # --- the network: what the controller can see ---------------------------------
    def network_metrics(self):
        status = controller_status()
        up = GaugeMetricFamily("fortress_controller_up", "1 when the SDN controller answers")
        up.add_metric([], 1 if status else 0)
        yield up
        if not status:
            return

        switches = GaugeMetricFamily("fortress_switch_connected",
                                     "1 when a switch is connected to the controller", labels=["switch"])
        expected = self.lab.core_switches + self.lab.access_switches
        for sw in expected:
            switches.add_metric([sw], 1 if sw in status.get("switches", []) else 0)
        yield switches

        links = GaugeMetricFamily("fortress_link_up", "1 when the controller sees this switch link",
                                  labels=["link"])
        seen = {"-".join(sorted(l)) for l in status.get("links", [])}
        for a in expected:
            for b in expected:
                if a < b:
                    name = f"{a}-{b}"
                    # Only the links that exist in this topology: an access switch has one to
                    # each core, and the cores have one to each other.
                    if (a in self.lab.core_switches) != (b in self.lab.core_switches) or \
                       (a in self.lab.core_switches and b in self.lab.core_switches):
                        links.add_metric([name], 1 if name in seen else 0)
        yield links

        cables = GaugeMetricFamily("fortress_link_cables",
                                   "cables in an aggregated link (T10)", labels=["link"])
        for link, n in status.get("aggregated", {}).items():
            cables.add_metric([link], n)
        yield cables

        hosts = GaugeMetricFamily("fortress_hosts_learned", "hosts the controller has located")
        hosts.add_metric([], len(status.get("hosts", [])))
        yield hosts

        rules = GaugeMetricFamily("fortress_flow_destinations",
                                  "destinations with forwarding rules installed")
        rules.add_metric([], status.get("installed", 0))
        yield rules

        groups = GaugeMetricFamily("fortress_failover_groups",
                                   "OpenFlow fast-failover groups (T11, proactive mode only)")
        groups.add_metric([], status.get("ff_groups", 0))
        yield groups

        reroutes = CounterMetricFamily("fortress_reroutes", "reroutes since the controller started")
        reroutes.add_metric([], status.get("reroutes", 0))
        yield reroutes

        last = status.get("last_reroute") or {}
        ms = GaugeMetricFamily("fortress_last_reroute_seconds",
                               "how long the controller took to recompute paths on the last event")
        ms.add_metric([], (last.get("controller_ms") or 0) / 1000.0)
        yield ms
        changed = GaugeMetricFamily("fortress_last_reroute_paths_changed",
                                    "paths that moved on the last reroute")
        changed.add_metric([], len(last.get("changed", [])))
        yield changed

        blocked = GaugeMetricFamily("fortress_host_blocked",
                                    "1 while a host is cut off at its access port (T21)",
                                    labels=["host", "ip", "switch", "port"])
        for b in status.get("blocked", []):
            blocked.add_metric([b["host"], b["ip"], b["switch"], b["port"]], 1)
        yield blocked
        count = GaugeMetricFamily("fortress_blocked_hosts", "how many hosts are blocked right now")
        count.add_metric([], len(status.get("blocked", [])))
        yield count

    # --- traffic: what the collector measured -------------------------------------
    def traffic_metrics(self):
        window = sources.latest_features()
        age = GaugeMetricFamily("fortress_features_age_seconds",
                                "how old the newest traffic measurement is")
        age.add_metric([], time.time() - window["time"] if window["time"] else -1)
        yield age
        per_host = {f: GaugeMetricFamily(f"fortress_host_{f}", f"{f} per host, last window",
                                         labels=["host", "zone"]) for f in FEATURES}
        zone_bytes = {}
        for row in window.get("hosts", []):
            for f in FEATURES:
                per_host[f].add_metric([row["host"], row["zone"]], float(row.get(f) or 0))
            zone_bytes[row["zone"]] = zone_bytes.get(row["zone"], 0.0) + float(row.get("bytes_per_s") or 0)
        yield from per_host.values()
        zones = GaugeMetricFamily("fortress_zone_bytes_per_second", "traffic per zone, last window",
                                  labels=["zone"])
        for zone in self.lab.zones:
            zones.add_metric([zone], zone_bytes.get(zone, 0.0))
        yield zones

    # --- threat: scores, bands and alerts -----------------------------------------
    def threat_metrics(self):
        import json
        try:
            state = json.loads(self.threat_state.read_text())
        except (OSError, ValueError):
            state = {"time": 0, "hosts": [], "bands": {}}
        age = GaugeMetricFamily("fortress_threat_age_seconds", "how old the newest scoring is")
        age.add_metric([], time.time() - state["time"] if state["time"] else -1)
        yield age
        score = GaugeMetricFamily("fortress_threat_score", "threat score 0-100 per host (T20)",
                                  labels=["host", "ip", "band"])
        for h in state.get("hosts", []):
            score.add_metric([h["host"], h.get("ip", ""), h["band"]], h["score"])
        yield score
        bands = GaugeMetricFamily("fortress_threat_hosts", "hosts in each band", labels=["band"])
        for b in ("normal", "suspicious", "breach"):
            bands.add_metric([b], state.get("bands", {}).get(b, 0))
        yield bands
        alerts = CounterMetricFamily("fortress_ids_alerts", "Suricata alerts seen, by kind",
                                     labels=["kind"])
        counted = {}
        for a in sources.ids_alerts(self.lab.ids["host"]):
            counted[a["kind"]] = counted.get(a["kind"], 0) + 1
        for kind in ("scan", "bruteforce", "other"):
            alerts.add_metric([kind], counted.get(kind, 0))
        yield alerts


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=9101)
    p.add_argument("--small", action="store_true")
    p.add_argument("--once", action="store_true", help="print the metrics once and exit")
    a = p.parse_args()
    collector = FortressCollector(Lab(small=a.small))
    if a.once:
        from prometheus_client import generate_latest
        REGISTRY.register(collector)
        print(generate_latest(REGISTRY).decode())
        return
    REGISTRY.register(collector)
    start_http_server(a.port)
    print(f"serving metrics on :{a.port}/metrics", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
