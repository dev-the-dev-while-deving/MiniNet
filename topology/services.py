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
import sys
import time
from pathlib import Path

from mininet.log import info

import render_configs
from render_configs import ETC, LOG, RUN, ROOT

sys.path.insert(0, str(ROOT / "detection"))
import sources   # noqa: E402  (needs ROOT above)

UDHCPC_SCRIPT = ROOT / "configs" / "udhcpc" / "fortress-udhcpc-script"


def node_dirs(node, owner=None):
    """Create /var/run/fortress/<node> and /var/log/fortress/<node>, optionally owned by a
    service user (a daemon that drops root must still be able to write there)."""
    for d in (RUN / node, LOG / node):
        d.mkdir(parents=True, exist_ok=True)
        if owner:
            shutil.chown(d, user=owner)


# --- SDN controller (T08) ------------------------------------------------------------
CTRL_APP = ROOT / "controller" / "fortress_ctrl.py"
REST = "http://127.0.0.1:8080/fortress"


def ryu_manager():
    """~/venvs/ryu/bin/ryu-manager of the normal lab user (we run as root under sudo)."""
    import pwd
    home = pwd.getpwnam(os.environ.get("SUDO_USER") or "ubuntu").pw_dir
    return f"{home}/venvs/ryu/bin/ryu-manager"


def controller_status():
    """The controller's /fortress/status as a dict, or None if it isn't answering."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"{REST}/status", timeout=2) as r:
            return json.loads(r.read())
    except OSError:
        return None


def start_controller(mode="reactive", timeout=30):
    """Start Ryu with our app in the VM's root namespace (it talks to OVS over 127.0.0.1:6653
    and serves its REST API on :8080), then wait until the REST API answers."""
    import subprocess
    node_dirs("ryu")
    log = open(LOG / "ryu" / "ryu.log", "w")
    env = {**os.environ, "FORTRESS_MODE": mode, "OPENSSL_armcap": "0"}
    proc = subprocess.Popen([ryu_manager(), "--observe-links", "--ofp-tcp-listen-port", "6653",
                             "--wsapi-port", "8080", str(CTRL_APP)],
                            stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    (RUN / "ryu" / "ryu.pid").write_text(f"{proc.pid}\n")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"the controller exited; see {LOG / 'ryu' / 'ryu.log'}")
        if controller_status() is not None:
            return
        time.sleep(0.5)
    raise RuntimeError(f"the controller did not answer within {timeout} s")


def wait_for_controller(lab, timeout=40):
    """Wait until every base switch is connected and LLDP has found every switch link
    (one core-to-core link plus two uplinks per access switch). Returns the seconds taken."""
    want_switches = set(lab.core_switches + lab.access_switches)
    want_links = 1 + 2 * len(lab.access_switches)
    start = time.time()
    while time.time() - start < timeout:
        s = controller_status() or {}
        if set(s.get("switches", [])) == want_switches and len(s.get("links", [])) >= want_links:
            return time.time() - start
        time.sleep(0.5)
    return timeout


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


def vrrp_status(net, router):
    """{VRID: "Master"/"Backup"/"Initialize"} for one router, from `show vrrp json`."""
    data = net[router].cmd(f"vtysh -N {router} -c 'show vrrp json'")
    import json
    try:
        routers = json.loads(data[data.index("["):])
    except ValueError:
        return {}
    return {vr["vrid"]: vr.get("v4", {}).get("status", "") for vr in routers}


def wait_for_vrrp(net, lab, timeout=20):
    """Wait until every zone's virtual router has exactly one Master. Returns the seconds taken."""
    start = time.time()
    vrids = [z["vlan"] for z in lab.zones.values()]
    while time.time() - start < timeout:
        status = [vrrp_status(net, r) for r in lab.gateway_routers()]
        if all(sum(s.get(v) == "Master" for s in status) == 1 for v in vrids):
            return time.time() - start
        time.sleep(0.5)
    return timeout


# --- Simulated failures (demo scenario 2, CLI commands `fail` / `recover`) -----------
def fail_node(net, name):
    """Simulate a power loss: take every interface of a node down. Its processes keep
    running, but it can no longer send or receive anything, exactly like a dead box."""
    node = net[name]
    if not node.inNamespace:
        raise ValueError(f"{name} is in the VM's root namespace; failing it would cut the VM off")
    names = [l.split(": ")[1].split("@")[0] for l in node.cmd("ip -o link show").splitlines() if ": " in l]
    names = [n for n in names if n != "lo"]
    for n in names:
        node.cmd(f"ip link set {n} down")
    node.failed_intfs = names
    return names


def recover_node(net, name):
    """Undo fail_node(): bring the interfaces back up. Two passes, because a VLAN or macvlan
    interface can only come up once the interface it sits on is up."""
    node = net[name]
    names = getattr(node, "failed_intfs", [])
    for _ in range(2):
        for n in names:
            node.cmd(f"ip link set {n} up")
    node.failed_intfs = []
    return names


# --- IPsec VPN to the FOB (T14) -------------------------------------------------------
def lab_user():
    """The normal (non-root) user that owns the venvs and the admin SSH key."""
    return os.environ.get("SUDO_USER") or "ubuntu"


def fortress_python():
    """~/venvs/fortress/bin/python of the normal lab user (it has the vici library)."""
    return ryu_manager().replace("venvs/ryu/bin/ryu-manager", "venvs/fortress/bin/python")


def vpn_cmd(net, router, args):
    """Run topology/vpn.py inside a router's namespace against that router's VICI socket."""
    return net[router].cmd(f"{fortress_python()} {ROOT / 'topology' / 'vpn.py'} {args.split()[0]} "
                           f"{RUN / router / 'charon.vici'} {' '.join(args.split()[1:])}")


def start_vpn(net, lab, timeout=20):
    """strongSwan on both VPN routers: one charon-systemd daemon per router namespace, with
    its own config (STRONGSWAN_CONF) and control socket, then load the tunnel over VICI\n    (topology/vpn.py, decisions D28) and wait until
    the IPsec SA is up. Returns the seconds it took (or the timeout)."""
    for r in lab.vpn["ends"]:
        node_dirs(r)
        pid = net[r].cmd(f"STRONGSWAN_CONF={ETC / r / 'strongswan.conf'} /usr/sbin/charon-systemd "
                         f"> {LOG / r / 'charon-stdout.txt'} 2>&1 & echo $!").strip().splitlines()[-1]
        (RUN / r / "charon.pid").write_text(pid + "\n")
    start = time.time()
    for r in lab.vpn["ends"]:
        while not (RUN / r / "charon.vici").exists() and time.time() - start < timeout:
            time.sleep(0.2)
        out = vpn_cmd(net, r, f"load {ETC / r / 'vpn.json'}")
        if "loaded" not in out:
            info(f"*** loading the VPN on {r} failed: {out.strip()[-300:]}\n")
    while time.time() - start < timeout:
        if vpn_up(net, lab):
            return time.time() - start
        time.sleep(0.5)
    return timeout


def vpn_up(net, lab):
    """True if the first VPN router has an ESTABLISHED IKE SA with an INSTALLED tunnel."""
    out = vpn_cmd(net, lab.vpn["ends"][0], "status")
    return "ESTABLISHED" in out and "INSTALLED" in out


# --- Web/file server on srv3 (T18: something normal to talk to) -----------------------
WWW = Path("/var/lib/fortress/www")


def start_web(net, lab):
    """A plain HTTP server on srv3: the destination for the baseline's web requests and file
    downloads, and the target in the demo. Python's own http.server is enough and needs no
    config; file.bin gives the baseline a transfer big enough to show up in bytes/s."""
    srv = next((h for h in lab.hosts.values() if h["role"] == "web-file"), None)
    if srv is None:
        return
    WWW.mkdir(parents=True, exist_ok=True)
    (WWW / "index.html").write_text("<h1>Fortress-Net file server</h1>\n")
    if not (WWW / "file.bin").exists():
        (WWW / "file.bin").write_bytes(b"fortress" * 131072)      # 1 MiB
    node_dirs(srv["name"])
    pid = net[srv["name"]].cmd(f"python3 -m http.server 80 --bind {srv['ip']} --directory {WWW} "
                               f"> {LOG / srv['name'] / 'http.log'} 2>&1 & echo $!").strip().splitlines()[-1]
    (RUN / srv["name"] / "http.pid").write_text(pid + "\n")


# --- Intrusion detection (T17) --------------------------------------------------------
def start_ids(net, lab, timeout=60):
    """Suricata on ids1, listening on both mirror ports (decisions D13, D30).

    It runs inside ids1's namespace, so it only ever sees the mirrored copies, and writes
    alerts to /var/log/fortress/ids1/eve.json, which the threat engine reads (T20).
    Returns the seconds it took to report "engine started" (or the timeout).
    """
    ids = lab.ids["host"]
    node_dirs(ids)
    log = LOG / ids / "suricata.log"
    log.unlink(missing_ok=True)          # so "engine started" below is from this run
    net[ids].cmd(f"suricata -c {ETC / ids / 'suricata.yaml'} --af-packet -D "
                 f"--pidfile {RUN / ids / 'suricata.pid'} > {LOG / ids / 'suricata-stdout.txt'} 2>&1")
    start = time.time()
    while time.time() - start < timeout:
        if log.exists() and "engine started" in log.read_text():
            return time.time() - start
        time.sleep(0.5)
    info(f"*** Suricata did not start within {timeout} s; see {log}\n")
    return timeout


def ids_alerts(lab, since=0.0):
    """Suricata alerts from eve.json (detection/sources.py does the reading, T20)."""
    return sources.ids_alerts(lab.ids["host"], since)


# --- Feature collector (T18) ----------------------------------------------------------
COLLECTOR_CSV = LOG / "collector" / "live.csv"
COLLECTOR_LATEST = LOG / "collector" / "latest.json"


def start_collector(net, lab, out=COLLECTOR_CSV, label="live", latest=None, seconds=0):
    """detection/flow_collector.py inside ids1, listening on the same mirror ports as Suricata.

    The always-on instance (label "live") keeps latest.json up to date for the threat engine
    (T20); baseline_traffic.py starts a second one that writes the training CSV. Two sniffers
    on one interface each get their own copy of every packet, so they don't disturb each other.
    """
    ids = lab.ids["host"]
    node_dirs(ids)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    intfs = " ".join(f"{ids}-{c}" for c in lab.ids["attach_to"])
    name = Path(out).stem
    pid = net[ids].cmd(f"{fortress_python()} {ROOT / 'detection' / 'flow_collector.py'} "
                       f"--interfaces {intfs} --out {out} --latest {latest or COLLECTOR_LATEST} "
                       f"--label {label} --seconds {seconds} "
                       f"> {LOG / ids / f'collector-{name}.log'} 2>&1 & echo $!").strip().splitlines()[-1]
    (RUN / ids / f"collector-{name}.pid").write_text(pid + "\n")
    return pid


def latest_features(path=COLLECTOR_LATEST):
    """The newest window the collector wrote (detection/sources.py does the reading)."""
    return sources.latest_features(path)


# --- SSH and central logging (T15) ----------------------------------------------------
SSH_DIR = ETC / "ssh"


def ensure_key(path):
    """Make an ed25519 key pair if it doesn't exist yet. Keys live on the VM's disk only."""
    import subprocess
    if not Path(path).exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", Path(path).name, "-f", str(path)],
                       check=True)


def start_ssh(net, lab):
    """One sshd per SSH node, inside its namespace, with its own host key and log file, plus
    the servers' SSH input rules. Admins log in with the lab's admin key (made once per VM,
    never committed): /etc/fortress/ssh/admin_key."""
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    ensure_key(SSH_DIR / "admin_key")
    (SSH_DIR / "authorized_keys").write_text((SSH_DIR / "admin_key.pub").read_text())
    (SSH_DIR / "authorized_keys").chmod(0o644)
    for node, _ in lab.ssh_nodes():
        node_dirs(node)
        ensure_key(ETC / node / "ssh_host_ed25519_key")
        out = net[node].cmd(f"/usr/sbin/sshd -f {ETC / node / 'sshd_config'} -E {LOG / node / 'sshd.log'}")
        if out.strip():
            info(f"*** sshd on {node}: {out.strip()}\n")
        if (ETC / node / "input.rules").exists():
            net[node].cmd(f"iptables-restore < {ETC / node / 'input.rules'}")


def start_logging(net, lab):
    """The rsyslog server on srv2, then a forwarder in every sending node (the controller's
    runs in the VM's root namespace and reaches srv2 through mon0)."""
    import subprocess
    srv = lab.host_with_role("syslog")["name"]
    for node in [srv] + lab.log_senders():
        node_dirs(node)
        (RUN / node / "rsyslog").mkdir(exist_ok=True)
        cmd = f"rsyslogd -f {ETC / node / 'rsyslog.conf'} -i {RUN / node / 'rsyslogd.pid'}"
        if node == "controller":
            subprocess.run(cmd.split(), capture_output=True)
        else:
            net[node].cmd(cmd)


# --- Start and stop ---------------------------------------------------------------
def start(net, lab):
    """Bring every service up, in order. Each step depends on the ones before it."""
    info("*** Rendering configs from fortress.yaml\n")
    for f in render_configs.render_all(lab):
        info(f"    {f}\n")
    # Routing first: FRR also runs VRRP, which provides every zone's gateway (.1). DHCP needs
    # it, because srv1's replies to the relays go out through the Servers zone's gateway.
    info("*** Routing: FRR (OSPF + VRRP) on every router, NAT on r1/r2\n")
    start_routing(net, lab)
    info(f"*** VRRP: every gateway has a Master after {wait_for_vrrp(net, lab):.1f} s\n")
    info("*** DHCP/DNS server (dnsmasq on srv1) and relays (r1, r2)\n")
    start_dhcp_server(net, lab)
    start_dhcp_relays(net, lab)
    info("*** DHCP clients\n")
    missing = start_dhcp_clients(net, lab)
    if missing:
        info(f"*** No DHCP lease for: {', '.join(missing)}\n")
    info(f"*** OSPF converged in {wait_for_ospf(net, lab):.1f} s\n")
    info("*** IPsec VPN r1 <-> r3 (strongSwan)\n")
    info(f"*** VPN up after {start_vpn(net, lab):.1f} s\n")
    info("*** SSH on routers and servers, central logging to srv2\n")
    start_ssh(net, lab)
    start_logging(net, lab)
    info("*** Web/file server on srv3\n")
    start_web(net, lab)
    info("*** Suricata on ids1 (both core mirrors)\n")
    info(f"*** IDS engine started after {start_ids(net, lab):.1f} s\n")
    info("*** Traffic feature collector on ids1\n")
    start_collector(net, lab)


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
