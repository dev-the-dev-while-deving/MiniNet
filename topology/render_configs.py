#!/usr/bin/env python3
"""render_configs.py: fills in the config templates in configs/ from config/fortress.yaml.

This is the "infrastructure as code" part of Fortress-Net (decisions D15): one YAML file
plus one template per service produce every router and server config. Change the YAML,
re-render, and every config follows.

Output goes to /etc/fortress/<node>/ in the VM (never the shared folder, decisions D12).
FRR is the exception: its multi-instance tooling insists on /etc/frr/<router>/.

Look at what it generates without touching /etc:
    python3 topology/render_configs.py --out /tmp/rendered [--small]
"""
import argparse
import ipaddress
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from labconfig import ROOT, Lab

TEMPLATES = ROOT / "configs"
ETC = Path("/etc/fortress")
FRR_ETC = Path("/etc/frr")       # FRR's instance tooling needs /etc/frr/<router>/
VRRP_ADVERT_MS = 200             # VRRP advert interval; FRR's default is 1000 ms (slower failover)
RUN = Path("/var/run/fortress")
LOG = Path("/var/log/fortress")

# StrictUndefined: a typo in a template variable is an error, not a silently empty line.
env = Environment(loader=FileSystemLoader(str(TEMPLATES)), undefined=StrictUndefined,
                  trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def dnsmasq(lab, out):
    """dnsmasq.conf for the DHCP/DNS server (srv1)."""
    srv = lab.host_with_role("dhcp-dns")
    zones = []
    for z in lab.zones.values():
        if not z.get("dhcp_range"):
            continue
        start, end = z["dhcp_range"]
        zones.append({**z, "pool_start": str(z["net"][start]), "pool_end": str(z["net"][end]),
                      "netmask": str(z["net"].netmask)})
    dhcp_hosts = [h for h in lab.hosts.values() if h["dhcp"]]
    # DNS records for everything with a fixed address. Routers are named after their
    # Management-VLAN address, the one admins will SSH to (T15).
    records = [(h["name"], h["ip"]) for h in lab.hosts.values() if not h["dhcp"]]
    if "management" in lab.zones:
        records += [(r, lab.zone_ip(r, "management")) for r in lab.gateway_routers()]
    records += [(lab.router_with_role("fob"), lab.fob["gateway"]),
                (lab.router_with_role("isp"), lab.internet["gateway"]),
                (lab.monitor["name"], lab.monitor["ip"])]
    text = env.get_template("dnsmasq/dnsmasq.conf.j2").render(
        node=srv["name"], intf=f"{srv['name']}-eth0", domain=lab.domain,
        run_dir=RUN / srv["name"], log_dir=LOG / srv["name"],
        records=records, dhcp_zones=zones, dhcp_hosts=dhcp_hosts, dns_ip=srv["ip"])
    return [write(out / srv["name"] / "dnsmasq.conf", text)]


def frr(lab, frr_out):
    """daemons + frr.conf for every router (OSPF area 0, costs, floating static route)."""
    files = []
    for r, info in lab.routers.items():
        lans = []
        if info["role"] == "gateway":
            # VRID = the zone's VLAN number, so "VRRP 40" is always Barracks.
            lans = [{"name": f"{r}-lan.{z['vlan']}", "title": f"{z['title']} (VLAN {z['vlan']})",
                     "vrid": z["vlan"], "vip": z["vip"], "priority": lab.vrrp[r]["vrrp_priority"],
                     "preempt": lab.vrrp["preempt"], "adv_ms": VRRP_ADVERT_MS}
                    for z in lab.zones.values()]
        elif info["role"] == "fob":
            lans = [{"name": f"{r}-lan", "title": "FOB LAN"}]
        elif info["role"] == "isp":
            lans = [{"name": f"{r}-inet", "title": "Internet hosts"}]
        conf = env.get_template("frr/frr.conf.j2").render(
            router=r, role=info["role"], router_id=info["router_id"], log_dir=LOG / r,
            links=lab.router_ifaces[r], lans=lans,
            originate_default=info["role"] == "isp",
            static_routes=lab.backup_static_routes.get(r, []))
        daemons = env.get_template("frr/daemons.j2").render(router=r, vrrp=info["role"] == "gateway")
        files.append(write(frr_out / r / "frr.conf", conf))
        files.append(write(frr_out / r / "daemons", daemons))
        # vtysh refuses to apply frr.conf at boot ("processing failure") if this file is missing.
        files.append(write(frr_out / r / "vtysh.conf", "! vtysh.conf (intentionally empty; see frr.conf)\n"))
    return files


def iptables(lab, out):
    """NAT towards the ISP and the zone firewall (T13) for the gateway routers."""
    isp = lab.router_with_role("isp")
    dns = lab.host_with_role("dhcp-dns")["ip"]
    files = []
    for r in lab.gateway_routers():
        nat_out = [i["name"] for i in lab.router_ifaces[r] if i["peer"] == isp]
        allow = []
        sources = [(z["name"], z["title"], str(z["net"]), z.get("reach", [])) for z in lab.zones.values()]
        sources.append(("fob", "FOB", str(lab.fob["net"]), lab.fob.get("reach", [])))
        for name, title, src, reach in sources:
            for target in reach:
                if target == "internet":
                    allow += [{"comment": f"{title} may reach the internet", "src": src, "dst": f"-o {o}"} for o in nat_out]
                elif target == "fob":
                    allow.append({"comment": f"{title} may reach the FOB", "src": src, "dst": f"-d {lab.fob['net']}"})
                elif target in lab.zones:
                    allow.append({"comment": f"{title} may reach {lab.zones[target]['title']}",
                                  "src": src, "dst": f"-d {lab.zones[target]['net']}"})
        text = env.get_template("iptables/rules.j2").render(router=r, nat_out=nat_out, dns=dns,
                                                            monitor=lab.monitor["ip"], allow=allow)
        files.append(write(out / r / "iptables.rules", text))
    return files


def render_all(lab, out=ETC, frr_out=FRR_ETC):
    """Render every config. Returns the list of files written."""
    out, frr_out = Path(out), Path(frr_out)
    return dnsmasq(lab, out) + frr(lab, frr_out) + iptables(lab, out)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default=str(ETC), help=f"output folder (default {ETC})")
    p.add_argument("--small", action="store_true")
    a = p.parse_args()
    out = Path(a.out)
    # With --out, FRR files go to <out>/frr/<router>/ too, so nothing touches /etc.
    frr_out = FRR_ETC if out == ETC else out / "frr"
    for f in render_all(Lab(small=a.small), out, frr_out):
        print(f"wrote {f}")
