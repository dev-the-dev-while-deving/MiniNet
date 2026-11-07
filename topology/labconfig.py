"""labconfig.py: loads config/fortress.yaml and works out everything derived from it.

fortress.yaml only lists the facts (subnets, host IPs, which routers share a link).
This module turns those facts into the concrete values the topology, the config
templates and the checks need, so that logic lives in exactly one place:

  - each host's MAC address (derived from its IP, so it never changes between runs)
  - each router's address on every link and in every zone
  - interface names (r1-r2, r1-lan.10, ...)
  - the reduced lab for --small

Nothing here touches the network; it only reads the YAML. It runs on the system python3.
"""
import ipaddress
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config" / "fortress.yaml"


def mac_for(ip):
    """Deterministic MAC for a host: 02:fe + the four IP bytes.

    02 = locally administered (never clashes with a real vendor MAC).
    Example: 192.168.10.10 -> 02:fe:c0:a8:0a:0a. Fixed MACs let dnsmasq hand
    every host the same reserved address every time (DHCP reservations, T04).
    """
    return "02:fe:" + ":".join(f"{b:02x}" for b in ipaddress.ip_address(ip).packed)


class Lab:
    """Everything about the lab, read from fortress.yaml.

    small=True keeps only the zones marked `small: true` and, in each zone, the first
    host plus any host with a role (e.g. the Barracks attacker), for 4 GB laptops.
    """

    def __init__(self, path=CONFIG_FILE, small=False):
        self.cfg = yaml.safe_load(Path(path).read_text())
        self.small = small
        self.domain = self.cfg["domain"]
        gw = self.cfg["gateways"]

        # --- Zones (the base VLANs) ---------------------------------------
        self.zones = {}
        for name, z in self.cfg["zones"].items():
            if small and not z.get("small"):
                continue
            net = ipaddress.ip_network(z["subnet"])
            hosts = dict(z.get("hosts") or {})
            if small:
                first = next(iter(hosts))
                hosts = {h: v for h, v in hosts.items() if h == first or v.get("role")}
            self.zones[name] = {
                **z,
                "name": name,
                "title": z["name"],
                "net": net,
                "prefixlen": net.prefixlen,
                "vip": str(net[gw["vip_host"]]),
                "r1_ip": str(net[gw["r1"]["host"]]),
                "r2_ip": str(net[gw["r2"]["host"]]),
                "hosts": hosts,
            }
        self.vlans = sorted(z["vlan"] for z in self.zones.values())
        self.vrrp = gw  # priorities and preempt, used by the VRRP template (T06)

        # --- Hosts (zone hosts, FOB hosts, internet hosts) -----------------
        # Each entry: name, zone, ip, prefixlen, mac, gateway, switch, vlan (None = untagged),
        # dhcp (True = gets its address from srv1 instead of a static one).
        self.hosts = {}
        for zname, z in self.zones.items():
            for hname, h in z["hosts"].items():
                self._add_host(hname, zname, h, z["net"], z["vip"], z["switch"], z["vlan"],
                               dhcp=z.get("dhcp_range") is not None)
        fob = self.cfg["fob"]
        fob_net = ipaddress.ip_network(fob["subnet"])
        for hname, h in fob["hosts"].items():
            self._add_host(hname, "fob", h, fob_net, fob["gateway"], fob["switch"], None, dhcp=False)
        inet = self.cfg["internet"]
        inet_net = ipaddress.ip_network(inet["subnet"])
        for hname, h in inet["hosts"].items():
            # Internet hosts hang straight off r4, no switch.
            self._add_host(hname, "internet", h, inet_net, inet["gateway"], None, None, dhcp=False)

        # --- Monitoring host in the VM's root namespace (D12) --------------
        mon = self.cfg["monitor"]
        mz = self.zones[mon["zone"]]
        self.monitor = {
            "name": mon["host"], "ip": mon["ip"], "prefixlen": mz["prefixlen"],
            "mac": mac_for(mon["ip"]), "gateway": mz["vip"],
            "switch": mz["switch"], "vlan": mz["vlan"],
        }

        # --- Routers and their point-to-point links ------------------------
        self.routers = self.cfg["routers"]
        self.links = []          # one entry per /30 link
        self.router_ifaces = {r: [] for r in self.routers}  # r -> [{name, ip, prefixlen, peer, cost}]
        for link in self.cfg["router_links"]:
            a, b = link["ends"]
            net = ipaddress.ip_network(link["subnet"])
            entry = {
                "a": a, "b": b, "net": net,
                "a_ip": str(net[1]), "b_ip": str(net[2]),
                # Interface names say where they lead: on r1, "r1-r3" is the link to r3.
                "a_if": f"{a}-{b}", "b_if": f"{b}-{a}",
                "cost": link["ospf_cost"], "purpose": link.get("purpose", ""),
            }
            self.links.append(entry)
            self.router_ifaces[a].append({"name": entry["a_if"], "ip": entry["a_ip"], "prefixlen": 30,
                                          "peer": b, "peer_ip": entry["b_ip"], "cost": entry["cost"]})
            self.router_ifaces[b].append({"name": entry["b_if"], "ip": entry["b_ip"], "prefixlen": 30,
                                          "peer": a, "peer_ip": entry["a_ip"], "cost": entry["cost"]})

        self.fob = {**fob, "net": fob_net, "prefixlen": fob_net.prefixlen}
        self.internet = {**inet, "net": inet_net, "prefixlen": inet_net.prefixlen}
        self.backup_static_routes = self.cfg.get("backup_static_routes") or {}
        self.vpn = self.cfg["vpn"]
        self.ids = self.cfg["ids"]

        # --- Switches -----------------------------------------------------
        sw = self.cfg["switches"]
        self.core_switches = sw["core"]
        # Only access switches that some kept zone uses (--small drops s3).
        used = {z["switch"] for z in self.zones.values()}
        self.access_switches = [s for s in sw["access"] if s in used]
        self.fob_switch = sw["fob"]

    def _add_host(self, name, zone, h, net, gateway, switch, vlan, dhcp):
        self.hosts[name] = {
            "name": name, "zone": zone, "ip": h["ip"], "prefixlen": net.prefixlen,
            "mac": mac_for(h["ip"]), "gateway": gateway, "switch": switch, "vlan": vlan,
            "dhcp": dhcp, "role": h.get("role", ""),
        }

    def gateway_routers(self):
        """The routers that act as zone gateways (r1 and r2)."""
        return [r for r, info in self.routers.items() if info["role"] == "gateway"]

    def router_with_role(self, role):
        return next(r for r, info in self.routers.items() if info["role"] == role)

    def zone_ip(self, router, zone):
        """A gateway router's own address in a zone (r1 -> .2, r2 -> .3)."""
        return self.zones[zone][f"{router}_ip"]

    def lab_subnets(self):
        """Every subnet inside the lab, e.g. for routes from the monitoring host."""
        nets = [z["net"] for z in self.zones.values()]
        nets += [self.fob["net"], self.internet["net"]] + [l["net"] for l in self.links]
        return [str(n) for n in nets]


if __name__ == "__main__":
    # Quick look at the derived values:  python3 topology/labconfig.py [--small]
    import sys
    lab = Lab(small="--small" in sys.argv)
    print(f"zones: {', '.join(lab.zones)}  (VLANs {lab.vlans})")
    print(f"access switches: {lab.access_switches}")
    for h in lab.hosts.values():
        how = "DHCP" if h["dhcp"] else "static"
        print(f"  {h['name']:7} {h['zone']:15} {h['ip']:15} {h['mac']}  {how}")
    for l in lab.links:
        print(f"  link {l['a_if']:6} {l['a_ip']:11} <-> {l['b_if']:6} {l['b_ip']:11} cost {l['cost']}")
