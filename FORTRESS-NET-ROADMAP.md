# Fortress-Net: Plan, Roadmap and Tasks

**Project:** CIE3 Computer Networks mini project (15 marks, team project, evaluated by viva)
**What we build:** a self-healing, AI-assisted secure network for a military base, built in **Mininet** with real Linux networking software
**Platform:** one Ubuntu 22.04 VM per member, created with **Multipass** (works on Apple Silicon Macs, Windows and Linux): 8 GB RAM recommended, 4 GB minimum, 2+ cores, 40 GB disk
**Last updated:** 2026-11-06

> The timeline below assumes about **4 weeks**. If the CIE3 date is earlier or later, keep the phase order and squeeze or stretch the weeks.

---

## 1. The plan in one page

### Goal
Show a network that:
1. **Survives failures by itself.** Cut a link or kill a router and traffic keeps flowing, with measured recovery times.
2. **Catches attacks on real traffic.** Suricata plus our own ML model spot port scans, brute-force logins and rogue devices.
3. **Responds without a human.** It blocks the attacker at its switch port, reroutes around the threat and sends Telegram alerts.

### The three threat scenarios (the whole demo is built around these)
| # | Scenario | What must happen |
|---|----------|------------------|
| 1 | **Link sabotage**: a core–access cable is cut | SDN controller reroutes; ping continues |
| 2 | **Equipment failure**: gateway router r1 dies | VRRP moves the gateway to r2 within seconds |
| 3 | **Insider attack**: a Barracks host scans ports or brute-forces SSH | AI + Suricata score it ≥ 70; host is isolated automatically |

### Architecture (6 layers)
| Layer | Software | Role |
|-------|----------|------|
| 1. Virtual network | Mininet + Open vSwitch | Hosts, switches, links |
| 2. SDN controller | Ryu (Python) | Topology discovery, shortest paths, rerouting, zone isolation, blocking |
| 3. Routing and WAN | FRRouting in Mininet hosts | OSPF, VRRP, backup static route, NAT, links to FOB and ISP |
| 4. Services | dnsmasq, rsyslog, strongSwan (chrony optional) | DHCP/DNS, central logs, IPsec VPN (time sync optional) |
| 5. Detection | Suricata + Python ML (Isolation Forest) | Signature and anomaly detection |
| 6. Command layer | Prometheus + Grafana, Telegram | Dashboards, alerts, phone alerts |

**Data flow:** traffic and login events → mirrored to Suricata and the flow collector → threat engine → Grafana and Telegram → block rule sent to Ryu → attacker cut off at its port.

### Topology and IP plan
- **Core:** c1, c2 (OVS, Ryu-controlled), joined by an LACP bond
- **Access:** s1–s5, one per zone plus one for the servers, each uplinked to both cores
- **Routers:** r1, r2 (zone gateways, VRRP, OSPF, NAT, VPN), r3 (FOB), r4 (ISP)
- **Servers:** srv1 DHCP/DNS, srv2 Syslog (+ optional NTP), srv3 web/file server (target for normal traffic); ids1 Suricata on a mirror port
- **Zone hosts:** 2–3 per zone (users and an attacker)

| Zone | VLAN | Subnet | VRRP VIP | r1 / r2 | Allowed to reach |
|------|------|--------|----------|---------|------------------|
| Command Center | 10 | 192.168.10.0/24 | .1 | .2 / .3 | All zones |
| Intelligence | 20 | 192.168.20.0/24 | .1 | .2 / .3 | Command only |
| Communications | 30 | 192.168.30.0/24 | .1 | .2 / .3 | Command, servers, FOB |
| Barracks | 40 | 192.168.40.0/24 | .1 | .2 / .3 | Servers and internet only |
| Servers | 60 | 10.10.60.0/24 | .1 | .2 / .3 | All zones |
| Management | 99 | 192.168.99.0/24 | .1 | .2 / .3 | Admin SSH only |
| FOB | 70 | 172.16.70.0/24 | .1 (r3) | — | Over IPsec VPN |

Router-to-router links use /30 subnets from 10.0.0.0/24: r1–r2, r1–r3 (primary WAN), r2–r3 (backup WAN), r1–r4, r2–r4.

---

## 2. Team workstreams

| Workstream | Covers | Phases | Owner |
|------------|--------|--------|-------|
| **A. Network and routing** | Topology script, VLANs, FRR (OSPF, VRRP, NAT), DHCP/DNS, Ansible (optional) | 1, 2 | _____ |
| **B. SDN controller** | Ryu app: discovery, rerouting, isolation rules, block API | 3, parts of 4 and 7 | _____ |
| **C. Security** | VPN, SSH, central logging, zone firewall rules | 4 | _____ |
| **D. AI and monitoring** | Suricata, flow collector, ML model, threat engine, Prometheus, Grafana, Telegram | 6, 7 | _____ |

**Everyone:** Phase 0 setup, failover measurements, report, demo rehearsal and all viva topics (Section 7).

Workstreams B, C and D can start their standalone pieces (controller logic on Mininet's sample topology, VPN and logging configs, model training on a sample capture) while A builds the real topology.

---

## 3. Repository layout

```
fortress-net/
├── README.md                  # how to run everything
├── topology/
│   └── fortress_topo.py       # Mininet topology: zones, cores, routers, servers
├── controller/
│   ├── fortress_ctrl.py       # Ryu app: discovery, shortest path, reroute, isolation
│   └── block_api.py           # REST endpoint: block / unblock a host
├── configs/
│   ├── frr/{r1,r2,r3,r4}/     # frr.conf, daemons
│   ├── strongswan/            # ipsec.conf / swanctl.conf for r1 and r3
│   ├── dnsmasq/               # DHCP scopes per VLAN, DNS records
│   ├── rsyslog/ chrony/       # chrony optional
│   └── iptables/              # NAT and zone rules on r1/r2
├── ansible/                   # optional
│   ├── inventory.ini
│   └── playbooks/             # deploy_frr.yml, deploy_services.yml, ...
├── detection/
│   ├── suricata/              # suricata.yaml, local.rules
│   ├── flow_collector.py      # polls Ryu flow stats every 5 s
│   ├── baseline_traffic.py    # generates normal traffic
│   ├── train_model.py         # Isolation Forest
│   └── threat_engine.py       # combines signals → score → response
├── monitoring/
│   ├── prometheus.yml
│   ├── exporter.py            # link status, zone traffic, threat scores
│   └── grafana/dashboards/    # exported dashboard JSON
├── scripts/
│   ├── start_all.sh           # one command brings up the whole lab
│   ├── stop_all.sh
│   └── failover_test.sh       # runs each failure and records loss/recovery
└── docs/
    ├── evidence/              # screenshots, pcaps, measurement tables
    └── report/
```

---

## 4. Roadmap by week

| Week | Phases | Milestone at end of week |
|------|--------|--------------------------|
| **Week 1** | 0 Setup, 1 Base network | Every member's VM runs the lab; every host gets an IP and pings its gateway |
| **Week 2** | 2 Routing, 3 SDN self-healing | Command pings the FOB and the "internet"; ping survives a cut core–access link |
| **Week 3** | 4 Security, 6 Detection | Barracks → Intelligence blocked; VPN up; a port scan scores ≥ 70 |
| **Week 4** | 7 Response and dashboard, 8 Evidence and report | Full breach flow works end to end; demo runs in under 10 minutes; report done |

**Rule:** never break the last working version. Tag a Git release at each milestone (`v0.1-base`, `v0.2-routing`, …) so there is always something to show.

---

## 5. Phases in detail, with tasks

Tick tasks off as they are done. The letter in brackets is the owning workstream.

### Phase 0: Setup (Week 1, days 1–2)
**Done when:** Mininet's sample topology runs with Ryu attached, on every member's machine.

- [ ] (All) Create the Ubuntu 22.04 VM with Multipass and share the project folder into it (see task T-1 in `CLAUDE-CODE-TASKS.md`); give it 8 GB RAM if possible
- [ ] (All) Install Mininet and Open vSwitch: `sudo apt install mininet openvswitch-switch`
- [ ] (All) Install FRRouting from the official FRR apt repo
- [ ] (B) Create a Python 3.9 virtual environment for Ryu (Ryu is unreliable on newer Python); fall back to `os-ken` if it fails
- [ ] (All) Test: `sudo mn --controller=remote,ip=127.0.0.1,port=6633` with `ryu-manager ryu.app.simple_switch_13`, then `pingall`
- [ ] (A) Create the GitHub repo with the layout in Section 3 and add everyone
- [ ] (All) Install tools: `suricata dnsmasq rsyslog strongswan nmap iperf3 tcpdump wireshark` (plus `chrony` if you do the optional time sync)
- [ ] (D) Install Prometheus and Grafana (Docker or native packages)
- [ ] (All) Python packages: `scikit-learn pandas networkx scapy flask requests prometheus-client`
- [ ] (All) Agree on a weekly check-in time and fill in the Owner column in Section 2
- [ ] (All) Take a VM snapshot called `clean-setup`

### Phase 1: Base network (Week 1, days 3–7)
**Done when:** every host gets an IP from DHCP and pings its VRRP gateway.

- [ ] (A) Write `fortress_topo.py`: cores c1, c2; access switches s1–s5; dual uplinks from each access switch to both cores
- [ ] (A) Add routers r1–r4 as Mininet hosts with IP forwarding on (`sysctl net.ipv4.ip_forward=1`)
- [ ] (A) Add servers srv1–srv3, ids1 and 2–3 hosts per zone with the names used in the IP plan
- [ ] (A) Tag access ports per VLAN on OVS (`ovs-vsctl set port <port> tag=<vlan>`); trunk the uplinks
- [ ] (A) Create VLAN sub-interfaces on r1 and r2 (e.g. `r1-eth0.10`) with their .2 / .3 addresses
- [ ] (A) Configure dnsmasq on srv1 with one DHCP scope per VLAN (gateway option = VIP .1) and DNS records for all servers
- [ ] (A) Point r1 and r2 at srv1 as a DHCP relay for each VLAN
- [ ] (A) *Optional:* decide how Ansible reaches the lab: either run `sshd` in each router and server host on the Management VLAN, or have Ansible render config templates that a push script copies in. Write the inventory.
- [ ] (A) *Optional:* Ansible playbook that deploys dnsmasq config
- [ ] (A) Test: every host has an IP in the right subnet, pings its gateway and resolves `srv1.fortress.mil`
- [ ] (A) Draw the topology diagram (draw.io or similar) for the report and demo
- [ ] (All) Git tag `v0.1-base`

### Phase 2: Routing (Week 2, days 1–3)
**Done when:** Command Center pings the FOB and the simulated internet.

- [ ] (A) Run FRR daemons (zebra, ospfd, vrrpd, staticd) inside each router host, each with its own config and runtime directory (or use IPMininet if this becomes painful)
- [ ] (A) OSPF area 0 on r1–r4; advertise all zone subnets and the /30 links
- [ ] (A) Set OSPF costs so r1–r3 is the primary WAN and r2–r3 is the backup
- [ ] (A) Floating static route to the FOB on r2 (higher administrative distance) as an extra backup
- [ ] (A) VRRP on r1 (priority 120) and r2 (priority 100) for every zone VIP, with preemption on. Note: FRR's VRRP needs a macvlan interface per virtual router, so create those in the topology script.
- [ ] (A) NAT (iptables MASQUERADE) on r1/r2 towards r4 for internet access
- [ ] (A) *Optional:* Ansible playbook `deploy_frr.yml` for all router configs
- [ ] (A) Test: `vtysh -c "show ip ospf neighbor"` shows full adjacencies; `show vrrp` shows r1 as master
- [ ] (A) Test: Command host pings r3 (FOB) and r4 (ISP); traceroute shows the primary path
- [ ] (All) Git tag `v0.2-routing`

### Phase 3: SDN self-healing (Week 2, days 3–7)
**Done when:** a continuous ping survives a core–access link failure.

- [ ] (B) Ryu app: topology discovery with LLDP (`--observe-links`), stored as a networkx graph
- [ ] (B) Learn host locations (MAC → switch, port) from packet-in events
- [ ] (B) Compute shortest paths with networkx and install flow rules hop by hop
- [ ] (B) Handle `EventOFPPortStatus` / link-delete events: remove the broken link from the graph, delete the affected flows, reinstall them on the new path
- [ ] (B) Avoid broadcast loops: flood only along a spanning tree of the graph
- [ ] (A) LACP bond between c1 and c2 (`ovs-vsctl add-bond c1 bond0 <port1> <port2> lacp=active`, same on c2)
- [ ] (B) Log every reroute with a timestamp (feeds the dashboard and the evidence)
- [ ] (B) Test: `ping -i 0.1` from Command to the servers, then `link c1 s1 down` in the Mininet CLI. Pings resume.
- [ ] (B) Test: bring the link back up and confirm traffic returns to the shortest path
- [ ] (All) Git tag `v0.3-selfheal`

### Phase 4: Security (Week 3, days 1–3)
**Done when:** Barracks cannot reach Intelligence, and the VPN to the FOB is up.

- [ ] (B) Zone isolation as Ryu flow rules, following the "Allowed to reach" column in Section 1
- [ ] (A) Matching iptables rules on r1/r2 for traffic that crosses the routers (defense in depth)
- [ ] (B) Rogue device control: unknown MACs get no forwarding rules until listed in an allowlist file
- [ ] (B) Rogue DHCP protection: only srv1's port may send DHCP offers (match UDP 67)
- [ ] (C) strongSwan IPsec tunnel between r1 and r3 for 192.168.0.0/16 ↔ 172.16.70.0/24
- [ ] (C) SSH only (no Telnet), management access only from VLAN 99
- [ ] (C) rsyslog: all routers, servers and the controller send logs to srv2
- [ ] (C) *Optional:* chrony on srv2 as the NTP server
- [ ] (All) Test: Barracks → Intelligence ping fails, Command → Intelligence succeeds
- [ ] (C) Test: `ipsec status` / `swanctl --list-sas` shows the tunnel up; tcpdump on the WAN link shows only ESP packets
- [ ] (C) Test: admin SSH into r1 works from VLAN 99; a wrong password is logged on srv2
- [ ] (All) Git tag `v0.4-security`

### Phase 5: removed
IoT (armory sensors, MQTT) is out of scope for now. Phase numbers are kept so references stay valid.

### Phase 6: Detection (Week 3, days 3–7)
**Done when:** a port scan scores ≥ 70 and normal traffic stays below 40.

- [ ] (D) Port mirroring on the core switches to ids1 (OVS `Mirror` with `select-all=true`, output to the ids1 port)
- [ ] (D) Suricata on ids1 with the ET Open ruleset plus our `local.rules` for scans and SSH brute force; output to `eve.json`
- [ ] (D) `flow_collector.py`: poll Ryu flow stats every 5 s and compute per host: packets/s, bytes/s, new flows/s, distinct destination ports
- [ ] (D) `baseline_traffic.py`: generate normal ping, HTTP, DNS and file transfers across zones for at least 30 minutes
- [ ] (D) Record the baseline features to CSV
- [ ] (D) `train_model.py`: Isolation Forest (scikit-learn) on the baseline; save the model with joblib
- [ ] (D) `threat_engine.py`: score = anomaly score + rules (Suricata alert +30, >50 ports probed +30, repeated failed SSH logins +25, scan followed by login attempts from the same host +15), capped at 100
- [ ] (D) Levels: 0–39 Normal (log), 40–69 Suspicious (dashboard warning), 70–100 Breach (alert + automatic response)
- [ ] (D) Every score keeps its list of reasons, so each alert can be explained
- [ ] (D) Test: `nmap -sS` from a Barracks host against Intelligence → score ≥ 70
- [ ] (D) Test: `hydra` or a scripted SSH brute force against r1 → score ≥ 70
- [ ] (D) Test: an hour of baseline traffic → no false Breach alerts. Tune the thresholds if needed.
- [ ] (All) **Lab rule:** attack tools run only inside our Mininet lab, never on college or public networks
- [ ] (All) Git tag `v0.6-detection`

### Phase 7: Response and dashboard (Week 4, days 1–3)
**Done when:** the full breach flow works end to end.

- [ ] (B) `block_api.py`: Ryu REST endpoint `POST /block {ip}` installs a high-priority drop rule at the host's access port; `POST /unblock` removes it
- [ ] (D) Threat engine calls the block API on Breach
- [ ] (D) `exporter.py`: Prometheus metrics for link status, traffic per zone, flow counts, current threat score per host, blocked hosts, reroute count
- [ ] (D) Prometheus scrapes the exporter and node metrics
- [ ] (D) Grafana "Command Center" dashboard: topology health, zone traffic, threat scores, incident table
- [ ] (D) Telegram bot (via BotFather) as a Grafana contact point; alert rule on threat score ≥ 70 and on link down
- [ ] (D) Incident report: each Breach writes a JSON/markdown record with timeline, evidence and actions
- [ ] (All) `start_all.sh` brings up the topology, controller, FRR, services, Suricata, monitoring and the threat engine with one command
- [ ] (All) Test the flow in Section 6 end to end three times in a row
- [ ] (All) Git tag `v0.7-response`

### Phase 8: Evidence, report and demo (Week 4, days 3–7)
**Done when:** the demo runs in under 10 minutes and the report is submitted.

- [ ] (All) `failover_test.sh`: for each failure below, run a 10 pings/s stream and an iperf3 stream, trigger the failure, and record packets lost and recovery time
- [ ] (All) Fill in the failover table (Section 6) with measured numbers, best of 3 runs
- [ ] (All) Capture Wireshark/tcpdump evidence: OSPF hellos, VRRP adverts, ESP on the WAN, the port scan, LLDP
- [ ] (All) Screenshots of Grafana, Telegram alerts, the incident report and the topology
- [ ] (All) Write the report: problem, design, implementation per layer, results, limitations, future work
- [ ] (All) Rehearse the demo script (Section 6) at least twice; time it
- [ ] (All) Record a full successful run as a backup video
- [ ] (All) VM snapshot called `demo-ready`
- [ ] (All) Viva practice: each member explains every topic in Section 7
- [ ] (All) Git tag `v1.0-final`

---

## 6. Demo script and evidence

### Demo (about 9 minutes)
| Time | Step | What the examiner sees |
|------|------|------------------------|
| 0:00 | Introduce the base, zones and architecture | Topology diagram; Grafana all green |
| 1:30 | Start continuous ping + iperf3 from Command to the FOB | Steady traffic graph |
| 2:00 | **Link sabotage:** `link c1 s1 down` | Controller reroutes; ping continues; reroute logged |
| 3:00 | **Equipment failure:** stop r1 | VRRP moves the gateway to r2 |
| 4:00 | Take the primary WAN down | OSPF / backup route keeps the FOB reachable |
| 5:00 | **Insider:** Barracks host runs nmap | Suricata + AI flag it; score passes 70 |
| 6:00 | Automatic response | Telegram alert on phone; host isolated; its pings stop |
| 7:00 | Wireshark capture and VPN-encrypted traffic | Real packets as evidence |
| 8:00 | Incident report and failover table | Timeline and recovery numbers |

**Before the demo:** restore the `demo-ready` snapshot, run `start_all.sh`, check the phone is online, have the backup video ready.

### Failover measurement table (fill in during Phase 8)
| Failure | Mechanism | Packets lost | Recovery time |
|---------|-----------|--------------|---------------|
| Core–access link down | SDN rerouting | | |
| One bond member down | LACP | | |
| r1 stopped | VRRP | | |
| Primary WAN down | OSPF / backup route | | |

### Breach response flow (must work end to end)
1. **Attack starts:** a Barracks host scans ports on the Intelligence servers.
2. **Seen:** Suricata raises a scan alert, and Ryu flow stats show one source hitting many ports.
3. **Scored:** the threat engine combines both signals with the anomaly score; the score passes 70.
4. **Alerted:** Grafana turns red and Telegram messages the team with host, zone, score and reasons.
5. **Blocked:** the engine calls the Ryu block API, which installs a drop rule at the host's access port.
6. **Recorded:** an incident report with timeline, evidence and actions is saved.

---

## 7. Viva topics everyone must know

| Topic | Likely question |
|-------|-----------------|
| OSI / TCP-IP layers | Which layer does each of our tools work at? |
| Subnetting | Why /24 for zones and /30 for router links? |
| VLANs, 802.1Q | How does tagging keep zones apart? What is a trunk? |
| SDN and OpenFlow | How is SDN different from traditional networking? What is a flow table? |
| Ryu controller | How does our controller detect a failure and reroute? |
| OSPF | How does it pick the best path? What are cost and area 0? |
| VRRP vs HSRP | How do they differ? What are priority and preemption? |
| LACP / EtherChannel | What happens when one link in a bond fails? |
| STP | Why do loops break networks, and how does our controller avoid them? |
| ACLs / firewall rules | Where are our rules applied, and why there? |
| IPsec | What do IKE phase 1 and 2 do? ESP vs AH? Tunnel vs transport mode? |
| AAA (theory only) | Authentication vs authorization vs accounting? How would RADIUS fit into our design? |
| DHCP, DNS, NAT | Explain DORA. Why NAT? What does a DHCP relay do? |
| IDS | Signature-based (Suricata) vs anomaly-based (our model)? |
| Isolation Forest | How does it find anomalies without labelled data? How do we handle false positives? |
| Prometheus + Grafana | What is a time-series metric? How does scraping work? How do alert rules fire? |
| Ansible (if used) | What is a playbook? Why is infrastructure as code better than manual config? |
| Cisco equivalents | What would each of our open-source parts be on Cisco gear? |

---

## 8. Risks and fallbacks

| Risk | Fallback |
|------|----------|
| Too much to finish in time | Core first: Phases 1–3 plus detection of one attack. The VPN, Ansible, chrony and threat-aware rerouting are optional. |
| Ryu install or Python version problems | Use os-ken, or Mininet's built-in POX controller |
| FRR inside Mininet is hard to configure | Use the IPMininet library, which sets up routers and OSPF for us |
| VRRP macvlan setup fails | Use keepalived for VRRP on r1/r2 instead of FRR's vrrpd |
| Ansible can't reach Mininet hosts | Use Ansible only to render config templates and push them with a script |
| Laptop too slow | Reduce to 3 zones and fewer hosts; run Grafana only during the demo |
| ML model gives many false alarms | Lean on the rule-based score + Suricata; explain the tuning in the viva |
| Examiner expects Cisco commands | Know the Cisco equivalents and explain how ours differ |
| Something breaks on demo day | VM snapshot + backup video of a full successful run |

---

## 9. Definition of done (whole project)

- [ ] All three threat scenarios work live, three runs in a row
- [ ] Failover table filled in with real measured numbers
- [ ] Breach flow (Section 6) works end to end with a Telegram alert
- [ ] `start_all.sh` brings up the full lab from a clean boot
- [ ] All code and configs in the GitHub repo, with a README that explains how to run it
- [ ] Report written with diagrams, screenshots and evidence
- [ ] Every member can answer every viva topic in Section 7
- [ ] `demo-ready` snapshot and backup video exist
