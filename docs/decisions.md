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

## T05: Routing

**D18. Routing details.**
- **One FRR instance per router** with FRR's own multi-instance support: `frrinit.sh start r1`, run through `net['r1'].cmd()`, so it lives in r1's namespace. It reads `/etc/frr/r1/{daemons,frr.conf,vtysh.conf}` and keeps sockets and PIDs in `/var/run/frr/r1/`. `vtysh -N r1` talks to r1 only. All three files are rendered from `configs/frr/*.j2`. `vtysh.conf` is empty, but vtysh refuses to apply `frr.conf` at boot without it ("processing failure").
- **OSPF settings:** area 0 everywhere. Router links are `point-to-point` (no designated-router election on a /30) with **hello 1 s / dead 4 s**, so a failed neighbour is noticed in about 4 s instead of the default 40 s; all 10 adjacencies reach Full about 7.5 s after start. Zone LANs, the FOB LAN and the internet LAN are `passive`: advertised, but no OSPF packets are sent towards hosts.
- **Costs** come from `fortress.yaml`: r1–r3 = 10 (primary WAN), r2–r3 = 100 (backup), everything else 10. `traceroute` from Command to the FOB shows `192.168.10.2` (r1), then `10.0.0.6` (r3 on the primary link).
- **Floating static route on r2:** `ip route 172.16.70.0/24 10.0.0.10 250`. Distance 250 loses to OSPF's 110, so it sits in the routing table unused (FRR logs "over-ridden by better route") and only takes over if OSPF loses every path to the FOB.
- **The ISP router r4 is in OSPF area 0** (as the roadmap asks) and originates the default route (`default-information originate always`), so "everything unknown" goes towards r4. **We still NAT on r1/r2** (`MASQUERADE` out of `r1-r4`/`r2-r4`), so the internet host only ever sees the gateways' public-side addresses. Be ready to say in the viva: because r4 also learns the internal routes through OSPF, NAT isn't strictly needed for reachability here. A real design would keep the ISP out of the internal OSPF area and use only a default route plus NAT. We kept r4 in OSPF because the roadmap asks for it, and it keeps the routing demo in one protocol.
- **iptables rules** are rendered per gateway (`configs/iptables/rules.j2`) and loaded with `iptables-restore` inside the router's namespace, never on the VM.

## T06: Gateway redundancy

**D19. VRRP with FRR's vrrpd (no keepalived fallback needed).**
- Every zone has one virtual router, **VRID = VLAN number** (VRRP 40 is always Barracks). Its gateway address is the zone's `.1`; r1 has priority 120, r2 100, with preemption on, VRRP version 3, and an advert every **200 ms** (FRR's default is 1000 ms).
- **macvlan per virtual router**, as FRR's vrrpd requires: `r1-vrrp10` sits on `r1-lan.10`, has the standard virtual MAC `00:00:5e:00:01:0a`, and holds `192.168.10.1`. vrrpd switches it "protodown" on the Backup, so only the Master answers for the gateway. Hosts' ARP tables show the virtual MAC, so after a failover nothing on the hosts has to change; the new Master's gratuitous ARP moves the MAC in the switches.
- **Two kernel details that cost a debugging round:** FRR's docs create the macvlan with `addrgenmode random`, which this kernel refuses ("Address family not supported"), so we leave it out. IPv6 is switched off on the macvlans instead, because r1's and r2's share one MAC and would otherwise get the same IPv6 address. The routers also set `arp_ignore=1` / `arp_announce=2`, so the plain VLAN interface never answers ARP for the gateway address on the macvlan.
- **VRRP starts before DHCP.** srv1's replies to the relays leave through the Servers gateway, so without VRRP there's no DHCP. This also removed T04's temporary gateway addresses on r1.
- **`fail <node>` / `recover <node>`** in the lab CLI simulate a power loss: every interface of the node goes down, and back up. In `--check` mode the failover test does this to r1 while cmd1 pings its gateway 10 times a second. Measured: r2 Master of every gateway after **0.9 s**, about **1.0 s** of lost pings (10 of 156), and r1 Master again **0.3 s** after it recovers. These are the first numbers for the failover table (roadmap Section 6; T25 repeats them properly).
- **"Agree again" means both routers.** While cut off, r1 can't hear r2 and still believes it is Master. The recovery check waits for r1 Master **and** r2 Backup.

## T07: Lab start and stop

**D20. One command up, one command clean.**
- **`start_all.sh`** runs, in order: `stop_all.sh` (clean slate), a VM clock step (D9), `check_config.py`, then `fortress_topo.py --standalone`. The topology does the rest: build, RSTP, FRR (OSPF + VRRP) and NAT, then DHCP/DNS, the health check, and the CLI. Each step needs the one before it (DHCP needs VRRP's gateways; checks need everything). `--check` replaces the CLI with the disruptive VRRP failover test and exits with the number of failures; `--small` passes through. From T08 it will also start the controller.
- **`stop_all.sh`** handles every way a lab can end badly. It stops a lab still running elsewhere, stops our daemons through their PID files (FRR through `frrinit.sh stop <router>`), removes our custom-named interfaces (Mininet's `mn -c` only knows names like `s1-eth1`, so it misses `s1-c1`), runs `mn -c`, and then **proves** the VM is clean: no lab processes, no OVS bridges, no lab interfaces. A killed run left 22 interfaces behind, and the next start failed with "File exists". That is why `start_all.sh` always calls it first.
- **Tested from a fresh VM boot:** one `start_all.sh` passed 66/66 health checks, a scripted CLI session ran `fail r1` / `recover r1` (r2 Master of all six gateways, then r1 again), `exit` stopped it, and `stop_all.sh` found nothing to clean. `--check` passes 71/71 (full) and 56/56 (`--small`).
- **Two traps worth knowing:** `mn -c` deletes `/tmp/*.log` and `/tmp/*.out`, so lab logs must not use those names (we log to `/var/log/fortress/`). `pkill -f pattern` run through `vm.sh` also matches the wrapper shell's own command line and kills it; writing the pattern as `[t]opology/...` avoids that.

## T08: SDN controller (base)

**D21. How the Ryu controller forwards.**
- **Four-table OpenFlow 1.3 pipeline,** set up once so later tasks only add rules: table 0 INGRESS (port roles, VLAN push), table 1 POLICY (zone isolation, T13), table 2 QOS (queues, T12), table 3 FORWARD (destination rules). Ports are classified from their names (`controller/vlan.py`): access ports get their VLAN pushed, trunks and links must be tagged, ids1's and unknown ports get no rule, so they are dropped. IPv6 is dropped at ingress (the lab is IPv4-only, and it saves the controller from hosts' IPv6 chatter).
- **Destination-based rules on a single tree.** For each learned (VLAN, MAC) every switch gets one rule in table 3, following one shortest-path tree rooted at the destination's switch (`controller/paths.py`). All switches use the same tree, so their choices can never loop. Links touching c2 cost 2, so c1 is the normal core and c2 the standby (the same split RSTP had).
- **Flooding by the controller, not by the switches.** Broadcasts, multicasts and unknown destinations come to the controller, which sends one copy out of every host and router port in that VLAN on every switch, never over switch-to-switch links. The dual uplinks therefore can't loop, and no spanning tree is needed. Cost: every ARP, DHCP and VRRP advert passes through the controller (about 30 VRRP adverts/s from six zones), which is fine at lab scale. The roadmap's "flood along a spanning tree" would be the next step if the controller ever became a bottleneck.
- **Hosts that move are re-pointed at once.** When a MAC shows up on a new port, its rules are rebuilt. This is what makes VRRP failover lossless under the controller: r2's first advert from the virtual MAC moves it to r2's trunk, and the failover test lost **0 of 174** pings (it lost about 1 s under RSTP).
- **The first-packet race (a real bug, fixed).** The first packet to a new destination used to be sent out of its first hop right after the rules were sent to all switches. OpenFlow doesn't order messages across switches, so the packet could reach the next switch before its rule and be dropped; the first DNS query in every zone took 2 s (dig's retry). Now that packet is sent straight out of the destination's own port, and any packet that still arrives early from a link is delivered the same way.
- **Module names:** `controller/netgraph.py` holds the switch graph and host table. (D14 called it `topology.py`; renamed so it can't clash with `ryu.topology` or the repo's `topology/` folder.) `rest_api.py` serves `GET /fortress/status` on port 8080; the checks use it.
- **s6 (FOB LAN) has no controller,** as D16 planned. `fortress_topo.py --standalone` still gives the T03–T07 RSTP setup for comparison or as a fallback.

## T09: Reactive rerouting

**D22. Rerouting on the port-down message.** When a switch reports a link port down (OpenFlow `PortStatus`), the controller removes that link from its graph, recomputes every destination's tree and overwrites the affected rules (`reroute()` in `fortress_ctrl.py`). That message is the earliest signal available. Ryu's LLDP discovery reports the same loss a little later and then finds nothing left to do. When LLDP sees the link again, traffic moves back to the shortest path. Every event is one JSON line in `/var/log/fortress/ryu/reroutes.jsonl`: time, event, link, controller time in ms, and for every switch whose path to a destination changed, the old and new switch path (e.g. `comms1: s1-c1-s3 → s1-c2-s3`). Links discovered at start-up that change nothing aren't logged. Measured in `--check`: cutting c1–s1 rerouted 29 paths in about 20 ms of controller time and lost 0 of 121 pings at 10 pings/s.

## T09b: Early measurements

**D23. `scripts/measure.py` and the first numbers.** It starts the lab (`fortress_topo.start_lab()`), runs a **100 pings/s** stream (`ping -D` timestamps every reply), triggers one failure, restores it, and reports lost pings and **recovery time = the longest gap between two replies**. That is accurate to about 10 ms. Rows are appended to `docs/results/early.csv`. First results (3 runs each, cmd1 → srv3, reactive mode):

| Failure | Lost | Recovery |
|---------|------|----------|
| Link c1–s1 down | 1–2 of ~400 | 52–61 ms |
| r1 power loss (all interfaces down) | 1–2 of ~430 | 51–52 ms |

The r1 result is faster than VRRP's own takeover (r2 Master after about 0.7 s, T06). **Not yet verified** explanation: when r1's trunk port goes down, the controller forgets where the virtual MAC was and floods frames for it to every trunk, including r2's, and r2's kernel forwards them before VRRP has finished electing it. T25 will confirm or correct this with a packet capture on r2's trunk. iperf3 streams are left to T25.

## T10: Link aggregation between the cores

**D24. Controller-based link aggregation, not an OVS LACP bond (a deviation from the task text).**
- **What we tried first:** an OVS bond (`ovs-vsctl add-bond c1 c1-c2 c1-c2a c1-c2b lacp=active`). LACP negotiated fine (`bond/show`: both members enabled, `lacp_status: negotiated`), yet pings across the cores failed **100% whenever both cables were up**, in every mode (balance-slb, balance-tcp, active-backup, with and without LACP), and worked only with one cable down.
- **Why:** an OVS bond is a feature of OVS's built-in `NORMAL` forwarding. Under an OpenFlow controller every bond member is still its own OpenFlow port (`c2-c1a` = 10, `c2-c1b` = 11), and the controller's rules bypass the bond logic. Our controller didn't recognise those member ports as switch links, so frames arriving on them were dropped.
- **What we do instead:** the two cables stay plain ports, and **the controller aggregates them**. Both are members of one edge in its graph (`netgraph.py`). Traffic towards the other core uses an OpenFlow **select group** with one bucket per cable: the switch hashes each flow onto one cable, which is what a LAG / EtherChannel does. Each bucket watches its own port, so a dead cable is skipped by the switch itself. The controller logs `member_down` / `member_up`, and only reroutes (`link_down`) when the last cable goes. Measured: one cable pulled under 10 pings/s, **0 of 83 lost**, no path changed, and the cable rejoins when LLDP sees it again.
- **Proactive mode (T11)** buckets that lead over the aggregated link watch the select group itself (`watch_group`), so they stay live while any cable is up.
- **In standalone mode** (`--standalone`) the two cables are just two parallel links and RSTP blocks one.
- **For the viva:** LACP (802.3ad) is the protocol two switches use to agree to bundle cables. Here the bundling decision is made centrally by the controller, which already knows both cables lead to the same switch (LLDP). The data-plane behaviour is the same: per-flow load sharing and hitless loss of one cable. We don't run the LACP protocol itself.

## T11: Proactive fast-failover

**D25. Fast-failover groups, and what the numbers show.**
- `--mode proactive` (passed to the controller as `FORTRESS_MODE`) makes every forwarding rule, except on the destination's own switch, point at an **OpenFlow fast-failover group** with two buckets: the primary next hop and a backup next hop. The backup is the best path once the primary link is removed (`paths.backup_next_hop`). Each bucket watches its port; the switch uses the first live bucket, so it fails over **in the data plane, without the controller**. Afterwards the reactive code (T09) still recomputes the trees and refreshes the groups. In the full lab: 186 groups; all failover checks pass with 0 lost pings at 10 pings/s.
- **Known limit:** a backup next hop is only checked against the *primary* link, not against the neighbour's own choice. In this two-tier topology the neighbour's shortest path never leads back, so there are no loops; a deeper topology would need loop-free alternates (the standard name is LFA) or per-ingress rules.
- **Measured** (`scripts/measure.py link c1 s1`, 100 pings/s, 5 runs each, `docs/results/early.csv`):

| Mode | Lost pings per failure | Longest gap |
|------|------------------------|-------------|
| reactive | 2, 3, 2, 2, 2 | 51–56 ms |
| proactive | 1, 1, 1, 0, 1 | 50–51 ms |

  Proactive loses fewer packets (the switch reroutes before the controller has even heard of the failure). The **~50 ms gap appears in both modes, even in a run with 0 lost pings**, so it is a stall, not loss, caused by injecting the failure: taking both veth ends down makes OVS revalidate its datapath flows. Our 10 ms ping resolution can't separate the two modes' recovery times below that floor. T25 should measure with a failure that doesn't stall the datapath (for example dropping the cable's packets with `tc` instead of downing the interface) to show the real gap.

## T12: Mission-priority traffic

**D26. QoS with OVS HTB queues and controller-chosen queues.**
- **Queues** (`qos:` in `fortress.yaml`): every port shared by several zones (switch links, router trunks, the servers' ports on s5) gets a Linux HTB setup capped at **50 Mbit/s**, split into queue 0 *normal* (min 10, priority 1), queue 1 *mission* (min 30, priority 0) and queue 2 *bulk* (min 5, priority 2). HTB guarantees each queue its minimum and hands spare capacity to the lowest priority number first. The 50 Mbit/s cap exists so a test flood can actually fill a link: Mininet's veth links have no speed of their own.
- **Classification** is the controller's QOS table (table 2): traffic **to or from** Command and Communications → `set_queue 1`; traffic **from** Barracks → `set_queue 2`; everything else → queue 0. It matches on IP subnets, not VLANs, because routed traffic changes VLAN at the router but keeps its source and destination addresses, so Command's packets stay in the mission queue on both legs.
- **Measured** in `--check` (`docs/results/qos.csv`): iperf3 asks for 100 Mbit/s UDP from bar1 to srv3 and gets **48.6 Mbit/s** (the cap). Meanwhile Command's ping to srv1 stays **under 0.4 ms** (0.34 quiet, 0.04 during the flood), while another Barracks host's ping to srv1 goes from 0.71 ms to **11.4 ms**, queued behind mission traffic. (Command being faster during the flood is most likely the busy VM keeping the CPU awake, not QoS.)
- Standalone mode has no queues (`set_queue` needs the controller).
- **For the viva:** this is the military theme driving a real QoS design: during a failure the surviving links are shared, and Command's traffic must not suffer for the Barracks' downloads.

## T13: Zone isolation and rogue control

**D27. Isolation split between the controller and the routers, plus port security.**
- **The "Allowed to reach" column** is `reach:` in `fortress.yaml` (the FOB got one too: Command and Servers). It means "zones this zone may *start* connections to".
- **Stateless vs stateful:** OpenFlow rules have no memory of connections, so the controller alone can't express "Command may reach Barracks, but Barracks may not reach Command": the replies from Barracks look like any other Barracks packet. So the work is split:
  - **Controller (POLICY table):** drops IPv4 between zone pairs where *neither* side may start (`Lab.blocked_pairs()`: Intelligence, Communications, Barracks and Management never talk to each other; 12 directed pairs). These never reach a router.
  - **r1/r2 (iptables FORWARD, `configs/iptables/rules.j2`):** default DROP; allow `ESTABLISHED,RELATED` (conntrack, so replies return), DNS to srv1 from every zone, the monitoring host, and one ACCEPT per `reach` entry, including the FOB and the internet (out of the link to r4). All traffic between zones is routed through r1/r2, so they see every connection's first packet. This is the defence in depth the roadmap asks for.
- **Port security:** each access port's ingress rule matches the MAC of the one host that belongs there (fixed MACs, D16). A different device, or a spoofed MAC, gets no rule and is dropped. An unknown device on a new port gets no rule at all. This is our MAC allowlist: the allowlist *is* `fortress.yaml`.
- **Rogue DHCP:** every access port except srv1's drops UDP source port 67 (DHCP server replies) at ingress. The relays on r1/r2 arrive on trunk ports, so they are unaffected.
- **Tested in `--check`:** Barracks → Intelligence blocked (controller), Barracks → Command blocked (router) while Command → Barracks works, and Barracks → Servers works. An unknown host plugged into s4 reaches nobody and gets no DHCP lease. A fake DHCP server on bar1 really sent an OFFER (`192.168.40.154`), the controller dropped it, and bar2 still got its reserved `192.168.40.11` from srv1.
- **Gotcha:** OVS updates rule packet counters periodically, so checks wait about 1.5 s before reading them.

## T14: IPsec VPN to the FOB

**D28. strongSwan per router, loaded over VICI.**
- **One `charon-systemd` daemon in r1's and in r3's namespace**, each with its own `strongswan.conf` (passed in `STRONGSWAN_CONF`), control socket and log in `/var/log/fortress/<router>/charon.log`. The classic `charon` daemon has a fixed PID file, so two copies can't run on one VM.
- **The tunnel:** IKEv2 with a pre-shared key, AES-256/SHA-256/DH-14 for IKE, AES-256-GCM ESP in **tunnel mode**, protecting `192.168.0.0/16, 10.10.60.0/24 ↔ 172.16.70.0/24`. It runs between the two ends of the primary WAN link (10.0.0.5 ↔ 10.0.0.6). The Servers subnet is included because Servers may reach the FOB (see the T01 note on `fortress.yaml`).
- **Loaded with strongSwan's Python VICI library (`topology/vpn.py`), not `swanctl`.** Ubuntu's AppArmor profile only lets `swanctl` use the default socket `/run/charon.vici` ("Permission denied" in the audit log), and two routers need two sockets. VICI is strongSwan's own control protocol (swanctl speaks it too), so `vpn.json` holds exactly the settings a `swanctl.conf` would. We don't loosen AppArmor.
- **The key:** made fresh at every lab start with `secrets.token_urlsafe(32)` (or taken from `FORTRESS_VPN_PSK`), and only written to `vpn.json` (mode 600) on the VM's own disk. It never goes in the repo.
- **Checking "only ESP on the wire" needs care:** Linux shows a decrypted incoming tunnel packet on the receiving interface again, so a plain `tcpdump -i r1-r3` shows plaintext ICMP that never crossed the cable. The check captures *outgoing* packets on both ends (`-Q out`), which together are exactly what crosses the link: 10 ESP, 0 plain ICMP.
- **Known limit, failover:** the tunnel exists only on r1. If r1 dies, r3's dead-peer detection (1 s checks, about 3 s to give up, tuned with `retransmit_*`) removes the tunnel, and base ↔ FOB traffic continues **unencrypted** over the backup path via r2. Before that tuning, r3 kept the stale tunnel for minutes and dropped the traffic. The proper fix is a second tunnel on r2 with route-based VPN (XFRM interfaces), so whichever router is active encrypts; it is noted as future work.

## T15: SSH and central logging

**D29. One sshd and one rsyslog per node, logs over the lab network.**
- **SSH:** one `sshd` per node (r1, r2, srv1–srv3), each with its own config, host key, PID file and log (`sshd -E /var/log/fortress/<node>/sshd.log`). Routers listen only on their Management-VLAN address. Each node's iptables INPUT allows port 22 only from `192.168.99.0/24`, and r1/r2 forward SSH from Management to the servers. Admins log in as the lab user with **`/etc/fortress/ssh/admin_key`**, made once per VM and never committed. Telnet isn't installed.
- **`UsePAM yes` is needed:** the lab user's password is locked, and with `UsePAM no` sshd treats a locked account as invalid even for key logins ("Failed password for invalid user"). With PAM, keys work and every password attempt fails (the account has no password), but each attempt is logged. That is what the brute-force detection (T20) needs.
- **Central logging:** srv2 runs an rsyslog **server** (UDP 514, one file per sender in `/var/log/fortress/srv2/remote/<node>.log`). r1, r2, r3, srv1, srv3 and the controller each run an rsyslog **forwarder** that tails their own log files (`imfile`) and sends every line across the lab network. Every Mininet host has the same hostname, so each forwarder writes its node name into the messages. r4 is excluded: it is the simulated ISP, not base equipment. The routers' firewall allows syslog to srv2 from any lab address, and the controller's forwarder reaches srv2 through mon0.
- **`freshStartTail`:** without it the forwarders resent the full history of earlier runs at start-up (thousands of lines from r3 alone), and that burst briefly cut the Servers zone off. Now only new lines are sent.
- **Tested:** admin1 SSHs into r1 and into srv1 (routed through r1); bar1 can't SSH into r1; a wrong-password attempt from admin1 appears in `srv2/remote/r1.log` as "Failed password for ubuntu from 192.168.99.10"; srv2 holds r3's and the controller's logs.
- chrony as a lab NTP server (optional) is not done: every node shares the VM's clock, which chrony already keeps (D9), so all log timestamps already line up.

## T17: Suricata on the mirror ports

**D30. Mirror both cores; our own rules; the SID says what fired.**
- **One OVS mirror per core** (`select_all=true`) with ids1's port on that core as the output. Naming a port as a mirror output also *reserves* it: OVS discards anything that arrives on it, so the IDS cannot inject traffic into the lab even though it hangs off a core switch. That is free port security for the one host that sees everything.
- **Each packet can be mirrored up to four times** (ingress and egress on a core, and a packet may cross both cores). The collector removes the duplicates (D31). Suricata keeps them, so its counting rules fire at roughly half the stated number of packets; the thresholds are set with that in mind and it only makes detection earlier, never later.
- **Our own five rules first** (`detection/suricata/local.rules`): a SYN sweep, NULL/FIN/Xmas probes, and an SSH connection-rate rule. They cover the two insider actions in scenario 3 and every one of them can be explained line by line in the viva. ET Open can be dropped in beside them as `et-open.rules` and the config picks it up, but thousands of signatures nobody on the team has read are not evidence of anything.
- **The SID range identifies the kind of alert, not the category.** eve.json's `category` is the human-readable description from Suricata's classification file ("Attempted Information Leak"), not the rule's `classtype`. Our rules therefore use reserved local SIDs with meaning: 1000001–1000009 scans, 1000010–1000019 brute force, and `sources.ids_kind()` maps a SID to `scan` / `bruteforce` / `other`. The first version of the check matched on `category` and found nothing while 372 alerts sat in the file.
- **The rules are copied to `/etc/fortress/ids1/rules/`** at start-up rather than read from the shared folder, so a half-saved edit on the Mac cannot take the IDS down mid-run.
- **Measured:** a 200-port sweep from the Barracks attacker host produced 372 scan alerts, and Suricata reported "engine started" 0.5 s after launch.

## T18: Traffic features and the baseline

**D31. Six features per host per 5 seconds, each packet counted once.**
- `detection/flow_collector.py` sniffs both mirror ports inside ids1 and summarises every host's behaviour each window: packets/s, bytes/s, new connections/s, **distinct destination ports**, distinct destination hosts, and **refused or unanswered connections**. The last two are what separate a sweep from a user.
- **Counting each packet once** needs two rules: ignore a packet unless its VLAN tag is the *source's* own zone VLAN (so a routed packet isn't counted again on the far side, D13), and remember the packets already counted this window (so the mirror's duplicates don't inflate anything).
- **srv3 runs a plain HTTP server** (`python3 -m http.server`, with a 1 MiB `file.bin`) so the baseline has something real to fetch, and the demo has a target.
- **The baseline only uses pairs `fortress.yaml` allows.** Generating traffic the firewall drops would teach the model that blocked traffic is normal, which is exactly backwards.
- **Two collector instances, one each way:** the always-on one keeps `latest.json` for the threat engine; `baseline_traffic.py` starts a second one that writes the training CSV with the label `normal`. Two sniffers on one interface each get their own copy of every packet.
- **`detection/features.py` holds the feature list and nothing else,** because the collector and the model run in the fortress venv (scapy, scikit-learn) while the topology and its checks run on the system Python that Mininet lives in. The first version had the checks import the collector, which pulled in scapy and failed.
- **Measured:** 25 s of baseline gave 43 clean rows across 13 hosts, with **at most 3 distinct destination ports** per host per window. The scan in the same run touched 200. That gap is what the model and the port-sweep rule are built on.

## T19: The anomaly model

**D32. An Isolation Forest trained only on normal traffic, judged on normal traffic it has never seen.**
- **Why this model:** it needs no attack examples. We have plenty of normal traffic and exactly two attack scenarios, so a classifier trained on labelled attacks would learn to recognise *those two* and nothing else. An Isolation Forest learns the shape of normal and reports how far a sample sits outside it, which is the honest version of "this host is behaving oddly".
- **Scaling first matters.** `bytes_per_s` runs into the thousands while `distinct_dst_ports` is in single digits; without a `StandardScaler` the forest would effectively only look at the byte counts. The model is a two-step scikit-learn pipeline.
- **The 0..1 scale is defined, not arbitrary.** 1.0 means "as far outside the baseline as the single most unusual thing in the training data"; anything beyond that is also 1.0. That is a sentence a student can defend, unlike a raw `decision_function` value.
- **How it is evaluated, and why that is honest:** the baseline is split, and the model never sees the held-out part. Both parts are normal traffic, so *every* alarm on the held-out part is a false alarm and the false-positive rate is measurable without a single attack sample. `train_model.py` exits non-zero if it is above 5%.
- **The model can never declare a breach by itself:** it contributes at most 30 of 100 points (D33).
- **Measured:** trained on 1141 rows of a 15-minute baseline (1630 rows, 13 hosts), **2.2% false positives** on 489 held-out normal rows, against the 5% target. Mean anomaly score on normal traffic is 0.010, so normal traffic contributes essentially nothing to a score. The highest-scoring normal rows are srv3 serving large downloads (0.81, 67 packets/s), which is the right answer: that is the most unusual *legitimate* thing in the lab.
- **A bug in the baseline that would have poisoned this:** the generator capped each zone's destinations at two hosts, which silently excluded srv3, the web server. The first baseline therefore contained no file transfers at all (peak 1.7 KB/s instead of 622 KB/s), and the model would have flagged every real download as an anomaly.
- **No model is a supported state.** The engine says so on startup and scores on its rules alone. `detection/data/` is git-ignored: a model trained against one machine's lab is not evidence for another's, and it is regenerated from the baseline in two commands.

## T20: The explainable threat engine

**D33. One score, the reasons that produced it, and never a breach from a single source.**
- Three independent sources, each written by a different part of the lab: Suricata's signatures (T17), the per-host traffic features and the model (T18, T19), and the failed SSH logins collected centrally on srv2 (T15). `detection/sources.py` reads all three; nothing else in the engine touches a file.
- **The weights are chosen so that a breach needs two independent signals.** A lone Suricata alert is 30 points, and the model alone can reach 30: both land in the Normal band. The `--self-check` proves this with worked examples, including "one signal plus a maximal anomaly score" coming out at 60 (Suspicious).
- **Correlation is the point of having three sources**, and it is worth points of its own: a host that sweeps ports *and then* tries passwords scores higher than one that did either, and an IDS brute-force signature backed by real failed logins in the central log scores higher than the signature alone.
- **Different windows for different evidence:** traffic is summarised every 5 s, Suricata alerts are read over the last 30 s, and failed logins over the last 120 s, because a login attempt is a human-speed event and five of them do not fit in one 5-second window.
- **`--self-check` is the test that matters.** The scoring is a pure function, so the whole table can be checked against worked examples without generating any traffic at all. That is also what makes the weights safe to tune later.
- **A known false positive, handled by design:** our SSH connection-rate rule can fire on a burst of *legitimate* admin logins. That is 30 points, which is Normal. The engine only escalates when a second, independent source agrees, which is exactly why it is built this way.
- **Measured:** a 200-port sweep from the Barracks attacker scored **93/100** on four independent signals (Suricata +30, 109 ports in one window +30, 304 refused connections +15, anomaly 0.59 +18), while every normal host in the same run stayed at 0.

## T21: Automatic response

**D34. Block the host at its own access port, and let the block expire.**
- The drop rule goes in table 0 at priority 500, matching the host's port **and** its MAC, so the attacker's traffic never reaches the core at all and no other host's traffic is touched. The forwarding rules pointing at the host are removed too, so nobody can reach it either.
- **The port is found from `fortress.yaml`**, not from what the controller has learned, so a host that has gone quiet can still be blocked.
- **Blocking is idempotent.** The engine re-asks every few seconds while a host stays in breach; a repeat refreshes the expiry instead of reinstalling the rule and filling the history.
- **Every block expires** (300 s by default, lifted by a timer in the controller), so a wrong block heals itself instead of needing someone at a keyboard. It can also be lifted immediately with `POST /fortress/unblock`.
- **A real bug worth remembering:** lifting a block with OpenFlow's ordinary (non-strict) delete also removes rules whose match is *more specific* than the one given, which would have taken the port's own admit rule with it and left the host cut off for good. The unblock therefore uses a strict delete with the exact priority. The priority map now lives in `controller/pipeline.py` so this kind of overlap is visible in one place.
- **The monitoring host is deliberately not blockable.** It is exempt from zone isolation (D27), and cutting it off would blind Prometheus and the dashboard at the moment they matter most.
- **Measured:** the attacker was cut off at its own port **10 s after the sweep started** (the roadmap's target was 10 s), its pings stopped, the other Barracks host was unaffected, and `POST /fortress/unblock` restored it.

## T23: The monitoring stack

**D35. A pull exporter that re-reads its sources on every scrape.**
- `monitoring/exporter.py` holds no state: each scrape re-reads the controller's REST API, the collector's latest window, the engine's scores and Suricata's alerts. A stale dashboard is therefore impossible, and the exporter can be restarted at any time without losing history, because Prometheus keeps that.
- **Prometheus runs with our own config and data directory**, not the packaged service, so the scrape list is in the repo (`monitoring/prometheus.yml`) and the lab's data cannot be confused with anything else on the VM.
- **The Grafana dashboard is provisioned from the repo**, not clicked together in a browser. `monitoring/grafana/dashboards/command-center.json` is copied in at lab start, so the dashboard is version-controlled and a teammate gets the same one.
- The panels answer the three questions in order: is anything wrong now (breach, suspicious and blocked counts, switches and links up), what is the threat picture (score per host, alerts by kind), and what is the network doing (traffic per zone, connection attempts, ports touched per host).

## T24: Alerts and incident reports

**D36. The engine sends the breach alert, because it is the only thing that knows why.**
- The roadmap's plan was a Telegram contact point in Grafana. The engine sends it instead: the message carries the score **and the reasons that produced it**, which Grafana's templating cannot reach. Grafana's own rules are still provisioned for what the engine cannot see (a link down) and as a second path for breaches.
- **Grafana refuses to start if a Telegram contact point has an empty bot token** ("could not find Bot Token in settings"), which is the default state for anyone who has not made a bot yet. The alerting provisioning is therefore only written when a token is actually configured; without one, the dashboard still comes up and the engine still scores, blocks and writes reports.
- **The token is never in the repo.** It is read from the environment or a git-ignored `.env` (`.env.example` documents how to get one from BotFather). The rendered Grafana file is mode 600 and owned by Grafana.
- **Evidence before notification.** On a breach the engine blocks the host, writes `docs/incidents/<timestamp>-<host>.md`, and only then sends the message: containing the attack and recording what happened must not wait for a round trip to Telegram.
- **One report and one alert per host per 300 s.** The score is recomputed every 5 s, and without this a single sustained breach would write a report and send a message twelve times a minute. The block is still refreshed on every pass.
- Incident reports are git-ignored: they are real evidence, but they are regenerated on every test run, so they stay local and get attached to the report rather than committed.

## T22: Threat-aware rerouting (attempted, then dropped)

**D37. Not done, because this topology cannot separate a zone from mission traffic by path.**
The roadmap marks T22 optional: during an insider attack the controller should quarantine the
compromised access switch's traffic and route Command's around it. It was built and tested, and
then removed. What the test showed:

- **An access switch is never a transit node here.** Command sits on s1 and the servers on s5,
  so Command-to-Servers traffic goes s1 to a core to s5. It never touches s4 (Barracks) in the
  first place, so there is nothing to route "around".
- **The cores are not a free choice for routed traffic.** Barracks may only reach the servers
  and the internet, which are both *routed* destinations, so its traffic has to reach the VRRP
  master r1 — and r1's trunk is on c1 only (r2's is on c2 only). Steering the zone onto the
  standby core produces `s4 -> c2 -> c1`: a longer path to the same core, not separation. The
  check proved it, showing s4 using both uplinks before *and* after the quarantine, because
  different destinations legitimately resolve to different cores (traffic to r1 via c1, to r2
  via c2).
- **Sending the zone to the other router instead is not possible either.** Hosts address their
  gateway by the VRRP virtual MAC, and the Backup keeps that MAC's interface down on purpose,
  so frames for it can only be delivered to the current master.
- **The QoS variant would duplicate T12.** Demoting a compromised zone's traffic to the lowest
  queue is a real network-level response, but Barracks is already in the bulk queue (D26), so
  for the demo's zone it changes nothing.

What the response does instead, and why it is enough: the host is cut off at **its own access
port** (T21), so its traffic never reaches a core switch at all. Containing the attacker at the
edge is strictly better than steering it around the network, and the three demo scenarios do
not need path changes to tell their story.

**What would make T22 worthwhile:** a three-tier topology where access switches transit each
other's traffic, or gateway routers trunked to *both* cores so the core really is a choice for
routed traffic. Both are changes to T03's topology, not to the controller, and the controller
side is a small change once the topology supports it: the path weights already take which core
to avoid as a parameter.
