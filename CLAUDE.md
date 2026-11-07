# CLAUDE.md: Fortress-Net

## What this project is
CIE3 Computer Networks mini project (team of four, 15 marks, judged in a viva). A self-healing, AI-assisted secure network for a military base, built in **Mininet** with real Linux networking software. There is no Packet Tracer version, and IoT (old Phase 5), RADIUS, fail2ban, Loki, Zeek and the Random Forest classifier are out of scope.

- Full design and roadmap: `FORTRESS-NET-ROADMAP.md`
- Work queue: `CLAUDE-CODE-TASKS.md`. Do **one task at a time**, in order, and stop when its acceptance check passes. The user runs the check before the next task starts.

**One-line pitch:** a self-healing military base network that measures its own recovery, compares reactive and proactive failover, and detects, explains and automatically contains insider attacks.

### The three demo scenarios (everything is built around these)
1. **Link sabotage:** a core–access link is cut, the SDN controller reroutes, and pings keep flowing.
2. **Equipment failure:** gateway router r1 dies, and VRRP moves the gateway to r2 within seconds.
3. **Insider attack:** a Barracks host port-scans or brute-forces SSH. Suricata plus the ML model score it at 70 or more, and the host is isolated automatically.

### Signature features (do not skip)
T11 proactive fast-failover (OpenFlow `OFPGT_FF` groups), T12 mission-priority QoS, T20 explainable threat engine (score with reasons, multi-source correlation), T25 automated chaos test with charts. T22 threat-aware rerouting is optional.

## Team workflow (two people, each with their own Claude Code)
GitHub (`dev-the-dev-while-deving/MiniNet`, branch `main`) is the shared state. The checklist at the bottom of `CLAUDE-CODE-TASKS.md` is the lock that stops two people doing the same task.
1. **Before starting anything:** `git pull --rebase`. Read the checklist and the Status section below.
2. **Claim the task first:** change its line to `- [~] T03 Topology (claimed: <github-user>, <date>)`, then commit (`claim T03`) and **push straight away**, before writing any code. If the push is rejected, pull: someone else may have claimed it.
3. Never start a task marked `[~]` by someone else, or one whose earlier tasks aren't `[x]` (unless it's a standalone piece the roadmap says can run in parallel, e.g. the controller on Mininet's sample topology).
4. **When done:** tick `[x]`, update Status below, commit (`T03: ...`), `git pull --rebase`, then push. Push after every task, never sit on local commits.
5. Shared files (`CLAUDE.md`, `CLAUDE-CODE-TASKS.md`, `config/fortress.yaml`, `docs/decisions.md`): keep edits small and pull right before editing them. On a merge conflict, keep both people's changes.
6. Each person has their **own** VM. Machine-specific details (RAM, VM IP, snapshots) below are the original author's; yours may differ, and they're never committed as config.

## Lab environment
- The original author's host is an Apple Silicon (arm64) Mac with 24 GB RAM. The lab runs in an Ubuntu 22.04 arm64 **Multipass** VM named `fortress` (8 GB RAM, 4 CPUs, 40 GB disk), created by `scripts/vm/create_vm.sh` (task T-1).
- This folder is mounted in the VM at `/home/ubuntu/fortress-net` (native 9p mount, so files edited on the Mac appear in the VM straight away).
- Edit files on the Mac; run, install and test **only inside the VM** with `./scripts/vm/vm.sh <command>`. The arguments run under `bash -lc` in the project folder, so quote pipes: `./scripts/vm/vm.sh 'ip a | grep inet'`. Never install Mininet, OVS or FRR on the Mac.
- If the `fortress` VM doesn't exist yet (`multipass list`), do task T-1 first.
- Venvs, Suricata logs and Prometheus data live inside the VM, not in the shared folder. Shared mounts are slower and break sockets and venvs.
- Python (decisions D3, D7): system `python3` (3.10) for the Mininet topology (run as root); `~/venvs/ryu` (3.9) for the Ryu controller (`~/venvs/ryu/bin/ryu-manager`); `~/venvs/fortress` (3.10) for detection, ML and monitoring. Venvs are **not** on PATH; always call them by full path.
- Apple Silicon VMs need `OPENSSL_armcap=0` (set in `/etc/environment` by `setup_vm.sh`) or pip-installed OpenSSL crashes with "Illegal instruction" (D8). chrony keeps the VM clock; after any VM stop/start, re-sync it with `sudo chronyc -a makestep` (D9).
- Packet captures: `tcpdump -w docs/evidence/<name>.pcap` in the VM, then open them in Wireshark on the Mac.
- Snapshots: `./scripts/vm/snapshot.sh <name>`. Planned names are `clean-vm` (after T-1), `clean-setup` (after T02) and `demo-ready`. Restore with `multipass stop fortress && multipass restore fortress.<name>`.
- `sudo` on the Mac needs the user's password, which Claude can't type. Run Mac-side sudo commands in a terminal tab the user can see.

## Stack (6 layers)
| Layer | Software |
|-------|----------|
| 1. Virtual network | Mininet + Open vSwitch |
| 2. SDN controller | Ryu 4.34 + eventlet 0.30.2 on Python 3.9 (os-ken dropped its launcher and sample apps; see D3) |
| 3. Routing and WAN | FRRouting in router namespaces: OSPF, VRRP, static; iptables NAT |
| 4. Services | dnsmasq (DHCP/DNS), rsyslog, strongSwan IPsec, sshd. chrony optional |
| 5. Detection | Suricata on a mirror port + scikit-learn Isolation Forest |
| 6. Command layer | Prometheus + Grafana, Telegram alerts |

Optional: chrony (lab NTP server), threat-aware rerouting (T22). Ansible is dropped (D15).

## Rules
- `config/fortress.yaml` (created in T01) is the single source of truth for zones, VLANs, subnets, VIPs, host names and router links. Never hard-code addresses in scripts.
- Everything must run on Ubuntu 22.04 in a VM with 8 GB RAM, and degrade to 4 GB with `--small` (3 zones, fewer hosts).
- Attack tools (nmap, hydra, floods) run **only inside the Mininet lab**, never on real networks.
- Secrets (Telegram bot token, VPN keys) come from environment variables or a git-ignored `.env`. Never commit them.
- Write clear comments. Students must explain every file in the viva, so prefer readable code over clever code.
- Record design choices and any fallback (e.g. keepalived instead of FRR vrrpd) in `docs/decisions.md`.
- Never break the last working version. Tag a Git release at each milestone (`v0.1-base`, `v0.2-routing`, `v0.3-selfheal`, `v0.4-security`, `v0.6-detection`, `v0.7-response`, `v1.0-final`).
- After each task: tick its box in `CLAUDE-CODE-TASKS.md`, tell the user how to check it, suggest a commit message (`T03: ...`), and wait.

## Topology
- **Core:** c1, c2 (OVS), joined by two cables the controller aggregates with a select group (T10, D24; OVS bonds only work in NORMAL mode).
- **Access:** s1–s5, one per zone plus one for the servers, each uplinked to both cores, which makes loops (RSTP or the controller's spanning tree handles them).
- **Routers** (Mininet hosts with IP forwarding): r1, r2 are zone gateways (VRRP, OSPF, NAT, VPN), r3 is the FOB and r4 is the simulated ISP/internet.
- **Servers:** srv1 DHCP/DNS (domain `fortress.mil`), srv2 syslog (+ optional NTP), srv3 web/file target; ids1 runs Suricata on mirror ports of both cores (no IP).
- **Zone hosts:** 2–3 per zone (normal users plus an attacker in Barracks).

## IP plan
| Zone | VLAN | Subnet | Allowed to reach |
|------|------|--------|------------------|
| Command Center | 10 | 192.168.10.0/24 | All zones |
| Intelligence | 20 | 192.168.20.0/24 | Command only |
| Communications | 30 | 192.168.30.0/24 | Command, servers, FOB |
| Barracks | 40 | 192.168.40.0/24 | Servers and internet only |
| Servers | 60 | 10.10.60.0/24 | All zones |
| Management | 99 | 192.168.99.0/24 | Admin SSH only |
| FOB | 70 | 172.16.70.0/24 | Over the IPsec VPN (r3 is .1) |

- Gateway VIP is `.1` in each base zone (r1 = .2, r2 = .3). VRRP priority: r1 120, r2 100, preemption on.
- Router links are /30s from 10.0.0.0/24: r1–r2, r1–r3 (primary WAN), r2–r3 (backup WAN), r1–r4, r2–r4. OSPF area 0 with costs that keep r1–r3 primary, plus a floating static route to the FOB on r2.
- IPsec (strongSwan) runs between r1 and r3 for 192.168.0.0/16 ↔ 172.16.70.0/24.

## Threat scoring (T20, implemented in `detection/threat_engine.py`)
One score out of 100 per host, every 5 s, with the reasons attached. The table below is the
contract; `threat_engine.py --self-check` proves it against worked examples.

| Signal | Points | When |
|--------|--------|------|
| anomaly | up to 30 | the model's 0..1 verdict, scaled (D32) |
| IDS signature | +30 | any Suricata alert from this host in the last 30 s |
| port sweep | +30 | more than 50 different destination ports in one 5 s window |
| refused connections | +15 | 20 or more SYNs unanswered, or resets coming back |
| failed SSH logins | +25 | 5 or more in the last 120 s, from srv2's central log |
| scan, then logins | +15 | correlation: the same host did both |
| two sources agree | +15 | correlation: an IDS brute-force rule **and** the central log |

Bands: **0–39 Normal** (logged), **40–69 Suspicious** (dashboard), **70–100 Breach** (block +
incident report + Telegram). The weights are set so a breach always needs **two independent
signals**: a lone Suricata alert is 30, and the model alone can reach 30 at most.

The last three rows are additions to the roadmap's table (D33): without them a port sweep or a
brute force could not reach 70 on rules alone, i.e. it would depend on the model being trained.

## Architecture decisions (T02b; full reasoning in `docs/decisions.md` D10–D15)
Follow these in every network task. If one turns out wrong, change `docs/decisions.md` first, then the code.
- **VLANs (D10):** OVS port tags only work in `NORMAL` mode. Under the controller, `controller/vlan.py` pushes the 802.1Q tag on ingress at access ports (VLAN map from `fortress.yaml`), keeps it across the cores and pops it at the egress access port. r1/r2 trunk ports stay tagged, with sub-interfaces like `r1-eth0.10`.
- **Before the controller, T03–T07 (D11):** `fortress_topo.py --standalone` sets `fail_mode=standalone` + `rstp_enable=true` on every bridge (the dual uplinks make loops) and uses OVS port tags. From T08, `fail_mode=secure`, OpenFlow 1.3, RSTP off.
- **What runs where (D12):** Mininet hosts get their own network namespace but share the filesystem, process list and `/dev/log`, so every per-node daemon gets its own config, PID, socket and log path. FRR uses `-N <router>` (`vtysh -N r1`). Rendered configs go in `/etc/fortress/<node>/`, logs in `/var/log/fortress/<node>/`, runtime files in `/var/run/fortress/<node>/`, all on the VM's disk, never the shared folder. Ryu, the threat engine, Prometheus and Grafana run in the VM root namespace. Per-router iptables always run inside that router (`r1.cmd(...)`). Central logs: daemons log to files and a per-node rsyslog forwarder sends them to srv2:514 over the lab network. Monitoring host `mon0` (root namespace, 192.168.99.250 on VLAN 99, added in T03) lets Prometheus reach the lab.
- **ML features (D13):** come from packets on ids1's mirror ports (ids1 has one interface on c1 and one on c2), in 5 s windows, counted only on the source host's VLAN so routed packets aren't counted twice. Suricata `eve.json` is the second signal. Ryu flow stats only feed the dashboard.
- **Controller (D14):** one Ryu app, `controller/fortress_ctrl.py`, run with `~/venvs/ryu/bin/ryu-manager --observe-links`, calling plain modules `topology.py`, `paths.py`, `vlan.py`, `policy.py`, `qos.py`, `response.py`, `rest_api.py`. The block API lives in `rest_api.py` (no separate `block_api.py`).
- **Configs (D15):** Jinja2 templates in `configs/<service>/*.j2`, rendered per node from `fortress.yaml` by `scripts/render_configs.py`. No Ansible; skip the roadmap's Ansible items.

## Status
Live claims are in the `CLAUDE-CODE-TASKS.md` checklist; this is a short summary.
- Done: T-1 (scripts; each member runs `create_vm.sh` on their own machine), T00 (this file), T01 (scaffold + `config/fortress.yaml`), T02 (`setup_vm.sh`, `check_env.sh`; Ryu on 3.9; snapshot `clean-setup`), T02b (decisions D10–D15), T03 (`topology/fortress_topo.py`, `--check`), T04 (dnsmasq on srv1, relays on r1/r2, udhcpc), T05 (FRR OSPF per router, floating static, NAT), T06 (VRRP via vrrpd + macvlan; `fail`/`recover` CLI), T07 (`start_all.sh` / `stop_all.sh`), T08 (Ryu controller: 4-table pipeline, controller flooding; D21), T09 (reroute on port-down, `reroutes.jsonl`), T09b (`scripts/measure.py`, `docs/results/early.csv`), T10 (c1-c2 aggregation via select groups, not an OVS bond; D24), T11 (`--mode proactive`: fast-failover groups; D25), T12 (HTB queues + controller set_queue; D26), T13 (controller drops never-talk pairs, r1/r2 stateful firewall, port security, rogue DHCP; D27), T14 (strongSwan r1<->r3 via VICI; D28), T15 (sshd per node, Management-only; rsyslog to srv2; D29), T17 (Suricata on both core mirrors; D30), T18 (flow collector, baseline traffic, srv3 web server; D31), T19 (Isolation Forest, 2.2% false positives; D32), T20 (explainable score with correlation; D33), T21 (block at the access port with expiry; D34), T23 (exporter, Prometheus, provisioned Grafana dashboard; D35), T24 (Telegram alert with reasons, incident reports; D36), T25 (`scripts/chaos_test.py` + `charts.py`: 5 failures x 5 runs x 2 modes, all self-healed, plus insider timing in `detection.csv`; found and fixed the VPN dying on a cut WAN, now between loopbacks; D39), T26 (`scripts/demo.sh`, `--auto` rehearsal), T27 (README, `docs/architecture.md`, `docs/testing.md`, `docs/cisco-equivalents.md`, `docs/report/report.md`). Tag: `v0.6-detection`. Tags: `v0.1-base`, `v0.2-routing`, `v0.3-selfheal`, `v0.4-security`.
- Not doing: T22 (threat-aware rerouting). Built and tested, then dropped: an access switch is never a transit node here and routed traffic must reach the VRRP master's core, so there is no path to separate (D37).
- Next free: T28 (viva prep sheet).

## Running the lab (from the Mac)
- Start: `./scripts/vm/vm.sh sudo ./scripts/start_all.sh [--small] [--standalone]`: clean slate, clock, config check, Ryu controller (unless `--standalone`), topology, FRR (OSPF + VRRP), NAT, DHCP/DNS, health check, then the Mininet CLI. `exit` in the CLI stops everything.
- Automated test: `./scripts/vm/vm.sh sudo ./scripts/start_all.sh --check`: same start, plus the disruptive VRRP failover test, then stop. Exit code = number of failed checks.
- Stop / clean up after a crash: `./scripts/vm/vm.sh sudo ./scripts/stop_all.sh`. It runs `mn -c` and prints ✅ for no lab processes, no OVS bridges, no lab interfaces.
- Controller: log `/var/log/fortress/ryu/ryu.log`, status `curl -s localhost:8080/fortress/status` (in the VM), flows `sudo ovs-ofctl -O OpenFlow13 dump-flows s1`.
- Detection and response (Part D/E): Suricata alerts `/var/log/fortress/ids1/eve.json`, traffic features `/var/log/fortress/collector/`, scores `/var/log/fortress/threat/state.json`, incident reports `docs/incidents/`. Score the lab now: `./scripts/vm/vm.sh '~/venvs/fortress/bin/python detection/threat_engine.py --once'`; check the scoring table: `--self-check`; see the Telegram message: `--test-alert`.
- Block or unblock a host by hand: `curl -s -XPOST localhost:8080/fortress/block -d '{"ip": "192.168.40.66"}'` (and `/fortress/unblock`), or `GET /fortress/blocked`.
- Monitoring: Grafana `http://<VM IP>:3000` (admin/admin, the Command Center dashboard is provisioned), Prometheus `:9090`, raw metrics `:9101/metrics`.
- Train the anomaly model: `sudo python3 detection/baseline_traffic.py --minutes 30` then `~/venvs/fortress/bin/python detection/train_model.py`. Without a model the engine scores on its rules alone and says so.
- In the CLI: `fail r1` / `recover r1` simulate a power loss; `admin1 ssh -i /etc/fortress/ssh/admin_key ubuntu@192.168.99.2` (admin SSH to r1); central logs in `/var/log/fortress/srv2/remote/`; `r1 vtysh -N r1 -c "show ip ospf neighbor"`; `cmd1 ping 172.16.70.10`.
- The pieces: `topology/fortress_topo.py` (build), `topology/services.py` (daemons per node), `topology/render_configs.py` (templates in `configs/` → `/etc/fortress/<node>/`, `/etc/frr/<router>/`), `topology/checks.py` (health checks), `topology/labconfig.py` (values derived from `fortress.yaml`).
- Gotcha: `mn -c` deletes `/tmp/*.log` and `/tmp/*.out`. Never log lab runs to those names.
- Gotcha: `pkill -f <pattern>` run through `vm.sh` matches the wrapper shell's own command line and kills it. Write the pattern as `[t]opology/...`.
- Environment check: `./scripts/vm/vm.sh ./scripts/check_env.sh --smoke` (all green after `setup_vm.sh`)
- Config check: `./scripts/vm/vm.sh python3 scripts/check_config.py` (run it after every edit to `fortress.yaml`)
- Snapshot: `./scripts/vm/snapshot.sh <name>`
- Demo: `./scripts/vm/vm.sh sudo ./scripts/demo.sh` (live, Enter between steps) or `--auto` (timed rehearsal).
- Only one lab per VM: check `pgrep -af '[c]haos_test|[s]tart_all|[d]emo.py|[r]yu-manager'` before starting any run.
- Chaos tests: `./scripts/vm/vm.sh sudo python3 scripts/chaos_test.py` (~30 min; `--runs 1 --only WAN` for one failure). Writes `docs/results/results.csv`, `summary.csv`, `detection.csv` and three charts. Trust `recovery_s` (longest silence) and `healed`, not `lost`: Mininet manages ~55 pings/s, so ~20 ms is the resolution limit.
