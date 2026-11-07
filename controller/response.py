"""response.py: cut a host off at its own switch port, and let it back on (T21).

This is the "respond" end of detect -> explain -> block -> report. The threat engine calls
POST /fortress/block when a host's score reaches 70 (see rest_api.py), and this module does
the work:

  * a drop rule at the host's **own access port**, above the rule that normally admits it, so
    nothing it sends gets past the first switch. Blocking at the edge rather than in the middle
    of the network means the attacker's traffic never reaches the core at all, and no other
    host's traffic is touched;
  * the forwarding rules pointing at that host are removed, so nobody can reach it either;
  * an expiry time, so a wrong block heals itself instead of needing someone at a keyboard.

The host is found from `fortress.yaml` (its zone's access switch, and the port named after it),
not from what the controller has learned, so a host can be blocked even if it has gone quiet.
"""
import time

from pipeline import PRIO_BLOCK, T_INGRESS

DEFAULT_SECONDS = 300


class Responder:
    def __init__(self, ctrl):
        self.ctrl = ctrl
        self.blocked = {}        # ip -> record, the hosts cut off right now
        self.history = []        # every block and unblock, for the dashboard and the report
        # Only real zone hosts can be blocked. The monitoring host is deliberately left out:
        # it is exempt from zone isolation (decisions D27) and cutting it off would blind
        # Prometheus and the dashboard at the exact moment they are needed most.
        self.by_ip = {h["ip"]: h for h in ctrl.lab.hosts.values()}

    # --- finding the host's port ----------------------------------------------------
    def locate(self, ip):
        """{switch, dpid, port, port_name, mac, vlan} for an address, or None if it isn't a
        host on an access port (a router, the monitor, or an address we don't know)."""
        host = self.by_ip.get(ip)
        if host is None or not host.get("switch"):
            return None
        want = f"{host['switch']}-{host['name']}"
        for dpid, ports in self.ctrl.roles.items():
            if self.ctrl.swname(dpid) != host["switch"]:
                continue
            for port_no, role in ports.items():
                if role.get("name") == want:
                    return {"switch": host["switch"], "dpid": dpid, "port": port_no,
                            "port_name": want, "mac": host["mac"], "vlan": host["vlan"]}
        return None

    # --- blocking -------------------------------------------------------------------
    def block(self, ip, reason="", seconds=DEFAULT_SECONDS):
        """Isolate a host. Returns a record, or {"error": ...} if it can't be done."""
        if ip in self.blocked:
            # The threat engine asks again every few seconds while a host stays in breach.
            # Refresh the expiry instead of reinstalling the rule and filling the history.
            record = self.blocked[ip]
            record["until"] = time.time() + seconds if seconds else None
            return {**record, "already": True}
        where = self.locate(ip)
        if where is None:
            return {"error": f"{ip} is not a host on an access port; nothing to block"}
        dp = self.ctrl.dps.get(where["dpid"])
        if dp is None:
            return {"error": f"switch {where['switch']} is not connected"}
        p = dp.ofproto_parser
        # No actions at all = drop. Matching the port AND the MAC means a device that swaps its
        # address on that port is still caught by the ordinary port-security rule underneath.
        self.ctrl.add_flow(dp, T_INGRESS, PRIO_BLOCK,
                           p.OFPMatch(in_port=where["port"], eth_src=where["mac"]))
        # Nobody can reach it either: drop the rules that forward to it.
        self.ctrl.uninstall(where["vlan"], where["mac"])
        now = time.time()
        record = {"ip": ip, "host": self.by_ip[ip]["name"], "reason": reason, "since": now,
                  "until": now + seconds if seconds else None, "seconds": seconds,
                  "switch": where["switch"], "port": where["port_name"]}
        self.blocked[ip] = record
        self.history.append({**record, "event": "block"})
        self.ctrl.logger.info("BLOCK %s (%s) at %s: %s", record["host"], ip, record["port"], reason or "no reason given")
        return record

    def unblock(self, ip, why="asked to"):
        """Let a host back on. It is re-learned as soon as it sends anything."""
        record = self.blocked.pop(ip, None)
        if record is None:
            return {"error": f"{ip} is not blocked"}
        where = self.locate(ip)
        dp = self.ctrl.dps.get(where["dpid"]) if where else None
        if dp is not None:
            p = dp.ofproto_parser
            self.ctrl.delete_flows(dp, T_INGRESS,
                                   p.OFPMatch(in_port=where["port"], eth_src=where["mac"]),
                                   priority=PRIO_BLOCK)
        out = {**record, "event": "unblock", "why": why, "blocked_for": round(time.time() - record["since"], 1)}
        self.history.append(out)
        self.ctrl.logger.info("UNBLOCK %s (%s) after %.0f s: %s", record["host"], ip, out["blocked_for"], why)
        return out

    def expire(self):
        """Unblock anything whose time is up. Called on a timer by the controller."""
        now = time.time()
        for ip, record in list(self.blocked.items()):
            if record["until"] and now >= record["until"]:
                self.unblock(ip, why=f"the {record['seconds']:.0f} s block expired")

    def status(self):
        return {"blocked": sorted(self.blocked.values(), key=lambda b: b["since"]),
                "history": self.history[-20:]}
