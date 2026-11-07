"""netgraph.py: the controller's picture of the network.

Two things:
  - the switch graph, built from the links Ryu discovers with LLDP (--observe-links):
    a networkx Graph whose nodes are datapath IDs and whose edges remember which port
    each cable remembers the port at both ends, e.g. G[s1][c1]["members"] == [{s1: 1, c1: 3}]; the two c1-c2 cables are two members of one edge (T10);
  - where each host is: (VLAN, MAC) -> (datapath ID, port), learned from the packets hosts send.

(Named netgraph, not topology, so it can't be confused with Ryu's ryu.topology or the
repo's topology/ folder.)
"""
import networkx as nx


class NetGraph:
    def __init__(self, core_dpids):
        self.g = nx.Graph()
        self.hosts = {}              # (vlan, mac) -> (dpid, port)
        self.core = set(core_dpids)  # used for link weights (paths.py)

    # --- switches and links -------------------------------------------------
    def add_switch(self, dpid):
        self.g.add_node(dpid)

    def remove_switch(self, dpid):
        if dpid in self.g:
            self.g.remove_node(dpid)
        self.hosts = {k: v for k, v in self.hosts.items() if v[0] != dpid}

    def add_link(self, a, pa, b, pb):
        """Add one cable between switches a and b. Parallel cables (the two c1-c2 cables, T10)
        become members of the same edge. Returns "new" (first cable), "member" (another
        parallel cable) or None (already known: LLDP re-reports links all the time)."""
        if not self.g.has_edge(a, b):
            self.g.add_edge(a, b, members=[{a: pa, b: pb}])
            return "new"
        members = self.g[a][b]["members"]
        if any(m[a] == pa for m in members):
            return None
        members.append({a: pa, b: pb})
        return "member"

    def remove_cable(self, a, pa):
        """The cable on (a, port pa) is gone. Returns (neighbour, "member"|"link") if something
        changed: "member" = other parallel cables remain, "link" = the switches lost their
        last cable and the edge is gone. None if pa wasn't a known cable."""
        for nbr in list(self.g.neighbors(a)) if a in self.g else []:
            members = self.g[a][nbr]["members"]
            left = [m for m in members if m[a] != pa]
            if len(left) == len(members):
                continue
            if left:
                self.g[a][nbr]["members"] = left
                return nbr, "member"
            self.g.remove_edge(a, nbr)
            return nbr, "link"
        return None

    def ports(self, a, b):
        """a's ports towards neighbour b (one per cable), sorted."""
        return sorted(m[a] for m in self.g[a][b]["members"])

    def remove_link(self, a, b):
        """Returns True if the link was there."""
        if self.g.has_edge(a, b):
            self.g.remove_edge(a, b)
            return True
        return False

    def link_on_port(self, dpid, port):
        """The neighbour switch reached through (dpid, port), or None."""
        for nbr in self.g.neighbors(dpid) if dpid in self.g else []:
            if any(m[dpid] == port for m in self.g[dpid][nbr]["members"]):
                return nbr
        return None

    def links(self):
        return [(a, b) for a, b in self.g.edges()]

    # --- hosts -----------------------------------------------------------------
    def learn(self, vlan, mac, dpid, port):
        """Remember where a host is. Returns the old location if it moved (else None)."""
        old = self.hosts.get((vlan, mac))
        self.hosts[(vlan, mac)] = (dpid, port)
        return old if old and old != (dpid, port) else None

    def forget_port(self, dpid, port):
        """A port went down: forget the hosts behind it. Returns the (vlan, mac) keys removed."""
        gone = [k for k, v in self.hosts.items() if v == (dpid, port)]
        for k in gone:
            del self.hosts[k]
        return gone
