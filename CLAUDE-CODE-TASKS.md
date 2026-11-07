# Fortress-Net: Task List for Claude Code

This file is the work queue for Claude Code. The full design is in `FORTRESS-NET-ROADMAP.md`. Read that first.

---

## How to use this file

1. Give Claude Code **one task at a time**, in order, e.g.:
   > Read CLAUDE.md, FORTRESS-NET-ROADMAP.md and CLAUDE-CODE-TASKS.md. Do task T03. Stop when its acceptance checks pass, then tick it off here.
2. After each task, **you** run the acceptance check in the Ubuntu VM. Don't move on until it passes.
3. Commit after every task: `git commit -m "T03: ..."`.
4. Tasks marked ⭐ are the **signature features**: the parts that make Fortress-Net different from a standard student project. Do not skip them.

**Important:** Mininet, Open vSwitch, FRR and Suricata only run on Linux. Task **T-1** creates an Ubuntu VM with Multipass and shares this folder into it. Claude Code stays on the Mac, edits files here, and runs every install and test inside the VM through `scripts/vm/vm.sh`. **Do T-1 before any other task.**

---

## Part A: Foundation

### T-1: Create the lab VM (do this before anything else)
The Mac is an **Apple Silicon (arm64) MacBook**. VirtualBox works poorly there, so use **Multipass** (Canonical's Ubuntu VM tool). It runs on Macs, Windows and Linux, so teammates can use the same setup. The project folder is shared into the VM, so Claude Code keeps editing files on the Mac and runs everything inside the VM with `multipass exec`.

Write `scripts/vm/create_vm.sh` (run on the Mac) that does the following, then run it:

1. Install Multipass if missing: `brew install --cask multipass` (install Homebrew first if needed, and ask the user before installing anything).
2. Pick resources from the Mac's RAM (`sysctl -n hw.memsize`) and cores (`sysctl -n hw.ncpu`):
   - 16 GB or more → `--memory 8G --cpus 4`
   - 8 GB → `--memory 4G --cpus 2`, and the lab must use `--small`
   - Disk: `--disk 40G`
3. Create the VM: `multipass launch 22.04 --name fortress --memory <M> --cpus <C> --disk 40G`
4. Share the project folder: `multipass mount --type native "$HOME/CN Project" fortress:/home/ubuntu/fortress-net`. If native mounts aren't supported on this setup, fall back to the default mount type.
5. Check the kernel has Open vSwitch support: `multipass exec fortress -- sudo modprobe openvswitch`. If that fails, install `linux-modules-extra-$(uname -r)` in the VM, or `linux-generic`, and reboot the VM.
6. Print the VM's IP (`multipass info fortress`). Grafana will later be at `http://<IP>:3000` in the Mac's browser.

Also write:
- `scripts/vm/vm.sh`: a small wrapper so any command runs in the project folder inside the VM, e.g. `./scripts/vm/vm.sh sudo python3 topology/fortress_topo.py`. It runs `multipass exec fortress -- bash -lc "cd ~/fortress-net && <command>"`.
- `scripts/vm/snapshot.sh <name>`: stops the VM, runs `multipass snapshot fortress --name <name>`, starts it again. Used for the `clean-setup` and `demo-ready` snapshots.
- A "Lab VM" section in `README.md` explaining these scripts for teammates. Windows/Intel teammates install Multipass from multipass.run and run the same `multipass` commands by hand.

**Rules for every later task:**
- Edit files on the Mac; run, install and test **only inside the VM** through `scripts/vm/vm.sh`. Never install Mininet, OVS or FRR on the Mac.
- Wireshark: capture with `tcpdump -w docs/evidence/<name>.pcap` in the VM. The file appears in the shared folder and opens in Wireshark on the Mac.
- Keep runtime data (Suricata logs, Prometheus data, venvs) inside the VM, not in the shared folder. Shared mounts are slower and can break sockets and venvs.

- **Check:** `multipass list` shows `fortress` running; `./scripts/vm/vm.sh ls` lists the project files; `./scripts/vm/vm.sh sudo modprobe openvswitch` succeeds; a `clean-vm` snapshot exists.

### T00: Project context file
- `CLAUDE.md` already exists. Review it against the roadmap, fix anything missing, and keep it updated with the decisions made in T02b.
- **Check:** a new Claude Code session can explain the project from CLAUDE.md alone.

### T01: Repository scaffold
- Create the folder layout from Section 3 of the roadmap, a `README.md`, `.gitignore` (venvs, pcaps, models, `__pycache__`) and `requirements.txt`.
- Create `config/fortress.yaml` as the **single source of truth**: zones, VLANs, subnets, VIPs, host names, router links. Every later script reads from it instead of hard-coding addresses.
- **Check:** `python -c "import yaml; yaml.safe_load(open('config/fortress.yaml'))"` works; `tree` matches the roadmap.

### T02: Setup script
- `scripts/setup_vm.sh`: installs Mininet, OVS, FRR, Suricata, dnsmasq, rsyslog, strongSwan, nmap, iperf3, tcpdump (and chrony, for the optional time-sync part of T15).
- Python: Ubuntu 22.04 ships Python 3.10, which Ryu doesn't support well. Try **os-ken** (maintained Ryu fork) on 3.10 first; if it fails, install Python 3.9 (deadsnakes PPA or pyenv) and use Ryu in its own venv. Record which one worked in `docs/decisions.md`.
- `scripts/check_env.sh`: prints a ✅/❌ for every tool and version.
- Run both inside the VM: `./scripts/vm/vm.sh sudo ./scripts/setup_vm.sh`. All packages must be arm64-compatible (they are in Ubuntu's repos; for Prometheus and Grafana use their arm64 builds).
- Put Python venvs in the VM's home folder (e.g. `~/venvs/`), not in the shared project folder.
- **Check:** in the VM, `setup_vm.sh` then `check_env.sh` is all green, and the controller starts with Mininet's sample topology. Then take a `clean-setup` snapshot.

### T02b: Architecture decisions (do this before writing any network code)
Write `docs/decisions.md` answering these, because later tasks depend on them. Each has a recommended answer:

1. **How do VLANs work with an SDN controller?** OVS port tags (`ovs-vsctl set port ... tag=`) only apply when a switch uses its built-in NORMAL forwarding. Once our controller installs its own flows, it must handle VLANs itself. **Recommended:** the controller is VLAN-aware. It reads the access-port → VLAN map from `fortress.yaml`, pushes the tag on ingress, carries it across the core and pops it at the egress access port. Router trunk ports stay tagged.
2. **What forwards traffic before the controller exists (T03–T07)?** **Recommended:** run the switches in `standalone` fail-mode with **RSTP enabled** (the dual uplinks form loops, so this is required) and OVS port tags. From T08 the switches move to `secure` mode under the controller.
3. **What runs where?** Mininet hosts share the VM's filesystem and processes and only get their own network namespace. **Recommended:**
   - Inside host namespaces: FRR (separate config and run directory per router), dnsmasq, sshd, strongSwan, rsyslog server, Suricata (on ids1).
   - In the VM's root namespace: Ryu, the threat engine, Prometheus, Grafana. Together they read logs such as Suricata's `eve.json` straight from the shared filesystem.
   - Add one link from the Management VLAN to the root namespace so Prometheus can scrape services inside the lab.
   - Per-router iptables rules must run with `ip netns exec` / Mininet `cmd()` in that router's namespace, not on the VM.
4. **Where do ML features come from?** Ryu flow stats are too coarse for features like "distinct destination ports per source" unless flows are per 5-tuple, which would explode the flow tables. **Recommended:** compute per-host features from packets seen on ids1's mirror port (scapy or tshark in 5-second windows) plus Suricata alerts. Ryu stats only feed the dashboard.
5. **How is the controller structured?** Five features (rerouting, fast-failover, QoS, isolation, threat-aware rerouting) live in one controller. **Recommended** modules: `topology.py` (graph), `paths.py` (primary + backup paths), `vlan.py`, `policy.py` (zone rules, allowlist), `qos.py`, `response.py` (block, quarantine), `rest_api.py`, all wired together in `fortress_ctrl.py`.
6. **Ansible or not?** **Recommended:** generate every config from `fortress.yaml` with Jinja2 templates (that's our infrastructure as code), and push into namespaces with a script. Ansible is optional, and the roadmap's Ansible items can be skipped.

- **Check:** `docs/decisions.md` exists with all six answers, and CLAUDE.md is updated to match.

### T03: Topology
- `topology/fortress_topo.py`: builds cores c1, c2; access s1–s5 with dual uplinks; routers r1–r4 as Linux hosts with forwarding on; srv1–srv3, ids1 and 2–3 hosts per zone. Everything is read from `fortress.yaml`.
- VLAN tags on access ports, trunks on uplinks, VLAN sub-interfaces on r1/r2.
- Command-line flags: `--controller-ip`, `--small` (3 zones, for slow laptops), `--standalone` (OVS standalone mode + RSTP, used until T08; see T02b).
- **Check:** `sudo python3 topology/fortress_topo.py --standalone` starts; `net` and `dump` show every node; hosts in the same zone ping each other with no broadcast storm.

### T04: DHCP and DNS
- dnsmasq config on srv1 generated from `fortress.yaml` (one scope per VLAN, gateway = VIP); DHCP relay on r1/r2; DNS names like `srv1.fortress.mil`.
- **Check:** every host gets an address in its zone subnet and resolves `srv1.fortress.mil`.

### T05: Routing with FRR
- Per-router FRR configs (OSPF area 0, costs that make r1–r3 primary and r2–r3 backup, floating static route on r2, NAT to r4).
- A helper that starts zebra/ospfd/staticd inside each router namespace with its own config and run directory.
- **Check:** `show ip ospf neighbor` is Full on all adjacencies; Command pings r3 and r4.

### T06: Gateway redundancy (VRRP)
- VRRP for every zone VIP: r1 priority 120, r2 priority 100, preemption on. Use FRR vrrpd with macvlan interfaces; if that fails after a real attempt, switch to keepalived and note why in `docs/decisions.md`.
- **Check:** stop r1 → hosts still ping their gateway; r2 is master.

### T07: Lab start/stop
- `scripts/start_all.sh` and `scripts/stop_all.sh`: bring the whole lab up or down in the right order, with a health check at the end.
- **Check:** from a clean boot, one command gives a working lab; stop leaves no stray processes or OVS bridges (`sudo mn -c` runs as part of it).

---

## Part B: Self-healing

### T08: Ryu controller, base
- Build the module structure from T02b. `topology.py`: LLDP discovery into a networkx graph. Host learning, shortest-path flow installation, spanning-tree flooding (no broadcast storms), and VLAN push/pop from `fortress.yaml` (`vlan.py`).
- Switch the topology from `--standalone` to controller mode.
- **Check:** with the controller, everything that worked in T04–T07 still works: DHCP, same-zone pings, inter-zone routing through r1/r2 and VRRP failover. No packet storm in `ovs-ofctl dump-flows`.

### T09: Reactive rerouting
- Handle port-down / link-delete events: remove the link, delete affected flows, install new paths. Log each reroute (time, link, old path, new path) as JSON.
- **Check:** 10 pings/s from Command, then `link c1 s1 down` → pings resume; log entry written.

### T09b: Early measurement tool
- `scripts/measure.py`: a small, reusable function that runs a high-rate ping (and optionally iperf3) between two hosts, triggers a given failure, and returns packets lost and recovery time. T25 builds on it later.
- Use it right away to record the first numbers for reactive rerouting and VRRP in `docs/results/early.csv`.
- **Check:** one command prints loss and recovery time for a link failure.
- **Why now:** having numbers from early on lets you see whether each later change (fast-failover, QoS) actually improves things.

### T10: LACP bond between the cores
- OVS LACP bond c1↔c2 set up from the topology script.
- **Check:** `ovs-appctl bond/show` shows both members; drop one → traffic continues.

### T11: ⭐ Proactive fast-failover (signature feature)
- Add a second mode where the controller **precomputes a backup path** for every primary path and installs both using **OpenFlow fast-failover groups** (`OFPGT_FF`). Then a switch can fail over by itself in the data plane, without waiting for the controller.
- Flag: `--mode reactive|proactive`.
- **Check:** both modes work; Task T25 measures the difference.
- **Why it matters:** most student Mininet projects only do reactive rerouting. Comparing reactive vs proactive with measured numbers is real research-style work.

### T12: ⭐ Mission-priority traffic (signature feature)
- During a failure the surviving links have less capacity. Give **Command Center and Communications traffic priority** using OVS queues (QoS) and OpenFlow set-queue actions. Barracks bulk traffic goes to a low-priority queue.
- **Check:** saturate a link with iperf from Barracks; Command's ping latency stays low while Barracks slows down. Record both.
- **Why it matters:** it ties the military theme to a real networking concept (QoS), so the theme isn't just a label.

---

## Part C: Security

### T13: Zone isolation and rogue control
- Ryu rules from the "Allowed to reach" column of the IP plan; MAC allowlist for rogue devices; DHCP replies only from srv1's port. Matching iptables rules on r1/r2.
- **Check:** Barracks → Intelligence fails; Command → Intelligence works; a new unknown host gets no connectivity; a fake DHCP server on a host is ignored.

### T14: IPsec VPN to the FOB
- strongSwan (swanctl) between r1 and r3 for base subnets ↔ 172.16.70.0/24.
- **Check:** SA is up; tcpdump on the WAN link shows only ESP.

### T15: SSH and central logging
- SSH on routers and servers with local accounts, reachable only from VLAN 99 (no Telnet). Brute-force protection is handled by the threat engine (T20/T21), so no fail2ban.
- rsyslog: all routers, servers and the controller send logs to srv2.
- Remember that hosts share one filesystem: give each sshd instance its own config and log file (see T02b).
- *Optional:* chrony on srv2 as the time server, so all log timestamps line up.
- **Check:** admin SSH into r1 works from VLAN 99 and fails from Barracks; a failed login on r1 appears in srv2's logs.

---

## Part D: Detection

### T17: Suricata on a mirror port
- OVS mirror from the cores to ids1; Suricata with ET Open + `local.rules` for scans and SSH brute force; output to `eve.json`.
- **Check:** `nmap -sS` from Barracks produces a scan alert in `eve.json`.

### T18: Flow collector and baseline
- `detection/flow_collector.py`: per-host features every 5 s, computed from packets on ids1's mirror port (packets/s, bytes/s, new connections/s, distinct destination ports, distinct destination hosts, failed TCP handshakes), as decided in T02b.
- `detection/baseline_traffic.py`: realistic normal traffic. Save the features to CSV.
- **Check:** 30 minutes of baseline gives a clean CSV with no missing values.

### T19: Anomaly model
- `detection/train_model.py`: Isolation Forest, saved with joblib, plus a short evaluation script that prints the false-positive rate on held-out normal traffic.
- **Check:** false-positive rate under 5% on normal traffic.

### T20: ⭐ Explainable threat engine (signature feature)
- `detection/threat_engine.py`: combines the anomaly score, Suricata alerts and failed-login events from the central logs (rsyslog on srv2) into one score (0–100). Every score carries **human-readable reasons**, e.g. "Suricata scan signature +30; 214 ports probed +30; anomaly 0.82 +28".
- **Multi-source correlation:** signals about the same host in the same time window add up. Example: a host that scans ports and then fails SSH logins on r1 scores higher than either action alone.
- **Check:** port scan ≥ 70 with reasons; baseline traffic < 40; scan + brute force from one host scores higher than either alone.
- **Why it matters:** most student IDS projects output "attack / not attack". Explaining *why*, and correlating several sources, is uncommon.

---

## Part E: Response and monitoring

### T21: Automatic response
- `controller/block_api.py` (REST block/unblock at the access port, with an auto-expiry option). The threat engine calls it on Breach.
- **Check:** port scan → attacker isolated within 10 seconds of starting; other hosts unaffected.

### T22: ⭐ Threat-aware rerouting (signature feature, *optional*)
- Do this only once T21 works reliably and there is time left.
- If an attack comes from inside a zone, the controller **quarantines** that access switch's non-essential traffic and **routes Command traffic around it**. The response changes the network's paths, not just a drop rule.
- **Check:** during an insider attack on s4, Command ↔ Servers traffic avoids s4; the attacker is isolated; everything else keeps working.

### T23: Monitoring stack
- `monitoring/exporter.py` (link status, zone traffic, flow counts, threat score per host, blocked hosts, reroute count, recovery times); Prometheus config; Grafana "Command Center" dashboard exported as JSON. Logs stay on srv2 (no Loki).
- **Check:** dashboard loads from the JSON and shows live data during a failure and an attack.

### T24: Telegram alerts and incident reports
- Grafana contact point for Telegram (bot token from an environment variable, **never committed**); alert rules for threat score ≥ 70 and link down.
- Every Breach writes `docs/incidents/<timestamp>.md` with timeline, evidence, score reasons and actions taken.
- **Check:** port scan → phone alert + incident file.

---

## Part F: Proof and presentation

### T25: ⭐ Automated chaos test and results (signature feature)
- `scripts/chaos_test.py`, built on `measure.py` from T09b: runs each failure (link, bond member, r1, primary WAN) **5 times**, in reactive and proactive mode, while measuring packet loss and recovery time with high-rate ping and iperf3. It also runs the attack scenarios and records detection time and block time.
- Outputs `docs/results/results.csv` plus charts (matplotlib): recovery time per mechanism, reactive vs proactive, detection-to-block time.
- **Check:** one command produces the full results table and charts.
- **Why it matters:** numbers you measured yourself, averaged over repeated runs, are what make a teacher take a project seriously.

### T26: Demo mode
- `scripts/demo.sh`: walks through the 10-minute demo script step by step, pausing for Enter between scenarios and printing what to point at.
- **Check:** a full rehearsal runs in under 10 minutes.

### T27: Documentation and report
- `README.md` (how to run), `docs/architecture.md` with diagrams, `docs/decisions.md` (why each tool was chosen), `docs/cisco-equivalents.md`, and a report draft in `docs/report/` using the results from T25.
- **Check:** a teammate who wasn't involved can run the lab from the README alone.

### T28: Viva prep sheet
- `docs/viva-prep.md`: every viva topic from the roadmap with a short answer, *linked to the exact file and line in our code where it happens*.
- **Check:** each team member can answer every question without notes.

### T29: Final hardening
- Run `chaos_test.py` three times from a clean boot. Fix anything flaky. Make sure no secrets are in Git. Tag `v1.0-final`.
- **Check:** three clean runs in a row.

---

## Why this won't look like a common project

Be honest with yourself about this: **the individual pieces are common.** Mininet + Ryu link-failure rerouting is a standard tutorial, and "ML-based intrusion detection in SDN" is one of the most common student project titles. A teacher who has seen those will recognise them.

What makes Fortress-Net different is everything around those pieces:

| What typical projects do | What Fortress-Net does |
|--------------------------|------------------------|
| Reactive rerouting only | Reactive **and** proactive fast-failover, compared with measured numbers (T11, T25) |
| Theme is just a label | Military theme drives real design: zone isolation, mission-priority QoS, VPN to a forward base (T12, T13, T14) |
| IDS says "attack / normal" | Explainable scores with reasons, correlated across several sources (T20) |
| Detection only, or a simple drop rule | Closed loop: detect → explain → block → report, plus rerouting around the threat if T22 is done (T21, T22, T24) |
| Screenshots as proof | Automated chaos testing with repeated measurements and charts (T25) |
| Manual configs | One config file, infrastructure as code, one-command start (T01, T07) |
| Simulator-only (Packet Tracer) | Real Linux software: FRR, strongSwan, Suricata, Prometheus/Grafana |

**How to pitch it to your teacher (one line):**
> "Fortress-Net is a self-healing military base network that measures its own recovery, compares reactive and proactive failover, and detects, explains and automatically contains insider attacks."

**Two things that protect you in the viva:**
1. Know which parts **your team wrote** (controller logic, fast-failover, QoS policy, threat engine, chaos tests) versus which are **existing tools you configured** (FRR, Suricata, strongSwan). Say this openly. Teachers trust students who are clear about it.
2. Every team member must understand the code Claude Code writes. Use T28 for this. If you can't explain a file, ask Claude Code to walk you through it before the viva.

No one can guarantee that nobody has built something similar. But with T11, T12, T20 and T25 done (and T22 if time allows), this is well above a typical mini project, and you'll have measured results to back it up.

---

## Task checklist

`[ ]` free · `[~]` claimed (don't touch, see CLAUDE.md "Team workflow") · `[x]` done. Claim and push **before** starting.
Every member does T-1 on their own machine; the box records that the scripts exist.

- [x] T-1 Create the lab VM (Multipass)
- [x] T00 Project context file
- [x] T01 Repository scaffold
- [ ] T02 Setup script
- [ ] T02b Architecture decisions
- [ ] T03 Topology
- [ ] T04 DHCP and DNS
- [ ] T05 Routing with FRR
- [ ] T06 Gateway redundancy (VRRP)
- [ ] T07 Lab start/stop
- [ ] T08 Ryu controller, base
- [ ] T09 Reactive rerouting
- [ ] T09b Early measurement tool
- [ ] T10 LACP bond
- [ ] T11 ⭐ Proactive fast-failover
- [ ] T12 ⭐ Mission-priority traffic
- [ ] T13 Zone isolation and rogue control
- [ ] T14 IPsec VPN
- [ ] T15 SSH and central logging (chrony optional)
- [ ] T17 Suricata on mirror port
- [ ] T18 Flow collector and baseline
- [ ] T19 Anomaly model
- [ ] T20 ⭐ Explainable threat engine
- [ ] T21 Automatic response
- [ ] T22 ⭐ Threat-aware rerouting (optional)
- [ ] T23 Monitoring stack
- [ ] T24 Telegram alerts and incident reports
- [ ] T25 ⭐ Automated chaos test and results
- [ ] T26 Demo mode
- [ ] T27 Documentation and report
- [ ] T28 Viva prep sheet
- [ ] T29 Final hardening

**Minimum version if time runs out:** T-1, T00–T09b, T11, T13, T17–T21, T25. That still includes three of the six signature features.
