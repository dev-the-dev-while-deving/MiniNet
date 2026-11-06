# Fortress-Net

A self-healing, AI-assisted secure network for a military base, built in Mininet. See `FORTRESS-NET-ROADMAP.md` for the design.

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
