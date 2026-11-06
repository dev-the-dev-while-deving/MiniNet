# CLAUDE.md: Fortress-Net

## What this project is
CIE3 Computer Networks mini project (team, 15 marks, viva). A self-healing, AI-assisted secure network for a military base, built in **Mininet** with real Linux networking software. There is no Packet Tracer version, and IoT, RADIUS, fail2ban, Loki, Zeek and the Random Forest classifier are out of scope.

- Full design and roadmap: `FORTRESS-NET-ROADMAP.md`
- Work queue: `CLAUDE-CODE-TASKS.md`. Do **one task at a time**, in order, and stop when its acceptance check passes.

## Lab environment
- The host is an Apple Silicon (arm64) Mac. The lab runs in an Ubuntu 22.04 **Multipass** VM named `fortress` (created by task T-1).
- This folder is mounted in the VM at `/home/ubuntu/fortress-net`.
- Edit files on the Mac; run, install and test **only inside the VM** with `./scripts/vm/vm.sh <command>`. Never install Mininet, OVS or FRR on the Mac.
- If the `fortress` VM doesn't exist yet (`multipass list`), do task T-1 first.
- Venvs, Suricata logs and Prometheus data live inside the VM, not in the shared folder.

## Stack
Mininet + Open vSwitch · Ryu (Python 3.9 venv) or os-ken · FRRouting (OSPF, VRRP, static) · dnsmasq · rsyslog · strongSwan · Suricata · scikit-learn · Prometheus + Grafana · Telegram alerts. Optional: chrony, Ansible, threat-aware rerouting (T22).

## Rules
- `config/fortress.yaml` is the single source of truth for zones, VLANs, subnets, VIPs and host names. Never hard-code addresses in scripts.
- Everything must run on Ubuntu 22.04 in a VM with 8 GB RAM (and degrade to 4 GB with `--small`).
- Attack tools (nmap, hydra, floods) run **only inside the Mininet lab**, never on real networks.
- Secrets (Telegram bot token, VPN keys) come from environment variables or a git-ignored `.env`. Never commit them.
- Write clear comments. Students must explain every file in a viva, so prefer readable code over clever code.
- Record design choices and any fallback (e.g. keepalived instead of FRR vrrpd) in `docs/decisions.md`.
- After each task: update the checklist in `CLAUDE-CODE-TASKS.md` and suggest a commit message.

## IP plan (summary)
| Zone | VLAN | Subnet |
|------|------|--------|
| Command Center | 10 | 192.168.10.0/24 |
| Intelligence | 20 | 192.168.20.0/24 |
| Communications | 30 | 192.168.30.0/24 |
| Barracks | 40 | 192.168.40.0/24 |
| Servers | 60 | 10.10.60.0/24 |
| Management | 99 | 192.168.99.0/24 |
| FOB | 70 | 172.16.70.0/24 |

Gateway VIP is `.1` in each zone (r1 = .2, r2 = .3). Router links use /30s from 10.0.0.0/24.

## Running the lab (from the Mac)
- Start: `./scripts/vm/vm.sh sudo ./scripts/start_all.sh`
- Stop: `./scripts/vm/vm.sh sudo ./scripts/stop_all.sh` (always runs `sudo mn -c`)
- Tests: `./scripts/vm/vm.sh sudo python3 scripts/chaos_test.py`
- Snapshot: `./scripts/vm/snapshot.sh <name>`
