# Design decisions

Why each tool and setup choice was made, and any fallback taken. Newest decisions at the bottom of each section. Students: be ready to explain each of these in the viva.

## T-1: Lab VM

**D1. Multipass instead of VirtualBox.** The main laptop is an Apple Silicon Mac, where VirtualBox works poorly. Multipass runs Ubuntu VMs on macOS (Apple Silicon and Intel), Windows and Linux with the same commands, so every teammate gets the same setup.

**D2. Native (9p) mount for the project folder.** It's faster than Multipass's default sshfs mount, but it can only be added while the VM is stopped, so `create_vm.sh` stops the VM, mounts, and starts it again. If native mounts aren't supported, it falls back to sshfs.

## T02: Software setup

**D3. SDN controller: Ryu 4.34 on Python 3.9 (the task's fallback), not os-ken.** Tested on 2026-11-07 in the arm64 VM:

| Tried | Result |
|-------|--------|
| os-ken 4.2.2 (latest) on 3.10 | Installs, but it's now a library only: no `osken-manager` launcher and no sample apps (`simple_switch_13`, `ofctl_rest`, `rest_topology`) |
| os-ken 3.1.1 on 3.10 | Has the launcher, still no sample apps |
| os-ken 2.11.2 on 3.10 | Has the launcher, no sample apps; crashed with "Illegal instruction" (see D8) |
| Ryu 4.34 + eventlet 0.30.2 on 3.10 | eventlet 0.30.2 doesn't run on Python 3.10 (`TypeError: cannot set 'is_timeout'`) |
| Ryu 4.34 + eventlet 0.33.3 on 3.10 | Newer eventlet removed `ALREADY_HANDLED`, which Ryu imports |
| **Ryu 4.34 + eventlet 0.30.2 on 3.9** | **Works:** `simple_switch_13` gives pingall 0% dropped on `mn --topo tree,depth=2`, and the `ofctl_rest` REST API answers |

Ryu also matches the roadmap, which names `ryu.app.*` apps and `--observe-links`, and most tutorials students will read. Python 3.9 comes from the deadsnakes PPA. Ryu's `setup.py` fails with setuptools 68+, so `setup_vm.sh` pins `setuptools==67.6.1` and installs Ryu with `--no-build-isolation`. Limitation: Ryu is no longer maintained, so we pin every version and never upgrade them.

**D4. FRRouting from the official FRR apt repo (frr-stable), not Ubuntu's package.** Ubuntu 22.04 ships FRR 8.1; the FRR repo has current releases (10.x) with arm64 builds and fixes to vrrpd, which we need for VRRP in T06. If the repo is ever unavailable, Ubuntu's `frr` package also has zebra, ospfd, vrrpd and staticd.

**D5. Only Open vSwitch and chrony run as VM-wide services.** FRR, Suricata and strongSwan get one copy per router or server, inside that Mininet host's network namespace, started by our scripts (T02b decides the details). Their packaged systemd services are disabled so they don't also run on the VM. chrony stays on because installing it replaces Ubuntu's timesyncd, so it is now the VM's own clock (D9); the optional lab NTP server on srv2 (T15) will be a separate chronyd with its own config. Prometheus and Grafana run on the VM but stay off until `start_all.sh` (T07) starts them, to save RAM.

**D6. `dnsmasq-base` instead of `dnsmasq`.** The full package starts a DNS service on the VM that fights systemd-resolved for port 53. We only want the binary, run inside srv1's namespace (T04).

**D7. Three Python environments.**
- System `python3` (3.10) runs the Mininet topology as root, because Ubuntu's `mininet` package installs its Python module there.
- `~/venvs/ryu` (3.9) runs the controller (D3).
- `~/venvs/fortress` (3.10) holds detection, ML and monitoring (`requirements.txt`).

Both venvs live in the VM's home folder, not the shared folder, because shared mounts are slow and break venvs. They are deliberately **not** on PATH: an early version put a venv first on PATH, so `python3` silently meant the venv and `import mininet` broke. Scripts call `~/venvs/<name>/bin/<tool>` by full path.

**D8. `OPENSSL_armcap=0` on Apple Silicon.** The VM's CPU reports vector extensions (SVE2 and others) that Apple chips don't really provide. The OpenSSL bundled in pip's `cryptography` wheel picks a fast path from those flags and crashes Python with "Illegal instruction" on import. eventlet imports it through dnspython, so this took down the os-ken controller. Setting `OPENSSL_armcap=0` makes OpenSSL ignore the CPU flags, which fixed it. `setup_vm.sh` sets it in `/etc/environment` only when `lscpu` reports an Apple CPU. Cost: OpenSSL uses slower generic code, which doesn't matter in the lab. Ubuntu's own OpenSSL 3.0.2 (used by strongSwan and Suricata) wasn't affected.

**D9. VM clock re-sync.** After a VM stop/start (every snapshot) the VM clock came back about 6 hours behind while `timedatectl` still said "synchronized: yes", and apt refused the package index ("not valid yet"). `snapshot.sh` and `setup_vm.sh` force a fresh sync: `chronyc makestep` once chrony is installed (it masks timesyncd), otherwise a timesyncd restart. `check_env.sh` checks that the clock is synced. Correct clocks also matter later for log timestamps and incident timelines.

## T02b: Architecture decisions

These six answers shape every network task from T03 on. Each one gives the decision, then why.

**D10. VLANs under the SDN controller: the controller is VLAN-aware.**
- OVS port tags (`ovs-vsctl set port s4-eth3 tag=40`) only work when a switch uses its built-in `NORMAL` forwarding. Once our controller installs its own OpenFlow rules, the tags are ignored and VLANs are the controller's job.
- So the controller (`controller/vlan.py`) reads the access-port → VLAN map from `fortress.yaml`. It **pushes** the 802.1Q tag when a frame enters an access port, the tag stays on across core and uplink links, and it **pops** the tag at the egress access port.
- Router ports are trunks: r1 and r2 plug into c1 and c2 and receive tagged frames on VLAN sub-interfaces (`r1-eth0.10`, `r1-eth0.20`, ...). Inter-VLAN traffic goes access → core → router trunk → back, as on a real router-on-a-stick.
- Standalone mode (D11) keeps the OVS port tags, so the same topology works before and after the controller arrives.

**D11. Forwarding before the controller exists (T03–T07): standalone switches with RSTP.**
- Until T08, every OVS bridge runs `fail_mode=standalone` (it behaves like a normal learning switch) with OVS port tags for VLANs.
- **RSTP is required**: each access switch has uplinks to both cores, which makes loops, and a loop without spanning tree is a broadcast storm. `topology/fortress_topo.py --standalone` sets `rstp_enable=true` on every bridge. (Verified on this OVS 2.17: `ovs-appctl rstp/show` works.)
- From T08 the bridges switch to `fail_mode=secure` with OpenFlow 1.3 under Ryu, and RSTP is turned off because the controller floods only along its own spanning tree.
- Trade-off to be ready for in the viva: in secure mode, if the controller dies, switches keep their installed rules but learn nothing new. That's normal SDN behaviour and is why the controller has to be reliable.

**D12. What runs where.** Mininet hosts are ordinary Linux processes: each one gets its **own network namespace** but **shares** the VM's filesystem, process list and `/dev/log`. So every per-node daemon needs its own config, PID file, socket and log path, or two copies will overwrite each other.

| Runs in | What | How it's kept separate |
|---------|------|------------------------|
| Router namespaces (r1–r4) | FRR: zebra, ospfd, staticd, vrrpd (r1/r2) | FRR's `-N <router>` path option: config in `/etc/frr/<router>/`, sockets in `/var/run/frr/<router>/`; `vtysh -N r1` talks to r1 only |
| Router namespaces | iptables NAT and zone rules (r1/r2) | Run with Mininet's `r1.cmd(...)` / `ip netns exec`, never on the VM itself |
| r1 and r3 | strongSwan (charon + swanctl) | Separate `strongswan.conf` per router (`STRONGSWAN_CONF`) with its own PID file and VICI socket path; checked in T14 |
| srv1 | dnsmasq (DHCP + DNS) | `--conf-file`, `--pid-file`, `--dhcp-leasefile` per instance |
| srv2 | rsyslog **server** (UDP/TCP 514), optional chronyd | Own config and PID file; no `imuxsock`, so it doesn't fight the VM's rsyslog over `/dev/log` |
| Every router and server | sshd (T15) | Own `sshd_config`, host keys, PID file and log file (`sshd -E`) per node |
| ids1 | Suricata on the mirror ports | `-l /var/log/fortress/ids1 --pidfile ...` |
| VM root namespace | OVS, Ryu controller, threat engine, Prometheus, Grafana, chrony (VM clock) | Ordinary processes; the controller listens on 127.0.0.1:6653 and its REST API on :8080 |

Supporting rules:
- **Runtime files live on the VM's own disk**, never in the shared folder: rendered configs in `/etc/fortress/<node>/`, logs in `/var/log/fortress/<node>/`, PIDs and sockets in `/var/run/fortress/<node>/`. Suricata's `eve.json` is `/var/log/fortress/ids1/eve.json`, which the threat engine reads directly.
- **Central logging must cross the network.** Unix sockets aren't namespaced, so `logger` or `syslog()` inside r1 would land in the *VM's* rsyslog, not srv2. Lab daemons therefore log to files in `/var/log/fortress/<node>/`, and a small rsyslog **forwarder** in each sending node's namespace (`imfile` → UDP/TCP to srv2:514) ships them. Logs then really travel over the lab network and show up in tcpdump. Details are in T15.
- **Monitoring link into the lab.** Prometheus in the root namespace can't reach anything inside the lab by itself. T03 adds one host `mon0` with `inNamespace=False` (so its interface lands in the VM's root namespace), plugged into s1 on the Management VLAN 99 at 192.168.99.250, with routes to the lab subnets via the VIP 192.168.99.1. T03 adds it to `fortress.yaml`. T13's isolation rules must allow monitoring traffic from it.

**D13. ML features come from packets on the ids1 mirror, not from Ryu flow stats.**
- Ryu flow stats only count per installed rule. Features like "distinct destination ports per source" would need one rule per 5-tuple, which blows up the flow tables, and the stats would lag behind the attack.
- So `detection/flow_collector.py` reads the packets ids1 receives (scapy, or tshark for speed) in **5-second windows** and computes per source host: packets/s, bytes/s, new connections/s (TCP SYNs), distinct destination ports, distinct destination hosts and failed handshakes (SYN with no SYN-ACK, or a RST). Suricata alerts from `eve.json` are a second signal. Ryu stats only feed the dashboard.
- **Mirror both cores.** An OVS mirror can only output to a port on the same bridge, and traffic may cross either core. So ids1 gets two interfaces, `ids1-eth0` on c1 and `ids1-eth1` on c2, each receiving its core's mirror, and Suricata and the collector listen on both. (`fortress.yaml` changes `ids.attach_to` to `[c1, c2]`.)
- **Count each packet once.** Routed traffic crosses the cores twice: once on the source VLAN towards r1, once on the destination VLAN back. The mirror keeps the 802.1Q tags, so the collector only counts a packet when its VLAN tag is the **source** host's zone VLAN.
- **Known gap:** traffic between two hosts on the same access switch and VLAN never reaches a core, so ids1 can't see it. Scenario 3 (Barracks → Intelligence) always crosses the cores, so this is acceptable; it goes in the report's limitations.

**D14. Controller structure: one Ryu app, plain modules behind it.** `controller/fortress_ctrl.py` is the only Ryu app (`ryu-manager --observe-links controller/fortress_ctrl.py`). It handles the OpenFlow events and calls ordinary Python modules, each of which can be explained (and tested) on its own:

| Module | Job | Task |
|--------|-----|------|
| `topology.py` | LLDP-discovered switches and links as a networkx graph; host locations (MAC → switch, port) | T08 |
| `paths.py` | Shortest primary path, plus a link-disjoint backup path for fast-failover | T08, T11 |
| `vlan.py` | Access-port → VLAN map from `fortress.yaml`; push/pop actions | T08 |
| `policy.py` | Zone reach rules, MAC allowlist, DHCP only from srv1's port | T13 |
| `qos.py` | Queue per traffic class (Command/Communications high, Barracks low) | T12 |
| `response.py` | Block, unblock and quarantine actions | T21, T22 |
| `rest_api.py` | `POST /block`, `POST /unblock` and status, using Ryu's built-in WSGI | T21 |

This replaces the roadmap's `controller/block_api.py`: the block API is `rest_api.py`, so the controller has one web server, not two.

**D15. Configs: Jinja2 templates rendered from `fortress.yaml`; no Ansible.**
- Every config (FRR, dnsmasq, strongSwan, rsyslog, iptables) is a Jinja2 template in `configs/<service>/`, e.g. `configs/frr/frr.conf.j2`. `scripts/render_configs.py` fills them in from `fortress.yaml` for each node and writes them to `/etc/fortress/<node>/` in the VM. The topology's start-up code then starts each daemon in its namespace with that config. This is our **infrastructure as code**: one YAML file plus templates define the whole network, and a change in the YAML flows into every config.
- **Ansible is skipped.** It would need sshd in every namespace before anything else works, adds a tool to learn and explain, and gives nothing the templates don't. The roadmap's optional Ansible items stay unticked; in the viva, explain the template approach as the same idea (declarative config, generated rather than hand-written).
- So the repo drops `ansible/` and the per-router `configs/frr/r1..r4/` folders: one template per service replaces four hand-written copies.

## T03: Topology

**D16. Topology details.**
- **Interface names say what they connect.** `s1-c1` is s1's uplink to c1, `r1-r3` is r1's link to r3, `r1-lan.10` is VLAN 10 on r1's trunk, `s4-bar3` is bar3's port on s4. `ovs-vsctl show`, `ip addr` and the FRR configs can then be read without a lookup table. Linux allows 15 characters per name, and every name fits.
- **Fixed MACs: `02:fe` + the host's IP bytes** (192.168.10.10 → `02:fe:c0:a8:0a:0a`). `02` marks a locally administered address, so it never clashes with a real vendor. Fixed MACs let dnsmasq always hand a host the same reserved IP (T04).
- **Explicit datapath IDs** (cores `0x101`, `0x102`, access `0x201`–`0x205`, FOB `0x301`). Mininet would otherwise take the digits from the name, so c1 and s1 would both be datapath 1.
- **RSTP root is c1 (priority 4096), backup root c2 (8192).** The tree is then predictable for the viva: every access switch forwards on its c1 uplink and blocks its c2 uplink (5 Alternate ports). Hosts, routers and ids1 sit on RSTP edge ports.
- **The topology waits for RSTP** instead of sleeping a fixed time: it polls `ovs-appctl rstp/show` until every Root and Designated port is Forwarding (about 5 s here). With a fixed 3 s wait, the first ping after start-up failed.
- **s6 (FOB LAN) is always a plain learning switch,** even in controller mode, because the FOB is outside the base's SDN domain. It has no loops, so it needs no RSTP.
- **ids1's two ports are parked on unused VLAN 4094** until T17 turns them into mirror outputs, so they receive nothing yet.
- **Exactly one internet host, cabled straight to r4.** More would need a switch; that's a one-line change if it's ever needed.
- **`--check` mode** builds the lab, runs `topology/checks.py` and exits with the number of failures. It's how the automated tests (and later `start_all.sh`) know the lab is healthy.

## T04: DHCP and DNS

**D17. DHCP and DNS details.**
- **dnsmasq on srv1** does both DHCP and DNS from one config rendered from `fortress.yaml` (`configs/dnsmasq/dnsmasq.conf.j2`). It has one scope per zone, and a reservation per host by MAC, so every host always gets the address listed in the YAML. Tests and attack scenarios can then rely on `bar3` being 192.168.40.66. DNS answers only for `fortress.mil` (the lab has no upstream DNS).
- **DHCP relays on both r1 and r2** (ISC `dhcrelay`). The relay puts its own zone address in the request (`giaddr`), and dnsmasq picks the matching scope from it. srv1 answers both relays and the client takes the first offer; both offers carry the same reserved address. "Packet to bogus giaddr" lines in the VM's syslog are harmless: r1 sees replies addressed to r2's relay and drops its copy.
- **BusyBox `udhcpc` as the DHCP client, not Ubuntu's `dhclient`.** Ubuntu's AppArmor profile for dhclient only lets it run its stock hook script and write leases to `/var/lib/dhcp`. Our hook script was refused ("Permission denied" in the AppArmor audit log). The stock script would also overwrite the VM's shared `/etc/resolv.conf`. We don't loosen the VM's AppArmor profile; `udhcpc` has none and runs our hook `configs/udhcpc/fortress-udhcpc-script`, which only sets the host's own address and route and writes DNS settings to `/var/run/fortress/<host>/resolv.conf`.
- **Hosts share the VM's `/etc/resolv.conf`,** so `ping srv1.fortress.mil` inside a host would ask the VM's resolver, not srv1. Checks and demos use `dig @<server from the lease>`, which tests what DHCP really handed out.
- **Temporary gateways (T04–T05 only):** srv1's replies to the relays and every host's default route need the zone gateway `.1`, which VRRP only provides from T06. Until then r1 holds each `.1` as a plain extra address. T06 replaces this.
- **We record udhcpc's PID ourselves,** because it runs in the foreground in the background (`-f ... &`), so `services.stop()` can always kill it. dhclient only wrote its PID file after getting a lease, so failed clients were left running.
