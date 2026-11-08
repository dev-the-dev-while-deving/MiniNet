# Viva prep sheet

Every topic from the roadmap (section 7), with a short answer you can say out loud and the exact
place in our code where it happens. Links go to the line (`file:line`). Learn the answer, then
open the line and make sure you could explain it from the code alone.

**How to use this:** each person takes a third of the topics, explains them to the others from
the code, then swaps. Aim to answer every question in about 30 seconds without notes.

---

## 1. OSI / TCP-IP layers

**Q: Which layer does each of our tools work at?**

| Layer | What | Our tools |
|-------|------|-----------|
| 2 Data link | frames, MACs, VLANs | Open vSwitch, 802.1Q tags, the controller's forwarding, VRRP's virtual MAC |
| 3 Network | IP, routing | FRR (OSPF), static routes, iptables NAT/firewall, IPsec ESP |
| 4 Transport | TCP/UDP ports | firewall rules on ports (22, 53, 514), the port-sweep detection |
| 7 Application | services | DHCP, DNS (dnsmasq), SSH, syslog, HTTP, IKE (strongSwan), Prometheus |
| across layers | | **OpenFlow** matches fields from L2 to L4 in one rule; **Suricata** inspects L3-L7 |

Example: one OpenFlow rule matches `in_port` (L1), `eth_src` (L2) and pushes a VLAN tag (L2):
[fortress_ctrl.py:197](../controller/fortress_ctrl.py#L197).

## 2. Subnetting

**Q: Why /24 for zones and /30 for router links?**

A /24 has 256 addresses, 254 usable: plenty for a zone, and it lines up with the third octet
(192.168.**10**.x = VLAN **10**), which makes the plan easy to read. A /30 has 4 addresses: network,
two hosts, broadcast. A point-to-point cable has exactly two ends, so a /30 wastes nothing. The
five router links are 10.0.0.0/30, .4/30, .8/30, .12/30, .16/30.
Loopbacks are /32: one address that belongs to the router itself.

- Zones: [fortress.yaml:40](../config/fortress.yaml#L40) onwards; router links:
  [fortress.yaml:145-149](../config/fortress.yaml#L145); how the two ends get .1 and .2 of each /30:
  [labconfig.py:101-108](../topology/labconfig.py#L101).

## 3. VLANs, 802.1Q

**Q: How does tagging keep zones apart? What is a trunk?**

802.1Q inserts a 4-byte tag with a 12-bit VLAN ID into the Ethernet frame. Switches only forward
a frame to ports in the same VLAN, so each VLAN is its own broadcast domain: an ARP from Barracks
never reaches Intelligence, even on the same switch. A **trunk** carries frames of many VLANs at
once, tagged, so the receiver knows which VLAN each belongs to. Our routers' links to the cores
are trunks, with one sub-interface per VLAN (`r1-eth0.10`).

Under SDN we do the tagging in OpenFlow (OVS port tags only work without a controller, D10):
- **Push** the tag where a host plugs in: [fortress_ctrl.py:197-199](../controller/fortress_ctrl.py#L197)
- **Pop** it on the way out to a host: [fortress_ctrl.py:325](../controller/fortress_ctrl.py#L325)
- Trunks accept only tagged frames: [fortress_ctrl.py:206-209](../controller/fortress_ctrl.py#L206)
- Tested: two VLANs on one switch can't ARP each other: [checks.py:75](../topology/checks.py#L75)

## 4. SDN and OpenFlow

**Q: How is SDN different from traditional networking? What is a flow table?**

Traditional: every switch runs its own control logic (MAC learning, spanning tree) and decides
alone. SDN separates the **control plane** (decisions) from the **data plane** (forwarding): one
controller with a view of the whole network programs every switch. A **flow table** is a list of
rules: *match* (fields like port, MAC, VLAN, IP) → *actions* (output, push tag, drop, go to
another table), with a priority; the highest-priority match wins. A packet that matches nothing
goes to the controller (*packet-in*).

Our four tables, in order: ingress → policy → QoS → forward:
[pipeline.py:21](../controller/pipeline.py#L21), priorities [pipeline.py:25-33](../controller/pipeline.py#L25).
Packet-in handler: [fortress_ctrl.py:418](../controller/fortress_ctrl.py#L418).

## 5. Ryu controller

**Q: How does our controller detect a failure and reroute?**

1. The switch sends a **PortStatus** message when a port goes down:
   [fortress_ctrl.py:220-230](../controller/fortress_ctrl.py#L220).
2. `cable_gone` works out whether it was the last cable to that neighbour (a real link failure)
   or one of two parallel c1-c2 cables (only the aggregation group loses a member):
   [fortress_ctrl.py:233-240](../controller/fortress_ctrl.py#L233).
3. `reroute` removes the link from the graph, recomputes a shortest-path tree for every
   destination and reinstalls only the rules that changed, logging what moved to
   `reroutes.jsonl`: [fortress_ctrl.py:289](../controller/fortress_ctrl.py#L289).
4. The trees come from Dijkstra in [paths.py:24](../controller/paths.py#L24).

That is **reactive** mode (~20-30 ms measured). **Proactive** mode installs fast-failover groups in
advance, so the switch switches to the backup port itself:
[fortress_ctrl.py:349-375](../controller/fortress_ctrl.py#L349); backup next hop:
[paths.py:45](../controller/paths.py#L45).

## 6. OSPF

**Q: How does it pick the best path? What are cost and area 0?**

OSPF is a link-state protocol: every router floods a description of its links (LSAs), so all
routers build the same map and each runs Dijkstra's shortest-path-first to find the lowest total
**cost** to every network. Cost is a number per interface (lower = better), usually derived from
bandwidth; we set it by hand so r1-r3 (cost 10) is the primary WAN and r2-r3 (cost 100) the
backup. **Area 0** is the backbone area; all other areas must connect to it. A small network
like ours uses only area 0. Hellos every 1 s, neighbour dead after 4 s (default 10/40).

- Costs: [fortress.yaml:146-147](../config/fortress.yaml#L146); applied:
  [frr.conf.j2:24](../configs/frr/frr.conf.j2#L24); timers: [frr.conf.j2:25-26](../configs/frr/frr.conf.j2#L25);
  router ID: [frr.conf.j2:49](../configs/frr/frr.conf.j2#L49).
- Floating static backup (distance 250, used only if OSPF has nothing):
  [frr.conf.j2:59](../configs/frr/frr.conf.j2#L59).
- Live: `r1 vtysh -N r1 -c "show ip ospf neighbor"`, `... "show ip route ospf"`.

## 7. VRRP vs HSRP

**Q: How do they differ? What are priority and preemption?**

Both let two routers share one gateway address, so hosts keep the same default gateway if a
router dies. **HSRP** is Cisco-proprietary (active/standby, virtual MAC 0000.0c07.acXX). **VRRP**
is an open standard (RFC 5798; Master/Backup, virtual MAC 00:00:5e:00:01:XX); in VRRP the virtual
IP can even be a real router's address. The **priority** decides who is Master (higher wins: r1
120, r2 100). **Preemption** means a higher-priority router takes Master back when it returns.
The Master sends adverts (every 200 ms for us); if the Backup misses about three, it takes over
(~0.7 s measured).

- [frr.conf.j2:37-43](../configs/frr/frr.conf.j2#L37); priorities:
  [fortress.yaml:21](../config/fortress.yaml#L21); advert interval:
  [render_configs.py:28](../topology/render_configs.py#L28).
- Tested: r1 dies → r2 Master in 0.7 s, 0 gateway pings lost → r1 back (preemption):
  [checks.py](../topology/checks.py) `check_vrrp_failover`.
- Why hosts lose **nothing** even though VRRP takes 0.7 s: r2's VRRP interface already owns the
  virtual MAC and forwards as soon as frames reach it (D39).

## 8. LACP / EtherChannel

**Q: What happens when one link in a bond fails?**

A bond (EtherChannel/LAG) makes several cables act as one link: more bandwidth, and if one cable
fails the others carry on. Each *flow* is hashed onto one cable (so packets of one flow stay in
order); when a cable fails, its flows are rehashed onto the remaining ones. LACP is the protocol
that negotiates which cables belong to the bond.

Ours: an OVS LACP bond doesn't work under an OpenFlow controller (D24), so the controller builds
the same thing with an OpenFlow **select group**, one bucket per c1-c2 cable. The switch hashes
flows onto buckets and skips a bucket whose port is down:
[fortress_ctrl.py:256-280](../controller/fortress_ctrl.py#L256). Measured: one cable down = 0
pings lost, ~19 ms.

## 9. STP

**Q: Why do loops break networks, and how does our controller avoid them?**

Ethernet frames have no TTL, and broadcasts are flooded out every port. With a loop, a single
broadcast circles forever and multiplies (a **broadcast storm**), and switches' MAC tables flap.
STP prevents this by electing a root bridge and blocking redundant ports, so the active topology
is a tree.

We have loops on purpose (every access switch connects to both cores). Before the controller we
used RSTP (D11). Under the controller: forwarding follows a computed shortest-path **tree** per
destination ([paths.py:24](../controller/paths.py#L24)), and broadcasts are flooded **by the
controller** only to the VLAN's edge ports, never back into the core:
[fortress_ctrl.py:394-416](../controller/fortress_ctrl.py#L394). Tested: "no broadcast storm".

## 10. ACLs / firewall rules

**Q: Where are our rules applied, and why there?**

Three places, each where it fits best:
1. **At the access port** (controller, table 0): port security (only the host's own MAC) and
   DHCP guard (only srv1 may answer DHCP): [fortress_ctrl.py:194-205](../controller/fortress_ctrl.py#L194).
   Earliest possible point: bad traffic never enters the network. Blocks go here too.
2. **In the switches' policy table**: zone pairs that may *never* talk are dropped before they
   reach a router: [fortress_ctrl.py:146-158](../controller/fortress_ctrl.py#L146).
3. **On r1/r2** (iptables, stateful): which zone may *start* a connection to which. The router
   sees both directions, so conntrack can let replies back in:
   [rules.j2:23-28](../configs/iptables/rules.j2#L23). The policy comes from `reach:` in
   [fortress.yaml:40](../config/fortress.yaml#L40).

Why split (D27): a stateless switch rule can't tell a reply from a new connection; the router
can. Dropping never-talk pairs in the switch saves the router the work.

## 11. IPsec

**Q: What do IKE phase 1 and 2 do? ESP vs AH? Tunnel vs transport mode?**

- **IKE phase 1** (IKE_SA_INIT + IKE_AUTH in IKEv2): the two peers agree on algorithms, do a
  Diffie-Hellman exchange to get a shared secret, and authenticate each other (we use a
  pre-shared key). Result: a secure control channel (the IKE SA).
- **Phase 2** (CHILD_SA): over that channel, they agree the keys and which traffic to protect
  (traffic selectors: 192.168.0.0/16 + 10.10.60.0/24 ↔ 172.16.70.0/24). Result: the IPsec SAs
  that actually encrypt data.
- **ESP** encrypts and authenticates the payload; **AH** only authenticates (no secrecy) and breaks
  with NAT. We use ESP with AES-256-GCM.
- **Tunnel mode** encrypts the whole original packet and adds a new outer IP header (site-to-site,
  our case); **transport mode** encrypts only the payload and keeps the original header
  (host-to-host).

Code: IKE proposal [render_configs.py:154](../topology/render_configs.py#L154), DPD keepalive
[render_configs.py:155](../topology/render_configs.py#L155), traffic selectors and tunnel mode
[render_configs.py:168-170](../topology/render_configs.py#L168), loopback endpoints
[render_configs.py:153](../topology/render_configs.py#L153). Proof on the wire: the demo's capture
shows only ESP between 1.1.1.1 and 3.3.3.3.

## 12. AAA (theory only)

**Q: Authentication vs authorization vs accounting? How would RADIUS fit into our design?**

**Authentication** = who are you (password, key, certificate). **Authorization** = what may you
do (which commands, which VLAN). **Accounting** = record what you did (logins, commands, time).
RADIUS (UDP, encrypts only the password) and TACACS+ (TCP, encrypts everything, separates the
three A's) are the usual servers.

In our design: today SSH uses keys (authentication), only from the Management VLAN
(authorization by network), and every login, good or failed, goes to srv2's central log
(accounting): [sshd_config.j2](../configs/ssh/sshd_config.j2), [rules.j2:34](../configs/iptables/rules.j2#L34).
With RADIUS we would put a FreeRADIUS server on the Servers VLAN and (1) point the routers' and
servers' SSH logins at it through PAM, and (2) use **802.1X** at the access ports, so the switch
only opens a port after RADIUS accepts the device, and RADIUS could even assign its VLAN. That
would replace our fixed MAC-per-port check. (RADIUS was out of scope.)

## 13. DHCP, DNS, NAT

**Q: Explain DORA. Why NAT? What does a DHCP relay do?**

- **DORA**: the client broadcasts *Discover*; the server offers an address (*Offer*); the client
  broadcasts *Request* for it; the server confirms (*Ack*) with the lease, gateway and DNS server.
  Ours: [dnsmasq.conf.j2:37-40](../configs/dnsmasq/dnsmasq.conf.j2#L37) (range, gateway = the VRRP VIP,
  DNS, domain); fixed address per MAC: [dnsmasq.conf.j2:46](../configs/dnsmasq/dnsmasq.conf.j2#L46).
- **DHCP relay**: Discover is a broadcast and broadcasts don't cross routers, so the relay on r1/r2
  receives it in each zone and forwards it as unicast to srv1, filling in which subnet it came
  from: [services.py:107-117](../topology/services.py#L107).
- **DNS** turns names (`srv1.fortress.mil`) into addresses: [dnsmasq.conf.j2:22-27](../configs/dnsmasq/dnsmasq.conf.j2#L22).
- **NAT**: private addresses (192.168.x.x) aren't routable on the internet and we don't want the
  internet to see our internal plan. r1/r2 rewrite the source to their own ISP-side address
  (PAT/masquerade): [rules.j2:18](../configs/iptables/rules.j2#L18). Lab-internal destinations are
  exempt, so a packet that briefly takes the default route is never translated:
  [rules.j2:14](../configs/iptables/rules.j2#L14).

## 14. IDS

**Q: Signature-based (Suricata) vs anomaly-based (our model)?**

**Signature-based** matches known patterns ("many SYNs to different ports from one host"):
precise, explainable, few false positives, but blind to anything new. **Anomaly-based** learns
what normal looks like and flags what is different: can catch the unknown, but produces false
positives and is harder to explain. We use both and **require them (or other sources) to agree**:
one alone can reach at most 30 of the 70 points needed for a breach.

- Our signatures: [local.rules:10](../detection/suricata/local.rules#L10) (SYN sweep),
  [local.rules:26](../detection/suricata/local.rules#L26) (SSH brute force).
- Suricata sees traffic via mirror ports on both cores:
  [fortress_topo.py:205](../topology/fortress_topo.py#L205).
- Scoring: [threat_engine.py:72-133](../detection/threat_engine.py#L72); bands:
  [threat_engine.py:60](../detection/threat_engine.py#L60).

## 15. Isolation Forest

**Q: How does it find anomalies without labelled data? How do we handle false positives?**

It builds many random trees: each splits the data on a random feature at a random value, again
and again, until every point is alone. An **anomaly is easy to isolate**: it takes few splits
(short path), because it is far from the crowd. Normal points sit in dense regions and need many
splits. The average path length over 200 trees becomes the anomaly score. No labels needed: we
train only on normal traffic.

- Training: [train_model.py:38-60](../detection/train_model.py#L38) (200 trees, contamination 2%);
  scoring: [anomaly.py:32](../detection/anomaly.py#L32); features (6 per host per 5 s):
  [features.py:10-15](../detection/features.py#L10).
- **False positives**: measured 2.2% on unseen normal traffic (target < 5%). And by design one
  source can't cause a breach: the model contributes at most 30 points
  ([threat_engine.py:90-93](../detection/threat_engine.py#L90)), so a false positive alone stays
  "Normal".

## 16. Prometheus + Grafana

**Q: What is a time-series metric? How does scraping work? How do alert rules fire?**

A **time series** is a named number with labels, sampled over time, e.g.
`fortress_threat_score{host="bar3"} 92`. **Scraping**: Prometheus *pulls* — every 5 s it fetches
`http://127.0.0.1:9101/metrics` and stores each value with a timestamp
([prometheus.yml:7-13](../monitoring/prometheus.yml#L7)). Our exporter rebuilds every metric from
the live lab on each scrape ([exporter.py:52](../monitoring/exporter.py#L52)). An **alert rule**
is a query plus a condition and a duration: e.g. "min(fortress_link_up) < 1 for 30 s" →
the rule goes *pending*, then *firing*, and the contact point is notified
([alerting.yaml.j2:35-49](../monitoring/grafana/alerting.yaml.j2#L35)). Breach alerts come from
the threat engine itself, because only it knows the reasons (D36).

## 17. Ansible (if used)

**Q: What is a playbook? Why is infrastructure as code better than manual config?**

We did **not** use Ansible (D15). A playbook is a YAML list of tasks Ansible runs over SSH on many
machines to bring them to a desired state. Our equivalent idea: every config is generated from
templates in [`configs/`](../configs) by [render_configs.py](../topology/render_configs.py), from
one file, [fortress.yaml](../config/fortress.yaml). Why that is better than typing configs by
hand: repeatable (same input, same result), reviewable in Git, no typos between devices, and the
design and the running network can't drift apart. Ansible solves pushing configs to many
separate machines over SSH; all our nodes share one VM's filesystem, so rendering files was enough.

## 18. Cisco equivalents

**Q: What would each of our open-source parts be on Cisco gear?**

Full table: [cisco-equivalents.md](cisco-equivalents.md). The short version: OVS + Ryu ≈ Catalyst
switches managed by DNA Center/ACI; FRR ≈ IOS routing (same `router ospf`, `ip ospf cost`,
`vrrp` commands); iptables ≈ NAT overload + ACLs/zone firewall; strongSwan ≈ an IKEv2
site-to-site VPN with `tunnel source Loopback0`; Suricata ≈ Firepower/Snort; select group ≈
EtherChannel; fast-failover group ≈ Flex Links / fast reroute; OVS mirror ≈ SPAN.

---

## Questions about *our* project (expect these)

**Why Mininet, not Packet Tracer?** Packet Tracer simulates; Mininet runs the *real* Linux
software (the same OVS, FRR, strongSwan and Suricata used in production), so we can measure real
recovery times, run a real IDS on real packets and capture them in Wireshark.

**Why does a breach need two signals?** So one tool being wrong can't isolate an innocent
soldier. A lone Suricata alert is 30, the model alone at most 30; 70 needs at least two
independent sources to agree (D33): [threat_engine.py:72](../detection/threat_engine.py#L72).

**How is the attacker blocked, and only the attacker?** The engine calls the controller's REST API
([rest_api.py:42](../controller/rest_api.py#L42)), which finds the host's own access port and
installs a priority-500 drop rule there, with an expiry
([response.py:52-80](../controller/response.py#L52)). Neighbours on the same switch use other
ports, so they're unaffected (tested). Unblock: [response.py:82](../controller/response.py#L82).

**Why is the VPN between loopbacks?** It used to run between the two addresses on the r1-r3
cable; cut the cable and the tunnel died with it even though OSPF had a detour. A loopback isn't
tied to any cable, so the encrypted packets follow OSPF's detour through r2. Found by our chaos
test (D39): [frr.conf.j2:12](../configs/frr/frr.conf.j2#L12).

**What did testing find?** The chaos test's first numbers were too good: a measuring bug hid an
outage that never ended, and behind it the VPN didn't survive a cut WAN cable. Both fixed, plus
three more VPN faults ([testing.md §5](testing.md)). An examiner will like this: it shows we
tested rather than assumed.

**Is proactive faster than reactive?** Not in our lab: both recover in roughly 20-130 ms, and
across five full runs the faster mode changed from run to run for every failure. The controller
is local and quick here; fast-failover matters more with a remote or busy controller.

**Does everything pass?** 142/142 on a running lab; 140/142 straight after a reboot. The two
failures are the rogue-DHCP test's proof steps (the fake server doesn't answer in time, so the
drop can't be shown); the protected host still gets the right address. Known issue, D40.

**Why did you drop T22 (threat-aware rerouting)?** We built it and tested it: in this topology an
access switch is never a transit node, and routed traffic has to reach the VRRP Master's core, so
there is no alternative path to move a zone onto (D37).

**What are the known limits?** When r1 dies, FOB traffic pauses ~3-5 s (the tunnel lives on r1;
r3 must first detect r1 is dead). Containment takes seconds, not milliseconds (5 s windows).
Fix for the first: a second tunnel on r2.

**Where does every address come from?** One file, [fortress.yaml](../config/fortress.yaml);
nothing is hard-coded, and [check_config.py](../scripts/check_config.py) validates it.

---

## Live commands to have ready (Mininet CLI)

| Show | Command |
|------|---------|
| OSPF neighbours / routes | `r1 vtysh -N r1 -c "show ip ospf neighbor"` · `... "show ip route"` |
| VRRP state | `r1 vtysh -N r1 -c "show vrrp summary"` |
| Flow tables | (in the VM) `sudo ovs-ofctl -O OpenFlow13 dump-flows s1` · `dump-groups c1` |
| Path | `cmd1 traceroute -n 172.16.70.10` |
| Power-fail a router | `fail r1` … `recover r1` |
| Cut a cable | `link c1 s1 down` … `link c1 s1 up` |
| Controller | (in the VM) `curl -s localhost:8080/fortress/status` · `/fortress/blocked` |
| Threat scores | (in the VM) `cat /var/log/fortress/threat/state.json` |
| Admin SSH | `admin1 ssh -i /etc/fortress/ssh/admin_key ubuntu@192.168.99.2` |
