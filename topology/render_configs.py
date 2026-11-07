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


def render_all(lab, out=ETC):
    """Render every config. Returns the list of files written."""
    out = Path(out)
    return dnsmasq(lab, out)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default=str(ETC), help=f"output folder (default {ETC})")
    p.add_argument("--small", action="store_true")
    a = p.parse_args()
    for f in render_all(Lab(small=a.small), a.out):
        print(f"wrote {f}")
