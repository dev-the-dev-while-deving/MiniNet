"""netgraph.py: the controller's picture of the network.

Two things:
  - the switch graph, built from the links Ryu discovers with LLDP (--observe-links):
    a networkx Graph whose nodes are datapath IDs and whose edges remember which port
    each end uses, e.g. G[s1][c1]["ports"] == {s1: 1, c1: 3};
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
        """Returns True if this is a new link (LLDP re-reports known links all the time)."""
        new = not self.g.has_edge(a, b)
        self.g.add_edge(a, b, ports={a: pa, b: pb})
        return new

    def remove_link(self, a, b):
        """Returns True if the link was there."""
        if self.g.has_edge(a, b):
            self.g.remove_edge(a, b)
            return True
        return False

    def link_on_port(self, dpid, port):
        """The neighbour switch reached through (dpid, port), or None."""
        for nbr in self.g.neighbors(dpid) if dpid in self.g else []:
            if self.g[dpid][nbr]["ports"][dpid] == port:
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
