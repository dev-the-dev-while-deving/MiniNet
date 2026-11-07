#!/usr/bin/env python3
"""check_config.py: sanity-check config/fortress.yaml before anything uses it.

Catches the typos that would otherwise show up as confusing network bugs later:
a host outside its subnet, two hosts with the same IP, a VLAN used twice,
overlapping subnets, a router link that isn't a /30, or a name that doesn't exist.

Usage:  python3 scripts/check_config.py [path/to/fortress.yaml]
Exit code 0 = all good, 1 = problems found (they are printed).
"""
import ipaddress
import sys
from pathlib import Path

import yaml

DEFAULT = Path(__file__).resolve().parent.parent / "config" / "fortress.yaml"


def check(cfg):
    """Return a list of problems found in the loaded config (empty list = OK)."""
    errors = []
    seen_ips = {}  # ip -> who uses it, to catch duplicates

    def claim_ip(ip, owner):
        if ip in seen_ips:
            errors.append(f"{ip} is used by both {seen_ips[ip]} and {owner}")
        seen_ips[ip] = owner

    switches = set(cfg["switches"]["core"]) | set(cfg["switches"]["access"]) | {cfg["switches"]["fob"]}
    routers = set(cfg["routers"])
    zones = cfg["zones"]
    reach_targets = set(zones) | {"fob", "internet"}
    gw = cfg["gateways"]

    # Zones, plus the FOB and internet LANs, which have the same shape.
    lans = dict(zones)
    lans["fob"] = cfg["fob"]
    lans["internet"] = cfg["internet"]

    vlans = {}
    nets = {}
    for zname, z in lans.items():
        net = ipaddress.ip_network(z["subnet"])  # raises if the subnet itself is malformed
        nets[zname] = net

        if "vlan" in z:
            if z["vlan"] in vlans:
                errors.append(f"VLAN {z['vlan']} is used by both {vlans[z['vlan']]} and {zname}")
            vlans[z["vlan"]] = zname

        if "switch" in z and z["switch"] not in switches:
            errors.append(f"zone {zname}: unknown switch {z['switch']}")

        for target in z.get("reach", []):
            if target not in reach_targets:
                errors.append(f"zone {zname}: reach lists unknown zone '{target}'")

        # Gateway addresses: VIP/r1/r2 inside base zones, a single gateway for FOB/internet.
        if zname in zones:
            reserved = {net[gw["vip_host"]]: "VIP", net[gw["r1"]["host"]]: "r1", net[gw["r2"]["host"]]: "r2"}
            for ip, who in reserved.items():
                claim_ip(ip, f"{zname} {who}")
        else:
            gateway = ipaddress.ip_address(z["gateway"])
            if gateway not in net:
                errors.append(f"{zname}: gateway {gateway} is not in {net}")
            claim_ip(gateway, f"{zname} gateway")

        pool = z.get("dhcp_range")
        for hname, h in (z.get("hosts") or {}).items():
            ip = ipaddress.ip_address(h["ip"])
            if ip not in net:
                errors.append(f"host {hname}: {ip} is not in {zname}'s subnet {net}")
            if pool and pool[0] <= int(ip) - int(net.network_address) <= pool[1]:
                errors.append(f"host {hname}: {ip} is inside {zname}'s DHCP pool {pool}, so it could be handed out twice")
            claim_ip(ip, f"host {hname}")

    # No two LANs may overlap (the VPN selector 192.168.0.0/16 is fine: it's not a LAN).
    names = list(nets)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if nets[a].overlaps(nets[b]):
                errors.append(f"subnets of {a} ({nets[a]}) and {b} ({nets[b]}) overlap")

    # Router links: /30s, known routers, no overlap with each other or the LANs.
    link_nets = []
    for link in cfg["router_links"]:
        net = ipaddress.ip_network(link["subnet"])
        if net.prefixlen != 30:
            errors.append(f"router link {link['ends']}: {net} is not a /30")
        for r in link["ends"]:
            if r not in routers:
                errors.append(f"router link {link['ends']}: unknown router {r}")
        for other in link_nets + list(nets.values()):
            if net.overlaps(other):
                errors.append(f"router link {net} overlaps {other}")
        link_nets.append(net)
        for idx, r in enumerate(link["ends"]):
            claim_ip(net[idx + 1], f"{r} on link {link['ends'][0]}-{link['ends'][1]}")

    # Backup static routes must point at an address on one of the router links.
    for r, routes in (cfg.get("backup_static_routes") or {}).items():
        for route in routes:
            via = ipaddress.ip_address(route["via"])
            if not any(via in n for n in link_nets):
                errors.append(f"{r}: static route via {via} is not on any router link")

    for r in cfg["vpn"]["ends"]:
        if r not in routers:
            errors.append(f"vpn: unknown router {r}")
    mon = cfg["monitor"]
    if mon["zone"] not in zones:
        errors.append(f"monitor: unknown zone {mon['zone']}")
    else:
        mz = zones[mon["zone"]]
        mip = ipaddress.ip_address(mon["ip"])
        if mip not in nets[mon["zone"]]:
            errors.append(f"monitor: {mip} is not in {mon['zone']}'s subnet")
        pool = mz.get("dhcp_range")
        if pool and pool[0] <= int(mip) - int(nets[mon["zone"]].network_address) <= pool[1]:
            errors.append(f"monitor: {mip} is inside {mon['zone']}'s DHCP pool")
        claim_ip(mip, f"monitor {mon['host']}")

    for sw in cfg["ids"]["attach_to"]:
        if sw not in cfg["switches"]["core"]:
            errors.append(f"ids: attach_to {sw} is not a core switch")
    for r, info in cfg["routers"].items():
        if "trunk_to" in info and info["trunk_to"] not in cfg["switches"]["core"]:
            errors.append(f"router {r}: trunk_to {info['trunk_to']} is not a core switch")

    return errors


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    cfg = yaml.safe_load(path.read_text())
    errors = check(cfg)
    for e in errors:
        print(f"❌ {e}")
    if errors:
        sys.exit(1)
    n_hosts = sum(len(z.get("hosts") or {}) for z in cfg["zones"].values())
    print(f"✅ {path.name} OK: {len(cfg['zones'])} zones, {n_hosts} zone hosts, "
          f"{len(cfg['routers'])} routers, {len(cfg['router_links'])} router links")


if __name__ == "__main__":
    main()
