#!/usr/bin/env python3
"""threat_engine.py: one explainable 0-100 threat score per host (T20, signature feature).

Most student IDS projects print "attack" or "normal". This one has to survive the question
"why?", so every score carries the reasons that produced it, and no single source can declare a
breach on its own. Three independent sources feed it (detection/sources.py):

    signatures  Suricata on the mirrored traffic           (T17)
    behaviour   per-host traffic features + the model      (T18, T19)
    logs        failed SSH logins, collected on srv2        (T15)

Scoring (also in CLAUDE.md; the table below is the one place it is defined):

    anomaly               up to 30   the model's 0..1 verdict, scaled
    IDS signature            +30     any Suricata alert from this host
    port sweep               +30     more than 50 different destination ports in one window
    refused connections      +15     20 or more SYNs nobody answered, or resets coming back
    failed SSH logins        +25     5 or more in the look-back window
    scan, then logins        +15     correlation: the same host did both
    two sources agree        +15     correlation: an IDS brute-force rule AND the central log

    0-39 Normal (log it)   40-69 Suspicious (dashboard)   70-100 Breach (alert and respond)

The weights are set so that a breach always needs **at least two independent signals**: a lone
Suricata alert is 30 (Normal), and the model alone can reach 30 at most.

Usage:
    threat_engine.py --self-check     # run the scoring table through worked examples
    threat_engine.py --once           # score the current window and print it
    threat_engine.py --watch          # keep scoring (what start_all.sh runs)
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "detection"))
sys.path.insert(0, str(ROOT / "topology"))

import anomaly
import sources
from features import FEATURES
from labconfig import Lab

STATE_DIR = sources.LOG / "threat"
CONTROLLER = "http://127.0.0.1:8080/fortress"

# --- the scoring table ---------------------------------------------------------------------
ANOMALY_MAX = 30          # the model can raise suspicion but never declare a breach alone
IDS_ALERT = 30
PORT_SWEEP, PORT_SWEEP_MIN = 30, 50
REFUSED, REFUSED_MIN = 15, 20
FAILED_LOGINS, FAILED_LOGINS_MIN = 25, 5
CORRELATE_SCAN_LOGIN = 15
CORRELATE_TWO_SOURCES = 15

SUSPICIOUS, BREACH = 40, 70

# How far back each source is read. Packets are summarised every 5 s, but a login attempt is a
# human-speed event, so failed logins are counted over a longer window.
ALERT_WINDOW = 30
LOGIN_WINDOW = 120


def band(score):
    return "breach" if score >= BREACH else "suspicious" if score >= SUSPICIOUS else "normal"


def score_host(host, feats, alerts, logins, anomaly_value=None):
    """Score one host. Pure function: same inputs, same output, no files, no network.

    host          display name (or the IP, for a device we don't know)
    feats         that host's latest feature row, or {} if it sent nothing this window
    alerts        Suricata alerts whose source is this host
    logins        failed SSH logins whose source is this host
    anomaly_value the model's 0..1 verdict, or None if no model is loaded

    Returns {host, score, band, reasons, signals, evidence}.
    """
    signals = {}          # signal name -> points, so the dashboard can show the breakdown
    reasons = []          # the human-readable sentences, in the order they were added

    def add(name, points, reason):
        signals[name] = points
        reasons.append(f"{reason} +{points}")

    if anomaly_value is not None:
        points = round(anomaly_value * ANOMALY_MAX)
        if points >= 1:
            add("anomaly", points, f"traffic {anomaly_value:.2f} outside the normal baseline")

    scan_alerts = [a for a in alerts if a["kind"] == "scan"]
    brute_alerts = [a for a in alerts if a["kind"] == "bruteforce"]
    if alerts:
        worst = (scan_alerts or brute_alerts or alerts)[0]
        add("ids_alert", IDS_ALERT,
            f'Suricata "{worst["signature"]}" ({len(alerts)} alerts)')

    ports = int(feats.get("distinct_dst_ports", 0) or 0)
    if ports > PORT_SWEEP_MIN:
        add("port_sweep", PORT_SWEEP, f"{ports} different destination ports in one window")

    refused = int(feats.get("failed_handshakes", 0) or 0)
    if refused >= REFUSED_MIN:
        add("refused", REFUSED, f"{refused} connection attempts refused or unanswered")

    if len(logins) >= FAILED_LOGINS_MIN:
        nodes = sorted({l["node"] for l in logins})
        add("failed_logins", FAILED_LOGINS,
            f"{len(logins)} failed SSH logins on {', '.join(nodes)}")

    # Correlation: the whole point of having three sources. A host that scans and then tries
    # passwords is further along an attack than one that only did either.
    scanned = bool(scan_alerts) or "port_sweep" in signals
    if scanned and len(logins) >= FAILED_LOGINS_MIN:
        add("scan_then_login", CORRELATE_SCAN_LOGIN,
            "port scan followed by login attempts from the same host")
    if brute_alerts and len(logins) >= FAILED_LOGINS_MIN:
        add("two_sources", CORRELATE_TWO_SOURCES,
            "the IDS and the central log agree about this host")

    score = min(sum(signals.values()), 100)
    return {"host": host, "score": score, "band": band(score), "reasons": reasons,
            "signals": signals,
            "evidence": {"features": {f: feats.get(f) for f in FEATURES} if feats else {},
                         "alerts": [a["signature"] for a in alerts[:5]],
                         "failed_logins": len(logins),
                         "anomaly": round(anomaly_value, 3) if anomaly_value is not None else None}}


def assess(lab, bundle=None, now=None):
    """Score every host that did anything recently. Returns a list of per-host results."""
    now = now or time.time()
    window = sources.latest_features()
    by_ip = {h["ip"]: h["name"] for h in lab.hosts.values()}
    feats = {row["ip"]: row for row in window.get("hosts", [])}
    alerts, logins = {}, {}
    for a in sources.ids_alerts(lab.ids["host"], since=now - ALERT_WINDOW):
        alerts.setdefault(a["src_ip"], []).append(a)
    for l in sources.failed_logins(lab.host_with_role("syslog")["name"], since=now - LOGIN_WINDOW):
        logins.setdefault(l["src_ip"], []).append(l)

    scores = []
    for ip in sorted(set(feats) | set(alerts) | set(logins)):
        row = feats.get(ip, {})
        value = anomaly.score(bundle, row) if (bundle and row) else None
        result = score_host(by_ip.get(ip, ip), row, alerts.get(ip, []), logins.get(ip, []), value)
        result["ip"] = ip
        scores.append(result)
    return sorted(scores, key=lambda s: -s["score"])


# --- response (T21 blocks, T24 reports) ----------------------------------------------------
def block_host(ip, reason, seconds=300):
    """Ask the controller to cut this host off at its own switch port (T21).

    Returns the controller's answer, or an error dict. The engine keeps running either way:
    losing the ability to block must not stop us scoring and alerting.
    """
    import urllib.error
    import urllib.request
    body = json.dumps({"ip": ip, "reason": reason, "seconds": seconds}).encode()
    req = urllib.request.Request(f"{CONTROLLER}/block", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"error": str(e)}


def write_incident(result, blocked, folder=ROOT / "docs" / "incidents"):
    """One markdown file per breach: timeline, score reasons, evidence, action taken (T24)."""
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp.strftime('%Y%m%dT%H%M%SZ')}-{result['host']}.md"
    ev = result["evidence"]
    lines = [f"# Incident: {result['host']} ({result['ip']}) scored {result['score']}/100", "",
             f"- **When:** {stamp.isoformat(timespec='seconds')}",
             f"- **Level:** {result['band'].upper()}",
             f"- **Host:** {result['host']} ({result['ip']})", "",
             "## Why the score is what it is", ""]
    lines += [f"{i}. {r}" for i, r in enumerate(result["reasons"], 1)]
    lines += ["", "## Evidence", "",
              f"- Suricata alerts: {', '.join(ev['alerts']) or 'none'}",
              f"- Failed SSH logins in the window: {ev['failed_logins']}",
              f"- Anomaly score from the model: {ev['anomaly'] if ev['anomaly'] is not None else 'no model loaded'}",
              "- Traffic in the 5 s window:", ""]
    lines += [f"  | {f} | {ev['features'].get(f)} |" for f in FEATURES] if ev["features"] else ["  (no traffic)"]
    lines += ["", "## Action taken", "",
              f"- {'Blocked at its access port by the SDN controller' if blocked and not blocked.get('error') else 'No block (see below)'}",
              f"- Controller reply: `{json.dumps(blocked)}`" if blocked else "- No block attempted", ""]
    path.write_text("\n".join(lines) + "\n")
    return path


def respond(result, do_block=True):
    """What happens on a breach: block the host, then write the incident report."""
    reason = "; ".join(result["reasons"])
    blocked = block_host(result["ip"], reason) if do_block else None
    return blocked, write_incident(result, blocked)


# --- running -------------------------------------------------------------------------------
def record(scores, responded):
    """Keep state.json (the newest picture, for the dashboard) and scores.jsonl (the history)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "state.json").write_text(json.dumps(
        {"time": time.time(), "hosts": scores, "bands": {b: sum(s["band"] == b for s in scores)
                                                         for b in ("normal", "suspicious", "breach")}},
        indent=1))
    with open(STATE_DIR / "scores.jsonl", "a") as f:
        for s in scores:
            if s["score"] > 0:
                f.write(json.dumps({"time": round(time.time(), 1), **s}) + "\n")
    for path in responded:
        with open(STATE_DIR / "incidents.jsonl", "a") as f:
            f.write(json.dumps({"time": round(time.time(), 1), "report": str(path)}) + "\n")


def once(lab, bundle, do_block=True, quiet=False):
    """Score the current window, respond to breaches, return the scores."""
    scores = assess(lab, bundle)
    responded = []
    for s in scores:
        if s["band"] == "breach":
            blocked, path = respond(s, do_block)
            s["blocked"] = blocked
            responded.append(path)
    record(scores, responded)
    if not quiet:
        for s in scores:
            print(f"{s['score']:3d}/100 {s['band']:10} {s['host']:8} {s['ip']:15}")
            for r in s["reasons"]:
                print(f"           - {r}")
        if not scores:
            print("no host sent anything in the last window")
    return scores


def self_check():
    """Run the scoring table through worked examples, so the logic can be checked without
    generating any attack traffic. Each case states what it is and what it must come out as."""
    cases = [
        ("a quiet host", {}, [], [], 0.0, "normal"),
        ("a busy but normal host (web and DNS)",
         {"packets_per_s": 40, "bytes_per_s": 50000, "new_conns_per_s": 2,
          "distinct_dst_ports": 3, "distinct_dst_hosts": 2, "failed_handshakes": 0}, [], [], 0.15, "normal"),
        ("a single Suricata alert and nothing else",
         {"distinct_dst_ports": 2, "failed_handshakes": 0},
         [{"kind": "other", "signature": "ET INFO something"}], [], 0.1, "normal"),
        ("a port sweep: IDS signature, many ports, refused connections",
         {"packets_per_s": 300, "bytes_per_s": 20000, "new_conns_per_s": 150,
          "distinct_dst_ports": 200, "distinct_dst_hosts": 1, "failed_handshakes": 190},
         [{"kind": "scan", "signature": "FORTRESS scan: TCP SYN sweep (possible nmap -sS)"}], [], 0.9, "breach"),
        ("password guessing: IDS signature plus failed logins in the central log",
         {"packets_per_s": 20, "bytes_per_s": 4000, "new_conns_per_s": 4,
          "distinct_dst_ports": 1, "distinct_dst_hosts": 1, "failed_handshakes": 0},
         [{"kind": "bruteforce", "signature": "FORTRESS brute force: repeated SSH connections"}],
         [{"node": "r1", "time": 0, "user": "ubuntu", "src_ip": "x"}] * 8, 0.4, "breach"),
        ("a few failed logins: someone mistyping a password",
         {"distinct_dst_ports": 1, "failed_handshakes": 0}, [],
         [{"node": "r1", "time": 0, "user": "ubuntu", "src_ip": "x"}] * 2, 0.1, "normal"),
    ]
    failures = 0
    print("scoring self-check (no traffic generated: these are worked examples)\n")
    for name, feats, alerts, logins, value, want in cases:
        r = score_host("example", feats, alerts, logins, value)
        ok = r["band"] == want
        failures += not ok
        print(f"{'OK  ' if ok else 'FAIL'} {r['score']:3d}/100 {r['band']:10} (wanted {want:10}) {name}")
        for reason in r["reasons"]:
            print(f"              - {reason}")
    # A breach must never rest on one source alone.
    lone = score_host("example", {"distinct_dst_ports": 2}, [{"kind": "scan", "signature": "x"}], [], 1.0)
    ok = lone["band"] != "breach"
    failures += not ok
    print(f"\n{'OK  ' if ok else 'FAIL'} one signal plus a maximal anomaly score is {lone['score']}/100 "
          f"({lone['band']}): a breach always needs two independent signals")
    print(f"\n{len(cases) + 1 - failures}/{len(cases) + 1} checks passed")
    return failures


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--self-check", action="store_true", help="run the worked examples and exit")
    p.add_argument("--once", action="store_true", help="score the current window and exit")
    p.add_argument("--watch", action="store_true", help="keep scoring every --interval seconds")
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--model", default=str(anomaly.DEFAULT_MODEL))
    p.add_argument("--no-block", action="store_true", help="score and report, but never block")
    p.add_argument("--small", action="store_true")
    a = p.parse_args()

    if a.self_check:
        sys.exit(1 if self_check() else 0)

    lab = Lab(small=a.small)
    bundle = anomaly.load(a.model)
    if bundle is None:
        print(f"no model at {a.model}: scoring on the rules alone (train one with train_model.py)",
              file=sys.stderr)
    if a.watch:
        while True:
            once(lab, bundle, not a.no_block, quiet=True)
            time.sleep(a.interval)
    once(lab, bundle, not a.no_block)


if __name__ == "__main__":
    main()
