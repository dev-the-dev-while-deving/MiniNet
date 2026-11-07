#!/usr/bin/env python3
"""vpn.py: load and inspect a strongSwan IPsec tunnel over its VICI control socket (T14).

strongSwan's normal tool for this is swanctl, but Ubuntu's AppArmor profile only lets swanctl
use the one default socket /run/charon.vici. Two VPN routers on one shared filesystem need two
sockets (/var/run/fortress/r1/charon.vici, .../r3/...), so we use strongSwan's own Python VICI
library instead, which speaks the same protocol (decisions D28). We don't loosen AppArmor.

Run inside the router's namespace with the fortress venv's Python (services.py does this):
    vpn.py load   <socket> <vpn.json>    load the pre-shared key and the connection
    vpn.py status <socket>               print one line per IKE SA and its tunnels
vpn.json is rendered by render_configs.py; it holds the same settings swanctl.conf would.
"""
import json
import socket
import sys

import vici


def session(path):
    s = socket.socket(socket.AF_UNIX)
    s.connect(path)
    return vici.Session(s)


def load(path, conf_file):
    conf = json.load(open(conf_file))
    s = session(path)
    s.load_shared(conf["secret"])          # the pre-shared key and the two IDs it belongs to
    s.load_conn(conf["connection"])        # start_action=start: the tunnel comes up by itself
    print("loaded", ", ".join(conf["connection"]))


def status(path):
    for sa in session(path).list_sas():
        for name, ike in sa.items():
            children = ", ".join(f"{c['name'].decode()} {c['state'].decode()}"
                                 for c in ike.get("child-sas", {}).values())
            print(f"{name}: {ike['state'].decode()} {ike['local-host'].decode()} <-> "
                  f"{ike['remote-host'].decode()}; tunnels: {children or 'none'}")


if __name__ == "__main__":
    cmd, path = sys.argv[1], sys.argv[2]
    load(path, sys.argv[3]) if cmd == "load" else status(path)
