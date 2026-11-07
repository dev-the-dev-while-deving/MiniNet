"""fortress_ctrl.py: the Fortress-Net SDN controller (one Ryu app, decisions D14).

Run (the topology starts it for you, see topology/services.py):
    ~/venvs/ryu/bin/ryu-manager --observe-links controller/fortress_ctrl.py

OpenFlow 1.3 pipeline on every base switch (c1, c2, s1..s5):

  table 0  INGRESS   which ports may send at all, and VLAN tagging (vlan.py):
                     access port -> push the port's VLAN tag; trunk/link port -> must be tagged;
                     ids/unknown ports -> no rule -> dropped. IPv6 is dropped (the lab is IPv4).
  table 1  POLICY    zone isolation and rogue-device rules (T13). For now: go to table 2.
  table 2  QOS       pick an output queue by traffic class (T12). For now: go to table 3.
  table 3  FORWARD   (VLAN, destination MAC) -> output port, installed by this app.
                     No match -> send the packet to the controller (packet-in).

On a packet-in the controller learns where the sender is, then either
  - floods it (broadcast, multicast or unknown destination): the controller itself sends a
    copy out of every host/router port in that VLAN on every switch. Copies never travel
    over switch-to-switch links, so the dual uplinks can't loop (no spanning tree needed);
  - or installs forwarding rules for the destination on every switch, following ONE
    shortest-path tree towards the destination's switch (paths.py), and sends the packet on.
Tags stay on across the cores and are popped at the destination's access port (D10).
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                       # vlan.py, netgraph.py, paths.py, rest_api.py
sys.path.insert(0, str(HERE.parent / "topology"))   # labconfig.py (reads config/fortress.yaml)

from ryu.app.wsgi import WSGIApplication
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib.packet import ether_types, ethernet, packet
from ryu.lib.packet import vlan as vlan_pkt
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event as topo_event

import paths
from labconfig import Lab
from netgraph import NetGraph
from rest_api import APP_KEY, FortressRest
from vlan import PortMap

T_INGRESS, T_POLICY, T_QOS, T_FORWARD = 0, 1, 2, 3
VID_PRESENT = 0x1000          # OpenFlow's "this frame has a VLAN tag" bit in vlan_vid
PRIO_FORWARD = 10
LOG_DIR = Path(os.environ.get("FORTRESS_LOG_DIR", "/var/log/fortress/ryu"))


class FortressController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lab = Lab()
        self.portmap = PortMap(self.lab)
        ids = self.lab.dpids()
        self.names = {dpid: name for name, dpid in ids.items()}      # 0x201 -> "s1"
        self.core2 = ids[self.lab.core_switches[1]]
        self.net = NetGraph([ids[c] for c in self.lab.core_switches])
        self.dps = {}           # dpid -> Ryu datapath (connected switches)
        self.roles = {}         # dpid -> {port_no: role} (vlan.py), filled from the port names
        self.installed = set()  # (vlan, mac) destinations that have forwarding rules
        self.mac_names = {h["mac"]: h["name"] for h in self.lab.hosts.values()}
        self.mac_names[self.lab.monitor["mac"]] = self.lab.monitor["name"]
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        kwargs["wsgi"].register(FortressRest, {APP_KEY: self})

    # --- small OpenFlow helpers ------------------------------------------------------
    def add_flow(self, dp, table, priority, match, actions=None, goto=None):
        p, ofp = dp.ofproto_parser, dp.ofproto
        inst = []
        if actions:
            inst.append(p.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions))
        if goto is not None:
            inst.append(p.OFPInstructionGotoTable(goto))
        dp.send_msg(p.OFPFlowMod(datapath=dp, table_id=table, priority=priority, match=match, instructions=inst))

    def delete_flows(self, dp, table, match):
        p, ofp = dp.ofproto_parser, dp.ofproto
        dp.send_msg(p.OFPFlowMod(datapath=dp, table_id=table, command=ofp.OFPFC_DELETE,
                                 out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY, match=match))

    def packet_out(self, dp, data, actions):
        p, ofp = dp.ofproto_parser, dp.ofproto
        dp.send_msg(p.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                   in_port=ofp.OFPP_CONTROLLER, actions=actions, data=data))

    def swname(self, dpid):
        return self.names.get(dpid, hex(dpid))

    # --- switches connecting -------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        """A switch connected: install the fixed parts of the pipeline, ask for its ports."""
        dp = ev.msg.datapath
        p, ofp = dp.ofproto_parser, dp.ofproto
        self.dps[dp.id] = dp
        self.net.add_switch(dp.id)
        # The lab is IPv4-only; dropping IPv6 early saves the controller from hosts' IPv6 chatter.
        self.add_flow(dp, T_INGRESS, 200, p.OFPMatch(eth_type=ether_types.ETH_TYPE_IPV6))
        self.add_flow(dp, T_POLICY, 0, p.OFPMatch(), goto=T_QOS)
        self.add_flow(dp, T_QOS, 0, p.OFPMatch(), goto=T_FORWARD)
        # Table-miss in FORWARD: send the whole packet to the controller.
        self.add_flow(dp, T_FORWARD, 0, p.OFPMatch(),
                      [p.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        dp.send_msg(p.OFPPortDescStatsRequest(dp, 0))
        self.logger.info("switch %s connected", self.swname(dp.id))

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc(self, ev):
        dp = ev.msg.datapath
        for port in ev.msg.body:
            self.port_added(dp, port.port_no, port.name.decode())

    def port_added(self, dp, port_no, port_name):
        """Give a port its INGRESS rule according to its role (vlan.py)."""
        if port_no > dp.ofproto.OFPP_MAX:          # the switch's own internal port
            return
        role = self.portmap.role(self.swname(dp.id), port_name)
        role["name"] = port_name
        self.roles.setdefault(dp.id, {})[port_no] = role
        p = dp.ofproto_parser
        if role["kind"] == "access":
            # Untagged frame from a host: tag it with the port's VLAN, then continue.
            self.add_flow(dp, T_INGRESS, 100, p.OFPMatch(in_port=port_no, vlan_vid=0),
                          [p.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                           p.OFPActionSetField(vlan_vid=VID_PRESENT | role["vlan"])], goto=T_POLICY)
        elif role["kind"] in ("trunk", "link"):
            # Router trunks and switch links carry tagged frames only.
            self.add_flow(dp, T_INGRESS, 100, p.OFPMatch(in_port=port_no, vlan_vid=(VID_PRESENT, VID_PRESENT)),
                          goto=T_POLICY)
        # ids and unknown ports get no rule: everything they send is dropped.

    @set_ev_cls(ofp_event.EventOFPStateChange, DEAD_DISPATCHER)
    def switch_gone(self, ev):
        dp = ev.datapath
        if dp.id in self.dps:
            del self.dps[dp.id]
            self.roles.pop(dp.id, None)
            self.net.remove_switch(dp.id)
            self.logger.info("switch %s disconnected", self.swname(dp.id))

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status(self, ev):
        """A port was added, removed, or went up/down."""
        msg = ev.msg
        dp, ofp, port = msg.datapath, msg.datapath.ofproto, msg.desc
        down = msg.reason == ofp.OFPPR_DELETE or bool(port.state & ofp.OFPPS_LINK_DOWN)
        if msg.reason == ofp.OFPPR_ADD or not down:
            self.port_added(dp, port.port_no, port.name.decode())
        if down:
            for key in self.net.forget_port(dp.id, port.port_no):
                self.uninstall(*key)

    # --- links (from LLDP discovery) ---------------------------------------------------
    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add(self, ev):
        s, d = ev.link.src, ev.link.dst
        if self.net.add_link(s.dpid, s.port_no, d.dpid, d.port_no):
            self.logger.info("link up %s-%s", self.swname(s.dpid), self.swname(d.dpid))
            self.reinstall_all()

    @set_ev_cls(topo_event.EventLinkDelete)
    def link_delete(self, ev):
        s, d = ev.link.src, ev.link.dst
        if self.net.remove_link(s.dpid, d.dpid):
            self.logger.info("link down %s-%s", self.swname(s.dpid), self.swname(d.dpid))
            self.reinstall_all()

    # --- forwarding ------------------------------------------------------------------------
    def out_actions(self, dpid, vlan, mac, tree):
        """Actions on switch dpid for frames to (vlan, mac), or None if it can't reach it."""
        loc = self.net.hosts.get((vlan, mac))
        if not loc or dpid not in self.dps:
            return None
        p = self.dps[dpid].ofproto_parser
        dst_dpid, dst_port = loc
        if dpid == dst_dpid:
            role = self.roles.get(dpid, {}).get(dst_port, {})
            pop = [p.OFPActionPopVlan()] if role.get("kind") == "access" else []
            return pop + [p.OFPActionOutput(dst_port)]
        if dpid not in tree:
            return None
        return [p.OFPActionOutput(self.net.g[dpid][tree[dpid]]["ports"][dpid])]

    def install(self, vlan, mac):
        """Rules for destination (vlan, mac) on every switch, along one shortest-path tree."""
        dst_dpid = self.net.hosts[(vlan, mac)][0]
        tree = paths.tree_to(self.net.g, dst_dpid, self.core2)
        for dpid, dp in self.dps.items():
            actions = self.out_actions(dpid, vlan, mac, tree)
            match = dp.ofproto_parser.OFPMatch(vlan_vid=VID_PRESENT | vlan, eth_dst=mac)
            if actions:
                self.add_flow(dp, T_FORWARD, PRIO_FORWARD, match, actions)
            else:
                self.delete_flows(dp, T_FORWARD, match)
        self.installed.add((vlan, mac))
        return tree

    def uninstall(self, vlan, mac):
        for dp in self.dps.values():
            self.delete_flows(dp, T_FORWARD, dp.ofproto_parser.OFPMatch(vlan_vid=VID_PRESENT | vlan, eth_dst=mac))
        self.installed.discard((vlan, mac))

    def reinstall_all(self):
        """The switch graph changed: recompute every installed destination's tree."""
        for key in list(self.installed):
            if key in self.net.hosts:
                self.install(*key)
            else:
                self.uninstall(*key)

    def flood(self, vlan, data, tagged, src):
        """Send a copy out of every host and router port in this VLAN, on every switch,
        except the port it came from. Nothing goes over switch-to-switch links."""
        for dpid, dp in self.dps.items():
            p = dp.ofproto_parser
            trunks, access = [], []
            for port_no, role in self.roles.get(dpid, {}).items():
                if (dpid, port_no) == src:
                    continue
                if role["kind"] == "trunk":
                    trunks.append(port_no)
                elif role["kind"] == "access" and role["vlan"] == vlan:
                    access.append(port_no)
            if not trunks and not access:
                continue
            actions = [] if tagged else [p.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                                         p.OFPActionSetField(vlan_vid=VID_PRESENT | vlan)]
            actions += [p.OFPActionOutput(t) for t in trunks]      # tagged towards routers
            if access:
                actions.append(p.OFPActionPopVlan())               # untagged towards hosts
                actions += [p.OFPActionOutput(a) for a in access]
            self.packet_out(dp, data, actions)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return                          # LLDP belongs to Ryu's topology discovery
        role = self.roles.get(dp.id, {}).get(in_port)
        tag = pkt.get_protocol(vlan_pkt.vlan)
        if role and role["kind"] == "link" and tag:
            # A packet that overtook its own rules: the next switch got it before the
            # flow-mod we sent it. Deliver it (no learning: links have no hosts behind them).
            if (tag.vid, eth.dst) in self.net.hosts:
                self.deliver(tag.vid, eth.dst, msg.data, tagged=True)
            return
        if not role or role["kind"] not in ("access", "trunk"):
            return                          # only hosts and routers originate traffic
        vlan = tag.vid if tag else role["vlan"]
        if vlan not in self.portmap.vlans:
            return
        # Learn where the sender is. If it moved (e.g. the VRRP virtual MAC after a failover,
        # now behind r2's trunk), its old rules point the wrong way: rebuild them.
        moved = self.net.learn(vlan, eth.src, dp.id, in_port)
        if moved and (vlan, eth.src) in self.installed:
            self.install(vlan, eth.src)
        multicast = int(eth.dst.split(":")[0], 16) & 1
        if multicast or (vlan, eth.dst) not in self.net.hosts:
            self.flood(vlan, msg.data, bool(tag), (dp.id, in_port))
            return
        self.install(vlan, eth.dst)
        self.deliver(vlan, eth.dst, msg.data, bool(tag))

    def deliver(self, vlan, mac, data, tagged):
        """Send one packet straight out of the destination's own port, on the destination's
        switch. Sending it out of the first hop instead races with the flow-mods we just sent
        to the other switches: OpenFlow doesn't order messages across switches, so the packet
        could reach the next switch before its rule and be lost (that cost the first DNS
        query in every zone 2 s until this was fixed)."""
        dst_dpid, dst_port = self.net.hosts[(vlan, mac)]
        dp = self.dps.get(dst_dpid)
        if dp is None:
            return
        p = dp.ofproto_parser
        actions = self.out_actions(dst_dpid, vlan, mac, {})
        if not tagged:
            actions = [p.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                       p.OFPActionSetField(vlan_vid=VID_PRESENT | vlan)] + actions
        self.packet_out(dp, data, actions)

    # --- status for the REST API ---------------------------------------------------------
    def status(self):
        return {
            "switches": sorted(self.swname(d) for d in self.dps),
            "links": sorted(sorted([self.swname(a), self.swname(b)]) for a, b in self.net.links()),
            "hosts": sorted(({"vlan": v, "mac": m, "name": self.mac_names.get(m, m),
                              "switch": self.swname(d), "port": self.roles.get(d, {}).get(pt, {}).get("name", pt)}
                             for (v, m), (d, pt) in self.net.hosts.items()), key=lambda h: (h["vlan"], h["name"])),
            "installed": len(self.installed),
            "time": time.time(),
        }
