"""fortress_ctrl.py: the Fortress-Net SDN controller (one Ryu app, decisions D14).

Run (the topology starts it for you, see topology/services.py):
    ~/venvs/ryu/bin/ryu-manager --observe-links controller/fortress_ctrl.py

OpenFlow 1.3 pipeline on every base switch (c1, c2, s1..s5):

  table 0  INGRESS   which ports may send at all, and VLAN tagging (vlan.py):
                     access port -> only its own host's MAC, push the port's VLAN tag, no DHCP
                     server replies unless it is srv1's port; trunk/link port -> must be tagged;
                     ids/unknown ports -> no rule -> dropped. IPv6 is dropped (the lab is IPv4).
  table 1  POLICY    zone isolation (T13): drop IPv4 between zones that may never talk.
                     Table 0 also holds the per-host block rules the threat engine asks for (T21).
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
from ryu.lib import hub
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
from pipeline import (PRIO_DHCP_GUARD, PRIO_DROP_IPV6, PRIO_FORWARD, PRIO_ISOLATION,
                      PRIO_MONITOR, PRIO_PORT, PRIO_QOS_BULK, PRIO_QOS_MISSION,
                      T_FORWARD, T_INGRESS, T_POLICY, T_QOS, VID_PRESENT)
from rest_api import APP_KEY, FortressRest
from response import Responder
from vlan import PortMap

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
        self.reroutes = []      # every reroute event (also in reroutes.jsonl)
        # T11: "reactive" = the controller reroutes after a failure (T09);
        # "proactive" = every rule also gets a precomputed backup port in an OpenFlow
        # fast-failover group, so the switch itself fails over in the data plane.
        self.mode = os.environ.get("FORTRESS_MODE", "reactive")
        self.groups = {}        # (dpid, vlan, mac) -> group id (proactive mode)
        self.next_group = {}    # dpid -> next free group id
        self.lags = {}          # (dpid, neighbour) -> ports in its select group (T10, parallel cables)
        self.mac_names = {h["mac"]: h["name"] for h in self.lab.hosts.values()}
        self.dhcp_server = self.lab.host_with_role("dhcp-dns")["name"]
        self.mac_names[self.lab.monitor["mac"]] = self.lab.monitor["name"]
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # T21: blocking and unblocking hosts, plus a timer so a block heals itself.
        self.responder = Responder(self)
        hub.spawn(self._expire_blocks)
        kwargs["wsgi"].register(FortressRest, {APP_KEY: self})

    def _expire_blocks(self):
        while True:
            hub.sleep(2)
            self.responder.expire()

    # --- small OpenFlow helpers ------------------------------------------------------
    def add_flow(self, dp, table, priority, match, actions=None, goto=None):
        p, ofp = dp.ofproto_parser, dp.ofproto
        inst = []
        if actions:
            inst.append(p.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions))
        if goto is not None:
            inst.append(p.OFPInstructionGotoTable(goto))
        dp.send_msg(p.OFPFlowMod(datapath=dp, table_id=table, priority=priority, match=match, instructions=inst))

    def delete_flows(self, dp, table, match, priority=None):
        """Delete rules in a table. Without a priority this is OpenFlow's non-strict delete,
        which also removes rules whose match is *more specific* than the one given; with a
        priority it deletes only the exact rule (needed when lifting a block, T21, so the
        port's ordinary admit rule survives)."""
        p, ofp = dp.ofproto_parser, dp.ofproto
        strict = priority is not None
        dp.send_msg(p.OFPFlowMod(datapath=dp, table_id=table,
                                 command=ofp.OFPFC_DELETE_STRICT if strict else ofp.OFPFC_DELETE,
                                 priority=priority or 0,
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
        self.add_flow(dp, T_INGRESS, PRIO_DROP_IPV6, p.OFPMatch(eth_type=ether_types.ETH_TYPE_IPV6))
        self.add_flow(dp, T_POLICY, 0, p.OFPMatch(), goto=T_QOS)
        self.install_policy(dp)
        self.add_flow(dp, T_QOS, 0, p.OFPMatch(), goto=T_FORWARD)
        self.install_qos(dp)
        # Table-miss in FORWARD: send the whole packet to the controller.
        self.add_flow(dp, T_FORWARD, 0, p.OFPMatch(),
                      [p.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        dp.send_msg(p.OFPPortDescStatsRequest(dp, 0))
        self.logger.info("switch %s connected", self.swname(dp.id))

    def install_policy(self, dp):
        """T13 POLICY table (decisions D27): drop IPv4 between zones that may never talk
        (neither side's "reach" in fortress.yaml lists the other), e.g. Barracks <-> Intelligence.
        Zones where one side may start connections pass here; the routers' stateful firewall
        decides who may start. The monitoring host (mon0) is exempt, so Prometheus can see everything."""
        p = dp.ofproto_parser
        mask = lambda z: (str(self.lab.zones[z]["net"].network_address), str(self.lab.zones[z]["net"].netmask))
        mon = self.lab.monitor["ip"]
        for field in ("ipv4_src", "ipv4_dst"):
            self.add_flow(dp, T_POLICY, PRIO_MONITOR, p.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, **{field: mon}), goto=T_QOS)
        for a, b in self.lab.blocked_pairs():
            self.add_flow(dp, T_POLICY, PRIO_ISOLATION, p.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                                       ipv4_src=mask(a), ipv4_dst=mask(b)))   # no actions = drop

    def install_qos(self, dp):
        """T12: QOS table rules from fortress.yaml (decisions D26). Mission zones (Command,
        Communications) go to the mission queue whether they send or receive; bulk zones
        (Barracks) go to the bulk queue when they send. Everything else uses queue 0.
        set_queue only marks the packet; the queue is used when it leaves through a port
        with HTB queues (the topology sets those up)."""
        p = dp.ofproto_parser
        for cname, c in self.lab.qos["classes"].items():
            prio = PRIO_QOS_MISSION if cname == "mission" else PRIO_QOS_BULK
            for z in c["zones"]:
                if z not in self.lab.zones:
                    continue
                net = self.lab.zones[z]["net"]
                fields = ["ipv4_src", "ipv4_dst"] if cname == "mission" else ["ipv4_src"]
                for field in fields:
                    match = p.OFPMatch(eth_type=ether_types.ETH_TYPE_IP,
                                       **{field: (str(net.network_address), str(net.netmask))})
                    self.add_flow(dp, T_QOS, prio, match, [p.OFPActionSetQueue(c["queue"])], goto=T_FORWARD)

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
            # Untagged frame from the host that belongs on this port: tag it with the port's VLAN.
            # Port security (T13): only that host's MAC (fixed in fortress.yaml, D16) is accepted,
            # so a different device on the port, or a spoofed MAC, gets no rule and is dropped.
            self.add_flow(dp, T_INGRESS, PRIO_PORT, p.OFPMatch(in_port=port_no, vlan_vid=0, eth_src=role["mac"]),
                          [p.OFPActionPushVlan(ether_types.ETH_TYPE_8021Q),
                           p.OFPActionSetField(vlan_vid=VID_PRESENT | role["vlan"])], goto=T_POLICY)
            if role["peer"] != self.dhcp_server:
                # Rogue DHCP protection (T13): only srv1's port may send DHCP server replies
                # (UDP source port 67). The relays on r1/r2 arrive on trunk ports and are allowed.
                self.add_flow(dp, T_INGRESS, PRIO_DHCP_GUARD, p.OFPMatch(in_port=port_no, eth_type=ether_types.ETH_TYPE_IP,
                                                             ip_proto=17, udp_src=67))
        elif role["kind"] in ("trunk", "link"):
            # Router trunks and switch links carry tagged frames only.
            self.add_flow(dp, T_INGRESS, PRIO_PORT, p.OFPMatch(in_port=port_no, vlan_vid=(VID_PRESENT, VID_PRESENT)),
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
            self.cable_gone(dp.id, port.port_no)
            for key in self.net.forget_port(dp.id, port.port_no):
                self.uninstall(*key)

    def cable_gone(self, dpid, port_no):
        """A cable between two switches went down (T09/T10). If it was the last cable to that
        neighbour, the link is gone: reroute. If parallel cables remain (c1-c2), only the
        aggregation group loses a member and every path stays the same."""
        nbr = self.net.link_on_port(dpid, port_no)
        if nbr is None:
            return
        event = "link_down" if len(self.net.ports(dpid, nbr)) == 1 else "member_down"
        self.reroute(event, dpid, nbr, lambda: self.net.remove_cable(dpid, port_no))

    # --- links (from LLDP discovery) ---------------------------------------------------
    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add(self, ev):
        s, d = ev.link.src, ev.link.dst
        if self.net.link_on_port(s.dpid, s.port_no) is not None:
            return                                   # LLDP re-reporting a known cable
        event = "member_up" if self.net.g.has_edge(s.dpid, d.dpid) else "link_up"
        self.reroute(event, s.dpid, d.dpid, lambda: self.net.add_link(s.dpid, s.port_no, d.dpid, d.port_no))

    @set_ev_cls(topo_event.EventLinkDelete)
    def link_delete(self, ev):
        self.cable_gone(ev.link.src.dpid, ev.link.src.port_no)

    def link_actions(self, dp, nbr):
        """Actions that send a frame to neighbour switch nbr. One cable: output to its port.
        Several parallel cables (c1-c2, T10): an OpenFlow SELECT group with one bucket per
        cable. The switch hashes each flow onto one bucket (link aggregation, like a LAG) and
        skips buckets whose watched port is down, so a dead cable is bypassed in the data
        plane. Once a neighbour has had a group it keeps it, even with one cable left."""
        p, ofp = dp.ofproto_parser, dp.ofproto
        ports = self.net.ports(dp.id, nbr)
        key = (dp.id, nbr)
        if len(ports) == 1 and key not in self.lags:
            return [p.OFPActionOutput(ports[0])]
        gid = 0x10000 + nbr
        if self.lags.get(key) != tuple(ports):
            buckets = [p.OFPBucket(weight=1, watch_port=pt, watch_group=ofp.OFPG_ANY, actions=[p.OFPActionOutput(pt)])
                       for pt in ports]
            command = ofp.OFPGC_MODIFY if key in self.lags else ofp.OFPGC_ADD
            dp.send_msg(p.OFPGroupMod(datapath=dp, command=command, type_=ofp.OFPGT_SELECT,
                                      group_id=gid, buckets=buckets))
            self.lags[key] = tuple(ports)
        return [p.OFPActionGroup(gid)]

    # --- rerouting (T09) -----------------------------------------------------------------
    def all_paths(self):
        """{(vlan, mac): {switch: [switch path to the destination]}} for installed destinations."""
        out = {}
        for key in self.installed:
            loc = self.net.hosts.get(key)
            if not loc:
                continue
            out[key] = {self.swname(sw): [self.swname(x) for x in paths.path_to(self.net.g, sw, loc[0], self.core2)]
                        for sw in self.dps}
        return out

    def reroute(self, event, a, b, change):
        """Apply a topology change, recompute every destination's tree, and log what moved.
        One JSON line per event in /var/log/fortress/ryu/reroutes.jsonl:
          {time, event, link, changed: [{dst, vlan, switch, old, new}, ...]}"""
        t0 = time.time()
        before = self.all_paths()
        change()
        self.reinstall_all()
        after = self.all_paths()
        changed = []
        for key, per_switch in after.items():
            for sw, new in per_switch.items():
                old = before.get(key, {}).get(sw)
                if old is not None and old != new:
                    changed.append({"dst": self.mac_names.get(key[1], key[1]), "vlan": key[0],
                                    "switch": sw, "old": old, "new": new})
        if event == "link_up" and not changed:
            return      # links found by LLDP at start-up, before any traffic: nothing to log
        entry = {"time": t0, "event": event, "link": [self.swname(a), self.swname(b)],
                 "controller_ms": round((time.time() - t0) * 1000, 1), "changed": changed}
        self.reroutes.append(entry)
        with open(LOG_DIR / "reroutes.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")
        self.logger.info("%s %s-%s: %d paths changed in %.1f ms", event, entry["link"][0], entry["link"][1],
                         len(changed), entry["controller_ms"])

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
        return self.link_actions(self.dps[dpid], tree[dpid])

    def install(self, vlan, mac):
        """Rules for destination (vlan, mac) on every switch, along one shortest-path tree.
        Proactive mode: on every switch except the destination's, the rule points at a
        fast-failover group instead of a port (ff_actions)."""
        dst_dpid = self.net.hosts[(vlan, mac)][0]
        tree = paths.tree_to(self.net.g, dst_dpid, self.core2)
        for dpid, dp in self.dps.items():
            actions = self.out_actions(dpid, vlan, mac, tree)
            match = dp.ofproto_parser.OFPMatch(vlan_vid=VID_PRESENT | vlan, eth_dst=mac)
            if actions and self.mode == "proactive" and dpid != dst_dpid:
                actions = self.ff_actions(dp, vlan, mac, tree[dpid], dst_dpid, actions)
            if actions:
                self.add_flow(dp, T_FORWARD, PRIO_FORWARD, match, actions)
            else:
                self.delete_flows(dp, T_FORWARD, match)
        self.installed.add((vlan, mac))
        return tree

    def ff_actions(self, dp, vlan, mac, primary_nbr, dst_dpid, primary_actions):
        """T11: put the primary port and a backup port in an OpenFlow fast-failover group.
        The switch uses the first bucket whose watched port is up, so when the primary
        link dies it switches to the backup by itself, without asking the controller.
        The backup next hop is the best path that avoids the primary link (paths.py)."""
        p, ofp = dp.ofproto_parser, dp.ofproto
        backup_nbr = paths.backup_next_hop(self.net.g, dp.id, primary_nbr, dst_dpid, self.core2)
        if backup_nbr is None:
            return primary_actions                  # no alternative path: plain rule
        buckets = []
        for nbr in (primary_nbr, backup_nbr):
            acts = self.link_actions(dp, nbr)
            if isinstance(acts[0], p.OFPActionGroup):
                # Towards an aggregated link: the bucket is live while any of its cables is up.
                buckets.append(p.OFPBucket(watch_port=ofp.OFPP_ANY, watch_group=acts[0].group_id, actions=acts))
            else:
                buckets.append(p.OFPBucket(watch_port=acts[0].port, watch_group=ofp.OFPG_ANY, actions=acts))
        key = (dp.id, vlan, mac)
        if key in self.groups:
            command = ofp.OFPGC_MODIFY
        else:
            self.next_group[dp.id] = self.next_group.get(dp.id, 0) + 1
            self.groups[key] = self.next_group[dp.id]
            command = ofp.OFPGC_ADD
        dp.send_msg(p.OFPGroupMod(datapath=dp, command=command, type_=ofp.OFPGT_FF,
                                  group_id=self.groups[key], buckets=buckets))
        return [p.OFPActionGroup(self.groups[key])]

    def uninstall(self, vlan, mac):
        for dp in self.dps.values():
            self.delete_flows(dp, T_FORWARD, dp.ofproto_parser.OFPMatch(vlan_vid=VID_PRESENT | vlan, eth_dst=mac))
            gid = self.groups.pop((dp.id, vlan, mac), None)
            if gid is not None:
                p, ofp = dp.ofproto_parser, dp.ofproto
                dp.send_msg(p.OFPGroupMod(datapath=dp, command=ofp.OFPGC_DELETE, type_=ofp.OFPGT_FF, group_id=gid))
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
            "aggregated": {f"{self.swname(a)}-{self.swname(b)}": len(self.net.g[a][b]["members"])
                           for a, b in self.net.links() if len(self.net.g[a][b]["members"]) > 1},
            "hosts": sorted(({"vlan": v, "mac": m, "name": self.mac_names.get(m, m),
                              "switch": self.swname(d), "port": self.roles.get(d, {}).get(pt, {}).get("name", pt)}
                             for (v, m), (d, pt) in self.net.hosts.items()), key=lambda h: (h["vlan"], h["name"])),
            "mode": self.mode,
            "installed": len(self.installed),
            "ff_groups": len(self.groups),
            "reroutes": len(self.reroutes),
            **self.responder.status(),
            "last_reroute": self.reroutes[-1] if self.reroutes else None,
            "time": time.time(),
        }
