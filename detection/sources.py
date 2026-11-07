"""sources.py: read the three things the threat engine reasons about (T20).

    Suricata alerts      /var/log/fortress/ids1/eve.json          (signatures, T17)
    traffic features     /var/log/fortress/collector/latest.json   (behaviour, T18)
    failed SSH logins    /var/log/fortress/srv2/remote/*.log       (the central log, T15)

Each one is a file on the VM's disk written by a different part of the lab, which is what makes
the correlation in T20 real: three independent sources agreeing about the same host.

Like features.py this module imports nothing heavy, because the topology's health checks read
it with the system Python while the threat engine reads it with the fortress venv.
"""
import json
import re
from datetime import datetime
from pathlib import Path

LOG = Path("/var/log/fortress")                       # same place services.py writes (D12)
COLLECTOR_LATEST = LOG / "collector" / "latest.json"

# The engine re-reads these files every few seconds, and both grow while the lab runs. Only the
# end of a file can hold anything recent enough to matter (alerts within 30 s, logins within
# 120 s), so reads are capped: the engine's cost then depends on the window, not on uptime.
TAIL_BYTES = 512 * 1024


def tail(path, max_bytes=TAIL_BYTES):
    """The last `max_bytes` of a file as whole lines (a cut first line is dropped)."""
    path = Path(path)
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()                  # drop the partial line we landed in
            return f.read().decode(errors="replace").splitlines()
    except OSError:
        return []

# Our own Suricata rules live in the local SID range (detection/suricata/local.rules).
# eve.json's "category" is a human-readable description ("Attempted Information Leak"), not the
# rule's classtype, so the SID is what reliably says WHICH kind of thing fired.
IDS_KINDS = {range(1000001, 1000010): "scan", range(1000010, 1000020): "bruteforce"}

FAILED_LOGIN = re.compile(r"^(?P<ts>\S+) (?P<node>\S+) \S*?:?Failed password for (?:invalid user )?"
                          r"(?P<user>\S+) from (?P<ip>\d+\.\d+\.\d+\.\d+)")


def ids_kind(sid):
    """"scan", "bruteforce", or "other" (an ET Open rule, if that ruleset was added)."""
    for sids, kind in IDS_KINDS.items():
        if sid in sids:
            return kind
    return "other"


def ids_alerts(ids_host="ids1", since=0.0, path=None):
    """Suricata alerts from eve.json, oldest first.

    Each entry: {time (epoch), kind, signature, sid, src_ip, dest_ip, dest_port, category}.
    `since` drops anything older, so a caller sees only what its own traffic caused.
    """
    path = Path(path) if path else LOG / ids_host / "eve.json"
    out = []
    if not path.exists():
        return out
    for line in tail(path):
        try:
            e = json.loads(line)
        except ValueError:
            continue                      # a line Suricata was still writing
        if e.get("event_type") != "alert":
            continue
        ts = datetime.strptime(e["timestamp"][:26], "%Y-%m-%dT%H:%M:%S.%f").timestamp()
        if ts < since:
            continue
        a = e.get("alert", {})
        out.append({"time": ts, "kind": ids_kind(a.get("signature_id") or 0),
                    "signature": a.get("signature", ""), "sid": a.get("signature_id"),
                    "category": a.get("category", ""), "src_ip": e.get("src_ip", ""),
                    "dest_ip": e.get("dest_ip", ""), "dest_port": e.get("dest_port")})
    return out


def latest_features(path=COLLECTOR_LATEST):
    """The newest window the collector wrote: {"time", "window", "hosts": [{...}, ...]}."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"time": 0, "window": 0, "hosts": []}


def failed_logins(syslog_node="srv2", since=0.0, log_dir=None):
    """Failed SSH password attempts, as collected centrally on srv2 (T15).

    Each entry: {time (epoch), node (where the attempt landed), user, src_ip}. These come from
    the *router's and server's own* sshd logs, forwarded over the lab network, so this signal is
    independent of anything the IDS or the collector saw.
    """
    folder = Path(log_dir) if log_dir else LOG / syslog_node / "remote"
    out = []
    if not folder.is_dir():
        return out
    for log in sorted(folder.glob("*.log")):
        for line in tail(log):
            m = FAILED_LOGIN.match(line)
            if not m:
                continue
            try:
                ts = datetime.fromisoformat(m.group("ts")).timestamp()
            except ValueError:
                continue
            if ts < since:
                continue
            out.append({"time": ts, "node": m.group("node"), "user": m.group("user"),
                        "src_ip": m.group("ip")})
    return sorted(out, key=lambda e: e["time"])
