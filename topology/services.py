"""services.py: starts and stops the lab's daemons inside the Mininet nodes.

Every daemon runs inside its node's network namespace (we run it through node.cmd(),
which executes in that node's shell), and gets its own PID file, logs and runtime files
under /var/run/fortress/<node>/ and /var/log/fortress/<node>/, because Mininet hosts
share one filesystem (decisions D12).

start() brings the services up in dependency order; stop() kills everything we started,
using the PID files, so nothing is left running after the lab stops.
"""
import os
import shutil
import signal
import time
from pathlib import Path

from mininet.log import info

import render_configs
from render_configs import ETC, LOG, RUN, ROOT

UDHCPC_SCRIPT = ROOT / "configs" / "udhcpc" / "fortress-udhcpc-script"


def node_dirs(node, owner=None):
    """Create /var/run/fortress/<node> and /var/log/fortress/<node>, optionally owned by a
    service user (a daemon that drops root must still be able to write there)."""
    for d in (RUN / node, LOG / node):
        d.mkdir(parents=True, exist_ok=True)
        if owner:
            shutil.chown(d, user=owner)


# --- Gateways ----------------------------------------------------------------------
def start_temporary_gateways(net, lab):
    """Until VRRP exists (T06), r1 holds every zone's gateway address (.1) as a plain
    extra address, so hosts and servers have a working default gateway."""
    r1 = lab.gateway_routers()[0]
    for z in lab.zones.values():
        net[r1].cmd(f"ip addr add {z['vip']}/{z['prefixlen']} dev {r1}-lan.{z['vlan']}")


# --- DHCP and DNS (T04) ------------------------------------------------------------
def start_dhcp_server(net, lab):
    """dnsmasq on srv1, with the config rendered from fortress.yaml."""
    srv = lab.host_with_role("dhcp-dns")["name"]
    node_dirs(srv, owner="dnsmasq")
    conf = ETC / srv / "dnsmasq.conf"
    out = net[srv].cmd(f"dnsmasq --conf-file={conf}")
    if out.strip():
        info(f"*** dnsmasq on {srv}: {out.strip()}\n")


def start_dhcp_relays(net, lab):
    """A DHCP relay on r1 and r2. Hosts broadcast DHCPDISCOVER in their own VLAN; the relay
    on that VLAN's sub-interface forwards it as unicast to srv1 in the Servers VLAN, adding
    its own address (giaddr) so srv1 knows which zone's scope to answer from."""
    srv = lab.host_with_role("dhcp-dns")
    upstream_vlan = lab.zones[srv["zone"]]["vlan"]
    client_vlans = [z["vlan"] for z in lab.zones.values() if z.get("dhcp_range")]
    for r in lab.gateway_routers():
        node_dirs(r)
        down = " ".join(f"-id {r}-lan.{v}" for v in client_vlans)
        net[r].cmd(f"dhcrelay -4 -q -pf {RUN / r / 'dhcrelay.pid'} -iu {r}-lan.{upstream_vlan} {down} {srv['ip']}")


def start_dhcp_clients(net, lab, timeout=30):
    """Run a DHCP client (BusyBox udhcpc, decisions D17) on every DHCP host, all at once,
    then wait until each has its address. Returns the hosts that got no lease in time."""
    hosts = [h for h in lab.hosts.values() if h["dhcp"]]
    for h in hosts:
        name, intf = h["name"], f"{h['name']}-eth0"
        node_dirs(name)
        # -f: stay in the foreground (we background it with &, so its PID is known right away);
        # -s: our hook script; -x hostname: tell the server who we are; -t/-T: retry every 2 s.
        # udhcpc keeps running to renew the lease, so it's stopped with the other daemons.
        pid = net[name].cmd(f"busybox udhcpc -f -i {intf} -s {UDHCPC_SCRIPT} -x hostname:{name} "
                            f"-t 10 -T 2 > {LOG / name / 'udhcpc.log'} 2>&1 & echo $!").strip()
        (RUN / name / "udhcpc.pid").write_text(pid.splitlines()[-1] + "\n")
    missing = {h["name"] for h in hosts}
    deadline = time.time() + timeout
    while missing and time.time() < deadline:
        for name in list(missing):
            if "inet " in net[name].cmd(f"ip -4 -o addr show dev {name}-eth0"):
                missing.discard(name)
        time.sleep(0.5)
    return sorted(missing)


# --- Routing (T05) -----------------------------------------------------------------
FRRINIT = "/usr/lib/frr/frrinit.sh"


def vtysh(net, router, command):
    """Run a vtysh command against one router's FRR instance (-N picks the instance)."""
    return net[router].cmd(f"vtysh -N {router} -c '{command}'")


def start_routing(net, lab):
    """FRR in every router namespace, plus the routers' iptables rules (NAT on r1/r2).
    `frrinit.sh start r1` starts FRR's watchdog and daemons with the r1 instance's config
    in /etc/frr/r1/; run through net['r1'].cmd() they live in r1's namespace."""
    for r in lab.routers:
        node_dirs(r, owner="frr")
        out = net[r].cmd(f"{FRRINIT} start {r}")
        if "fail" in out.lower():
            info(f"*** FRR on {r}: {out.strip()}\n")
    for r in lab.gateway_routers():
        net[r].cmd(f"iptables-restore < {ETC / r / 'iptables.rules'}")


def vtysh_json(net, router, command):
    """Run a vtysh "... json" command and parse the result ({} if FRR isn't answering).
    Any warning text vtysh prints before the JSON is skipped."""
    import json
    out = vtysh(net, router, command)
    try:
        return json.loads(out[out.index("{"):])
    except ValueError:
        return {}


def ospf_neighbors(net, router):
    """{neighbour router-id: state} from `show ip ospf neighbor json`, e.g. {"3.3.3.3": "Full/-"}."""
    data = vtysh_json(net, router, "show ip ospf neighbor json")
    return {rid: n[0].get("nbrState", "") for rid, n in data.get("neighbors", {}).items()}


def wait_for_ospf(net, lab, timeout=40):
    """Wait until every router is Full with every neighbour it has a link to.
    Returns the seconds it took (or the timeout)."""
    start = time.time()
    want = {r: len(ifaces) for r, ifaces in lab.router_ifaces.items()}
    while time.time() - start < timeout:
        if all(sum(s.startswith("Full") for s in ospf_neighbors(net, r).values()) == n
               for r, n in want.items()):
            return time.time() - start
        time.sleep(1)
    return timeout


# --- Start and stop ---------------------------------------------------------------
def start(net, lab):
    """Bring every service up, in order. Each step depends on the ones before it."""
    info("*** Rendering configs from fortress.yaml\n")
    for f in render_configs.render_all(lab):
        info(f"    {f}\n")
    info("*** Gateways: r1 holds the zone gateway addresses (until VRRP, T06)\n")
    start_temporary_gateways(net, lab)
    info("*** DHCP/DNS server (dnsmasq on srv1) and relays (r1, r2)\n")
    start_dhcp_server(net, lab)
    start_dhcp_relays(net, lab)
    info("*** DHCP clients\n")
    missing = start_dhcp_clients(net, lab)
    if missing:
        info(f"*** No DHCP lease for: {', '.join(missing)}\n")
    info("*** Routing: FRR (OSPF) on every router, NAT on r1/r2\n")
    start_routing(net, lab)
    info(f"*** OSPF converged in {wait_for_ospf(net, lab):.1f} s\n")


def stop(net=None, lab=None):
    """Stop every daemon we started, using the PID files under /var/run/fortress.
    Safe to call when nothing is running (stop_all.sh calls it after a crash)."""
    import subprocess
    # FRR instances: one watchfrr per router; frrinit.sh stops it and all its daemons.
    for watchfrr in sorted(Path("/var/run/frr").glob("*/watchfrr.pid")):
        subprocess.run([FRRINIT, "stop", watchfrr.parent.name], capture_output=True)
    for pidfile in sorted(RUN.glob("*/*.pid")):
        try:
            pid = int(pidfile.read_text().split()[0])
            os.kill(pid, signal.SIGTERM)
        except (ValueError, IndexError, ProcessLookupError, PermissionError):
            pass
        pidfile.unlink(missing_ok=True)
