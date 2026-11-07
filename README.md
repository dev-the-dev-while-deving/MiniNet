# Fortress-Net

A self-healing, AI-assisted secure network for a military base, built in Mininet with real Linux networking software (Open vSwitch, an OpenFlow controller, FRRouting, strongSwan, Suricata, Prometheus/Grafana).

It survives link and router failures by itself, detects insider attacks with Suricata plus an Isolation Forest model, explains every threat score, and isolates the attacker automatically.

- Design and roadmap: [`FORTRESS-NET-ROADMAP.md`](FORTRESS-NET-ROADMAP.md)
- Task queue and progress: [`CLAUDE-CODE-TASKS.md`](CLAUDE-CODE-TASKS.md)
- All addresses and names: [`config/fortress.yaml`](config/fortress.yaml). Check it with `./scripts/vm/vm.sh python3 scripts/check_config.py`

## Repository layout

| Folder | What lives there |
|--------|------------------|
| `config/` | `fortress.yaml`, the single source of truth for zones, VLANs, subnets, hosts and router links |
| `topology/` | Mininet topology script |
| `controller/` | SDN controller app and its REST block API |
| `configs/` | Jinja2 templates for router and service configs (FRR, strongSwan, dnsmasq, rsyslog, chrony, iptables), rendered per node from `fortress.yaml` (no Ansible; see `docs/decisions.md` D15) |
| `detection/` | Suricata rules, the traffic feature collector, baseline traffic, the anomaly model, the threat engine and its Telegram alerts |
| `monitoring/` | Prometheus config, the metrics exporter, the provisioned Grafana dashboard |
| `scripts/` | Lab start/stop, setup, checks, failover and chaos tests; `scripts/vm/` manages the VM |
| `docs/` | Design decisions (`decisions.md`), evidence (screenshots, pcaps), report |

Empty folders hold a `.gitkeep` until the task that fills them.

## Quick start

1. Create the lab VM (below): `./scripts/vm/create_vm.sh`
2. Install the lab software in it: `./scripts/vm/vm.sh sudo ./scripts/setup_vm.sh`, then check it with `./scripts/vm/vm.sh ./scripts/check_env.sh --smoke`
3. Start the lab: `./scripts/vm/vm.sh sudo ./scripts/start_all.sh` (add `--small` on a 4 GB VM)

`start_all.sh` builds the whole network, starts routing, gateway redundancy, DHCP and DNS, prints a health check (all ✅) and opens the Mininet CLI. Things to try there:

| Command | Shows |
|---------|-------|
| `cmd1 ping 172.16.70.10` | Command Center reaching the FOB through r1 and r3 |
| `cmd1 traceroute -n 172.16.70.10` | the primary WAN path (r1, then r3) |
| `r1 vtysh -N r1 -c "show ip ospf neighbor"` | OSPF adjacencies |
| `r1 vtysh -N r1 -c "show vrrp summary"` | r1 is VRRP Master for every zone |
| `fail r1`, then `cmd1 ping 192.168.10.1`, then `recover r1` | scenario 2: r2 takes over the gateways in about a second |

Type `exit` to stop the lab. If anything goes wrong (a crash, a closed terminal), `./scripts/vm/vm.sh sudo ./scripts/stop_all.sh` removes all leftovers and confirms the VM is clean.

Automated check: `./scripts/vm/vm.sh sudo ./scripts/start_all.sh --check` runs every health check, the failure tests and the full insider scenario, then exits (0 = all passed).

## Watching it work

With the lab running, from your own browser:

| Where | What |
|-------|------|
| `http://<VM IP>:3000` | Grafana, with the **Fortress-Net Command Center** dashboard already loaded (login `admin` / `admin`) |
| `http://<VM IP>:9090` | Prometheus, to try queries like `fortress_threat_score` |
| `http://<VM IP>:9101/metrics` | the raw metrics the exporter publishes |

`./scripts/vm/create_vm.sh` prints the VM's IP; `multipass info fortress` shows it again.

## Detection and response

The lab watches itself. Three independent sources feed one score per host:

| Source | What it sees | Where it writes |
|--------|--------------|-----------------|
| Suricata on ids1 | signatures on traffic mirrored from both cores | `/var/log/fortress/ids1/eve.json` |
| The feature collector | per-host packets/s, ports touched, refused connections, every 5 s | `/var/log/fortress/collector/` |
| Central logging | failed SSH logins from every router and server | `/var/log/fortress/srv2/remote/` |

The threat engine combines them into a score out of 100 **with the reasons attached**, and a
breach (70+) is blocked at the host's own switch port, written up in `docs/incidents/`, and sent
to Telegram if a bot is configured (copy `.env.example` to `.env`).

```bash
./scripts/vm/vm.sh '~/venvs/fortress/bin/python detection/threat_engine.py --self-check'   # the scoring, on worked examples
./scripts/vm/vm.sh '~/venvs/fortress/bin/python detection/threat_engine.py --once'         # score the live lab now
./scripts/vm/vm.sh '~/venvs/fortress/bin/python detection/threat_engine.py --test-alert'   # see the Telegram message
```

Training the anomaly model (the lab is started and stopped for you; 30 minutes is the real run):

```bash
./scripts/vm/vm.sh sudo python3 detection/baseline_traffic.py --minutes 30
./scripts/vm/vm.sh '~/venvs/fortress/bin/python detection/train_model.py'
```

Without a trained model the engine still works, scoring on its rules alone and saying so.

## Lab VM

Mininet, Open vSwitch, FRR and Suricata only run on Linux, so the lab runs in an Ubuntu 22.04 VM made with [Multipass](https://multipass.run). You edit files on your own machine; this folder is shared into the VM at `/home/ubuntu/fortress-net`, and everything runs there.

| Script | What it does |
|--------|--------------|
| `scripts/vm/create_vm.sh` | Installs Multipass (asks first), creates the `fortress` VM sized to your RAM (8 GB if you have 16 GB+, else 4 GB and use `--small`), mounts this folder, checks the Open vSwitch kernel module, prints the VM IP |
| `scripts/vm/vm.sh <cmd>` | Runs `<cmd>` in the project folder inside the VM, e.g. `./scripts/vm/vm.sh sudo mn --test pingall`. With no command it opens a shell there |
| `scripts/vm/snapshot.sh <name>` | Stops the VM, takes a snapshot, starts it again. We use `clean-vm`, `clean-setup` and `demo-ready` |

**Mac (Apple Silicon or Intel):**
```bash
./scripts/vm/create_vm.sh
./scripts/vm/snapshot.sh clean-vm
```

**Windows / Linux:** install Multipass from multipass.run, then run the same commands by hand from the project folder (PowerShell uses `${PWD}`):
```bash
multipass launch 22.04 --name fortress --memory 8G --cpus 4 --disk 40G
multipass mount . fortress:/home/ubuntu/fortress-net
multipass exec fortress -- sudo modprobe openvswitch
multipass stop fortress && multipass snapshot fortress --name clean-vm && multipass start fortress
```
Then use `multipass exec fortress -- bash -lc "cd ~/fortress-net && <cmd>"` in place of `vm.sh`.

Restore a snapshot: `multipass stop fortress && multipass restore fortress.clean-vm`.

**Rules:** never install Mininet/OVS/FRR on the host machine. Keep venvs, Suricata logs and Prometheus data inside the VM, not in the shared folder. Save captures to `docs/evidence/*.pcap` in the VM and open them in Wireshark on your machine.
