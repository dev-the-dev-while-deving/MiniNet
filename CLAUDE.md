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
- Venvs (`~/venvs/`), Suricata logs and Prometheus data live inside the VM, not in the shared folder. Shared mounts are slower and break sockets and venvs.
- Packet captures: `tcpdump -w docs/evidence/<name>.pcap` in the VM, then open them in Wireshark on the Mac.
- Snapshots: `./scripts/vm/snapshot.sh <name>`. Planned names are `clean-vm` (after T-1), `clean-setup` (after T02) and `demo-ready`. Restore with `multipass stop fortress && multipass restore fortress.<name>`.
- `sudo` on the Mac needs the user's password, which Claude can't type. Run Mac-side sudo commands in a terminal tab the user can see.

## Stack (6 layers)
| Layer | Software |
|-------|----------|
| 1. Virtual network | Mininet + Open vSwitch |
| 2. SDN controller | os-ken on Python 3.10, or Ryu in a Python 3.9 venv (decided in T02) |
| 3. Routing and WAN | FRRouting in router namespaces: OSPF, VRRP, static; iptables NAT |
| 4. Services | dnsmasq (DHCP/DNS), rsyslog, strongSwan IPsec, sshd. chrony optional |
| 5. Detection | Suricata on a mirror port + scikit-learn Isolation Forest |
| 6. Command layer | Prometheus + Grafana, Telegram alerts |

Optional: chrony, Ansible, threat-aware rerouting (T22).

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
- **Core:** c1, c2 (OVS), joined by an LACP bond (T10).
- **Access:** s1–s5, one per zone plus one for the servers, each uplinked to both cores, which makes loops (RSTP or the controller's spanning tree handles them).
- **Routers** (Mininet hosts with IP forwarding): r1, r2 are zone gateways (VRRP, OSPF, NAT, VPN), r3 is the FOB and r4 is the simulated ISP/internet.
- **Servers:** srv1 DHCP/DNS (domain `fortress.mil`), srv2 syslog (+ optional NTP), srv3 web/file target; ids1 runs Suricata on a mirror port.
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

## Threat scoring (T20)
Score 0–100 = Isolation Forest anomaly + rules: Suricata alert +30, >50 ports probed +30, repeated failed SSH logins +25, scan then login attempts from the same host +15. Bands: 0–39 Normal (log), 40–69 Suspicious (dashboard), 70–100 Breach (Telegram alert + automatic block through the controller's REST API). Every score keeps its list of reasons.

## Architecture decisions
Filled in by T02b; the full reasoning goes in `docs/decisions.md`. Until then, don't write network code that depends on them (VLAN handling under the controller, standalone/RSTP mode before T08, what runs in which namespace, where ML features come from, controller module layout, Ansible vs Jinja2).

## Status
Live claims are in the `CLAUDE-CODE-TASKS.md` checklist; this is a short summary.
- Done: T-1 (scripts; each member runs `create_vm.sh` on their own machine), T00 (this file).
- In progress: T01 (dev-the-dev-while-deving).
- Next free: T02 after T01; T02b can be written in parallel.

## Running the lab (from the Mac)
These scripts don't exist yet; the task that creates each one is in brackets.
- Start: `./scripts/vm/vm.sh sudo ./scripts/start_all.sh` (T07)
- Stop: `./scripts/vm/vm.sh sudo ./scripts/stop_all.sh`, which always runs `sudo mn -c` (T07)
- Environment check: `./scripts/vm/vm.sh ./scripts/check_env.sh` (T02)
- Tests: `./scripts/vm/vm.sh sudo python3 scripts/chaos_test.py` (T25)
- Snapshot: `./scripts/vm/snapshot.sh <name>` (exists)
