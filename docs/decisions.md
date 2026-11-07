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
