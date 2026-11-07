"""vlan.py: what every switch port is, and which VLAN it belongs to (decisions D10).

OVS port tags only work in NORMAL mode, so under the controller VLANs are our job. The
topology names every port after what it connects to (decisions D16), e.g. on s1:
    s1-cmd1  -> access port for host cmd1      -> VLAN 10, untagged on the wire
    s1-c1    -> link to core switch c1         -> tagged, all VLANs
    c1-r1    -> trunk to gateway router r1     -> tagged, all VLANs
    c1-ids1  -> ids1's mirror port             -> nothing forwarded there (T17 mirrors into it)
    anything else (e.g. a rogue host plugged in later) -> unknown -> dropped
so this module only needs fortress.yaml and the port name to know a port's role.
"""


class PortMap:
    def __init__(self, lab):
        self.lab = lab
        self.switches = set(lab.core_switches + lab.access_switches)
        self.trunk_routers = set(lab.gateway_routers())
        # access hosts: name -> {vlan, mac}; the monitor host is an access host too.
        self.access = {h["name"]: {"vlan": h["vlan"], "mac": h["mac"]}
                       for h in lab.hosts.values() if h["vlan"] is not None}
        mon = lab.monitor
        self.access[mon["name"]] = {"vlan": mon["vlan"], "mac": mon["mac"]}
        self.ids = lab.ids["host"]
        self.vlans = set(lab.vlans)

    def role(self, switch, port_name):
        """{'kind': 'access'|'trunk'|'link'|'ids'|'unknown', 'vlan', 'mac', 'peer'} for a port."""
        prefix = f"{switch}-"
        peer = port_name[len(prefix):] if port_name.startswith(prefix) else ""
        if peer in self.access:
            a = self.access[peer]
            return {"kind": "access", "vlan": a["vlan"], "mac": a["mac"], "peer": peer}
        if peer in self.trunk_routers:
            return {"kind": "trunk", "vlan": None, "mac": None, "peer": peer}
        if peer in self.switches or (peer[:-1] in self.switches and peer[-1:].isalpha()):
            # s1-c1, or one of the parallel core cables c1-c2a / c1-c2b (T10, decisions D24)
            return {"kind": "link", "vlan": None, "mac": None, "peer": peer.rstrip("ab") if peer not in self.switches else peer}
        if peer == self.ids:
            return {"kind": "ids", "vlan": None, "mac": None, "peer": peer}
        return {"kind": "unknown", "vlan": None, "mac": None, "peer": peer}
