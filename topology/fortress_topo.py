#!/usr/bin/env python3
"""fortress_topo.py: builds the Fortress-Net lab in Mininet from config/fortress.yaml.

Run it inside the VM as root (Mininet needs root), from the Mac:
    ./scripts/vm/vm.sh sudo python3 topology/fortress_topo.py --standalone          # lab + CLI
    ./scripts/vm/vm.sh sudo python3 topology/fortress_topo.py --standalone --check  # build, test, exit

Flags:
    --standalone      switches act as normal learning switches with RSTP (until T08)
    --controller-ip   where the Ryu controller listens (controller mode, from T08)
    --small           3 zones, one host each (+ the attacker), for 4 GB laptops
    --check           run the health checks, print the results and exit (no CLI)

What gets built (see docs/decisions.md D10-D12):
    cores c1, c2 joined by a trunk; access switches s1..s5, each with an uplink to both cores;
    zone hosts on VLAN-tagged access ports; r1/r2 with a trunk into c1/c2 and one VLAN
    sub-interface per zone; r3 (FOB) with its own LAN switch s6; r4 (ISP) with an internet host;
    /30 links between routers; ids1 on both cores (parked until T17); mon0 in the VM's root namespace.
"""
import argparse
import ipaddress
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # find labconfig/checks next to this file

from mininet.cli import CLI
from mininet.log import info, setLogLevel
from mininet.net import Mininet
from mininet.node import Host, OVSSwitch, RemoteController

import checks
from labconfig import Lab

OF_PORT = 6653
PARK_VLAN = 4094   # unused VLAN for ids1's ports, so they receive nothing until the mirrors exist (T17)


class LinuxRouter(Host):
    """A Mininet host that forwards IP packets between its interfaces, i.e. a router."""

    def config(self, **params):
        super().config(**params)
        # net.* sysctls are per network namespace, so this only affects this router.
        self.cmd("sysctl -qw net.ipv4.ip_forward=1")
        # Reverse-path filtering drops packets that arrive on "unexpected" interfaces, which
        # breaks asymmetric paths after a failover. Routers in this lab turn it off.
        self.cmd("sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0")


def dpid(n):
    """OpenFlow datapath ID as 16 hex digits. Set explicitly, because Mininet would
    otherwise derive it from the digits in the name and c1 and s1 would both get 1."""
    return f"{n:016x}"


class FortressNet:
    """Builds and configures the lab. `ports` remembers how every switch port must be set up."""

    def __init__(self, lab, args):
        self.lab = lab
        self.args = args
        self.ports = {}   # switch port name -> {"switch", "tag", "trunk", "edge"}
        self.net = Mininet(controller=None, switch=OVSSwitch, build=False,
                           autoSetMacs=False, ipBase="100.64.0.0/10", waitConnected=False)

    # --- Building --------------------------------------------------------------
    def link(self, a, b, a_if, b_if):
        self.net.addLink(a, b, intfName1=a_if, intfName2=b_if)

    def switch_port(self, name, switch, tag=None, trunk=False, edge=False):
        self.ports[name] = {"switch": switch, "tag": tag, "trunk": trunk, "edge": edge}

    def build(self):
        lab, net, args = self.lab, self.net, self.args
        base_mode = "standalone" if args.standalone else "secure"
        if not args.standalone:
            net.addController("ryu", controller=RemoteController, ip=args.controller_ip, port=OF_PORT)

        # Switches: 0x1xx cores, 0x2xx access, 0x3xx FOB.
        for i, s in enumerate(lab.core_switches, 1):
            net.addSwitch(s, dpid=dpid(0x100 + i), failMode=base_mode, protocols="OpenFlow13")
        for i, s in enumerate(lab.access_switches, 1):
            net.addSwitch(s, dpid=dpid(0x200 + i), failMode=base_mode, protocols="OpenFlow13")
        # The FOB LAN switch sits outside the base's SDN domain: always a plain learning switch.
        net.addSwitch(lab.fob_switch, dpid=dpid(0x301), failMode="standalone", protocols="OpenFlow13")

        c1, c2 = lab.core_switches
        # Core-to-core trunk (T10 turns this into an LACP bond).
        self.link(c1, c2, f"{c1}-{c2}", f"{c2}-{c1}")
        self.switch_port(f"{c1}-{c2}", c1, trunk=True)
        self.switch_port(f"{c2}-{c1}", c2, trunk=True)
        # Every access switch has an uplink to both cores. These redundant uplinks form loops,
        # which is why standalone mode needs RSTP.
        for s in lab.access_switches:
            for c in lab.core_switches:
                self.link(s, c, f"{s}-{c}", f"{c}-{s}")
                self.switch_port(f"{s}-{c}", s, trunk=True)
                self.switch_port(f"{c}-{s}", c, trunk=True)

        # Zone, FOB and internet hosts.
        for h in lab.hosts.values():
            static_ip = f"{h['ip']}/{h['prefixlen']}"
            net.addHost(h["name"], ip=static_ip, mac=h["mac"], defaultRoute=f"via {h['gateway']}")
            if h["zone"] == "internet":
                continue  # linked to r4 below
            sw = h["switch"]
            self.link(h["name"], sw, f"{h['name']}-eth0", f"{sw}-{h['name']}")
            self.switch_port(f"{sw}-{h['name']}", sw, tag=h["vlan"], edge=True)

        # Routers.
        for r in lab.routers:
            net.addHost(r, cls=LinuxRouter, ip=None)
        # r1/r2: one trunk each into "their" core switch, carrying every zone VLAN.
        for r in lab.gateway_routers():
            core = lab.routers[r]["trunk_to"]
            self.link(r, core, f"{r}-lan", f"{core}-{r}")
            self.switch_port(f"{core}-{r}", core, trunk=True, edge=True)
        # Point-to-point /30 links between routers (no switch in between).
        for l in lab.links:
            self.link(l["a"], l["b"], l["a_if"], l["b_if"])
        # r3's LAN at the FOB.
        r3 = lab.router_with_role("fob")
        self.link(r3, lab.fob_switch, f"{r3}-lan", f"{lab.fob_switch}-{r3}")
        self.switch_port(f"{lab.fob_switch}-{r3}", lab.fob_switch, edge=True)
        # r4 and the simulated internet: one host straight on r4.
        r4 = lab.router_with_role("isp")
        inet_hosts = [h for h in lab.hosts.values() if h["zone"] == "internet"]
        if len(inet_hosts) != 1:
            sys.exit("fortress_topo.py supports exactly one internet host (it is cabled straight to r4)")
        self.link(r4, inet_hosts[0]["name"], f"{r4}-inet", f"{inet_hosts[0]['name']}-eth0")

        # ids1: one port on each core, parked on an unused VLAN until the mirrors exist (D13, T17).
        ids = lab.ids["host"]
        net.addHost(ids, ip=None)
        for c in lab.ids["attach_to"]:
            self.link(ids, c, f"{ids}-{c}", f"{c}-{ids}")
            self.switch_port(f"{c}-{ids}", c, tag=PARK_VLAN, edge=True)

        # mon0: in the VM's root namespace, so tools on the VM can reach the lab (D12).
        mon = lab.monitor
        net.addHost(mon["name"], inNamespace=False, ip=None)
        self.link(mon["name"], mon["switch"], f"{mon['name']}-eth0", f"{mon['switch']}-{mon['name']}")
        self.switch_port(f"{mon['switch']}-{mon['name']}", mon["switch"], tag=mon["vlan"], edge=True)

    # --- Configuring after start -------------------------------------------------
    def configure_switches(self):
        """VLAN tags, trunks and (standalone mode) RSTP. See D10 and D11."""
        lab = self.lab
        trunks = ",".join(str(v) for v in lab.vlans)
        for port, p in self.ports.items():
            sw = self.net[p["switch"]]
            settings = []
            if p["tag"] is not None:
                settings.append(f"tag={p['tag']}")          # access port: one VLAN, untagged on the wire
            if p["trunk"]:
                settings += ["vlan_mode=trunk", f"trunks={trunks}"]  # carries every zone VLAN, tagged
            if p["edge"]:
                # Edge ports lead to hosts/routers, never to another switch, so RSTP can
                # forward on them immediately instead of waiting for the timers.
                settings.append("other_config:rstp-port-admin-edge=true")
            if settings:
                sw.vsctl("set", "port", port, *settings)
        if self.args.standalone:
            # Make the cores the RSTP root (c1) and backup root (c2), so the tree is predictable:
            # each access switch forwards on its c1 uplink and blocks its c2 uplink.
            for s in lab.core_switches + lab.access_switches:
                prio = {lab.core_switches[0]: 4096, lab.core_switches[1]: 8192}.get(s, 32768)
                self.net[s].vsctl("set", "bridge", s, "rstp_enable=true",
                                  f"other_config:rstp-priority={prio}")

    def configure_routers(self):
        """Addresses on every router interface, plus the VLAN sub-interfaces on r1/r2."""
        lab, net = self.lab, self.net
        for r in lab.gateway_routers():
            node = net[r]
            for z in lab.zones.values():
                sub = f"{r}-lan.{z['vlan']}"   # e.g. r1-lan.10 = VLAN 10 on r1's trunk
                node.cmd(f"ip link add link {r}-lan name {sub} type vlan id {z['vlan']}")
                node.cmd(f"ip addr add {lab.zone_ip(r, z['name'])}/{z['prefixlen']} dev {sub}")
                node.cmd(f"ip link set {sub} up")
        for r, ifaces in lab.router_ifaces.items():
            for i in ifaces:
                net[r].cmd(f"ip addr add {i['ip']}/{i['prefixlen']} dev {i['name']}")
        r3, r4 = lab.router_with_role("fob"), lab.router_with_role("isp")
        net[r3].cmd(f"ip addr add {lab.fob['gateway']}/{lab.fob['prefixlen']} dev {r3}-lan")
        net[r4].cmd(f"ip addr add {lab.internet['gateway']}/{lab.internet['prefixlen']} dev {r4}-inet")

    def configure_monitor(self):
        """mon0 is in the VM's root namespace: give it its address and routes into the lab.

        Safety check: never add a route that overlaps one of the VM's own networks
        (for example the Multipass network the Mac uses to reach the VM).
        """
        mon = self.lab.monitor
        node = self.net[mon["name"]]
        dev = f"{mon['name']}-eth0"
        node.cmd(f"ip link set {dev} address {mon['mac']}")
        node.cmd(f"ip addr add {mon['ip']}/{mon['prefixlen']} dev {dev}")
        own = [ipaddress.ip_interface(a).network for a in
               subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True)
               .stdout.split() if "/" in a and not a.startswith(("127.", mon["ip"]))]
        mon_net = ipaddress.ip_network(f"{mon['ip']}/{mon['prefixlen']}", strict=False)
        for subnet in self.lab.lab_subnets():
            net = ipaddress.ip_network(subnet)
            if net == mon_net:
                continue
            if any(net.overlaps(o) for o in own):
                info(f"*** mon0: NOT adding route {net}: it overlaps one of the VM's own networks\n")
                continue
            node.cmd(f"ip route replace {net} via {mon['gateway']} dev {dev}")

    def wait_for_rstp(self, timeout=40):
        """Standalone mode: wait until RSTP has finished, i.e. every Root and Designated port on
        every base switch is Forwarding. (Alternate/Backup ports stay Discarding: that's RSTP
        blocking the loops.) Until then, packets are dropped. Returns the seconds it took."""
        bridges = self.lab.core_switches + self.lab.access_switches
        start = time.time()
        while time.time() - start < timeout:
            waiting = 0
            for s in bridges:
                for line in self.net[s].cmd(f"ovs-appctl rstp/show {s}").splitlines():
                    if re.search(r"\b(Root|Designated)\s+(Discarding|Learning)\b", line):
                        waiting += 1
            if waiting == 0:
                return time.time() - start
            time.sleep(0.5)
        info(f"*** RSTP still converging after {timeout} s; checks may fail\n")
        return timeout

    def start(self):
        self.build()
        self.net.build()
        self.net.start()
        self.configure_switches()
        self.configure_routers()
        self.configure_monitor()
        return self.net


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Build the Fortress-Net Mininet lab from config/fortress.yaml")
    p.add_argument("--standalone", action="store_true",
                   help="switches run as learning switches with RSTP, no controller (until T08)")
    p.add_argument("--controller-ip", default="127.0.0.1", help="Ryu controller address (controller mode)")
    p.add_argument("--small", action="store_true", help="3 zones and fewer hosts, for 4 GB laptops")
    p.add_argument("--check", action="store_true", help="run the health checks and exit instead of opening the CLI")
    return p.parse_args(argv)


def main():
    args = parse_args()
    setLogLevel("info")
    lab = Lab(small=args.small)
    fnet = FortressNet(lab, args)
    net = fnet.start()
    try:
        if args.standalone:
            info("*** Waiting for RSTP to converge\n")
            info(f"*** RSTP converged in {fnet.wait_for_rstp():.1f} s\n")
        failures = checks.run_all(net, lab, args)
        if not args.check:
            CLI(net)
    finally:
        net.stop()
    if args.check:
        sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
