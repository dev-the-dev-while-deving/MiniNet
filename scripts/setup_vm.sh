#!/usr/bin/env bash
# setup_vm.sh: installs everything the Fortress-Net lab needs inside the Ubuntu 22.04 VM.
# Run from the Mac:  ./scripts/vm/vm.sh sudo ./scripts/setup_vm.sh
# Safe to run again; apt and pip skip what is already installed.
#
# Three Pythons (see docs/decisions.md D3 and D7):
#   - System python3 (3.10) runs the Mininet topology, because Ubuntu's mininet package
#     installs its Python module there and the topology runs as root.
#   - ~/venvs/ryu (Python 3.9) runs the Ryu SDN controller. Ryu needs an old eventlet
#     that doesn't work on 3.10.
#   - ~/venvs/fortress (3.10) holds the detection/monitoring packages from requirements.txt.
# Both venvs live in the VM, not the shared folder (see CLAUDE.md). They are not put on PATH,
# so "python3" always means the system Python; call them as ~/venvs/<name>/bin/<tool>.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LAB_USER="${SUDO_USER:-ubuntu}"          # the normal user who owns the venvs
LAB_HOME="$(getent passwd "$LAB_USER" | cut -d: -f6)"
VENVS="$LAB_HOME/venvs"
export DEBIAN_FRONTEND=noninteractive    # no interactive package questions (e.g. tshark)

step() { echo; echo "==> $*"; }
# Ubuntu's automatic updates often run right after boot or a clock fix and hold the apt lock.
# Wait up to 10 minutes for the lock instead of failing straight away.
apt_get() { apt-get -o DPkg::Lock::Timeout=600 "$@"; }
# Run a command as the normal user, so the venvs aren't owned by root.
as_user() { sudo -u "$LAB_USER" -H "$@"; }

# --- 0. Fix the clock ---------------------------------------------------------
# After the VM is stopped/started (snapshots) or the Mac sleeps, the VM clock can be hours
# behind while still claiming to be synchronized. apt then rejects the package index as
# "not valid yet". Before chrony is installed, Ubuntu's systemd-timesyncd keeps the clock;
# installing chrony replaces (masks) timesyncd, and chrony then keeps the VM clock.
sync_clock() {
  if command -v chronyc >/dev/null; then
    systemctl start chrony
    sleep 2; chronyc -a 'burst 4/4' >/dev/null; sleep 5; chronyc -a makestep >/dev/null
  else
    systemctl restart systemd-timesyncd; sleep 5
  fi
  date -u
}
step "Syncing the VM clock"
sync_clock

# --- 1. Extra apt repositories (FRR and Grafana ship arm64 builds) ----------
step "Adding the FRRouting, Grafana and deadsnakes (Python 3.9) repositories"
apt_get update -q
apt_get install -y -q curl gnupg ca-certificates lsb-release software-properties-common
install -d /usr/share/keyrings
curl -fsSL https://deb.frrouting.org/frr/keys.gpg -o /usr/share/keyrings/frrouting.gpg
echo "deb [signed-by=/usr/share/keyrings/frrouting.gpg] https://deb.frrouting.org/frr $(lsb_release -sc) frr-stable" \
  > /etc/apt/sources.list.d/frr.list
curl -fsSL https://apt.grafana.com/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/grafana.gpg
echo "deb [signed-by=/usr/share/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
  > /etc/apt/sources.list.d/grafana.list
add-apt-repository -y -n ppa:deadsnakes/ppa   # -n: we run apt-get update ourselves next
apt_get update -q

# --- 2. Lab packages ---------------------------------------------------------
step "Installing lab packages"
# This lets tshark install without asking whether normal users may capture packets.
echo "wireshark-common wireshark-common/install-setuid boolean false" | debconf-set-selections
apt_get install -y -q \
  mininet openvswitch-switch \
  frr frr-pythontools \
  suricata \
  dnsmasq-base isc-dhcp-relay busybox-static \
  rsyslog \
  strongswan-swanctl charon-systemd \
  nmap iperf3 tcpdump tshark iputils-arping traceroute sshpass \
  chrony \
  prometheus grafana \
  python3-venv python3-pip python3-yaml python3-jinja2 \
  python3.9 python3.9-venv \
  git jq tree
# dnsmasq-base = just the binary. The full "dnsmasq" package starts a service on the VM that
# fights systemd-resolved for port 53; we only run dnsmasq inside srv1's namespace (T04).

# --- 3. Services: only OVS and the VM's own clock run on the VM itself ---------
# FRR, Suricata and strongSwan run inside Mininet hosts (one copy per router or server),
# started by our own scripts, so their VM-wide services must not run.
# chrony stays on: it is the VM's own NTP client now that it has replaced timesyncd.
# (The optional lab NTP server on srv2, T15, is a separate chronyd with its own config.)
# Prometheus and Grafana run in the VM but are started by start_all.sh (T07), which saves RAM.
step "Configuring services"
systemctl enable --now openvswitch-switch
systemctl enable --now chrony
sync_clock
for svc in frr suricata strongswan isc-dhcp-relay prometheus prometheus-node-exporter grafana-server; do
  if systemctl list-unit-files "$svc.service" --no-legend | grep -q .; then
    systemctl disable --now "$svc" >/dev/null 2>&1 || true
  fi
done
modprobe openvswitch
echo openvswitch > /etc/modules-load.d/openvswitch.conf   # load it on every boot

# --- 4. Apple Silicon CPU workaround -----------------------------------------
# On Apple Silicon the VM's CPU advertises vector features (SVE2 and friends) that the chip
# doesn't really provide. Newer OpenSSL builds (e.g. inside pip's "cryptography" wheel) pick a
# fast path from those flags and crash with "Illegal instruction". OPENSSL_armcap=0 tells
# OpenSSL to ignore the flags and use plain code. /etc/environment applies it to every login
# and sudo session. Only needed on Apple CPUs. See docs/decisions.md D8.
if lscpu | grep -q 'Vendor ID:.*Apple'; then
  step "Apple CPU detected: setting OPENSSL_armcap=0"
  grep -q '^OPENSSL_armcap=' /etc/environment || echo 'OPENSSL_armcap=0' >> /etc/environment
  export OPENSSL_armcap=0
fi

# --- 5. Python venvs -----------------------------------------------------------
as_user mkdir -p "$VENVS"

# Controller: Ryu 4.34 on Python 3.9 with eventlet 0.30.2 (newer eventlet removed an API Ryu
# uses). Ryu's setup.py breaks with setuptools 68+, so pin setuptools first and build Ryu
# without pip's isolated build environment (which would pull in the newest setuptools again).
step "Creating $VENVS/ryu (Ryu controller, Python 3.9)"
[ -x "$VENVS/ryu/bin/python" ] || as_user python3.9 -m venv "$VENVS/ryu"
as_user "$VENVS/ryu/bin/pip" install -q "setuptools==67.6.1" wheel
as_user "$VENVS/ryu/bin/pip" install -q --no-build-isolation \
  "ryu==4.34" "eventlet==0.30.2" networkx pyyaml requests

# Everything else: detection, ML, monitoring, config templates.
step "Creating $VENVS/fortress (requirements.txt, Python 3.10)"
[ -x "$VENVS/fortress/bin/python" ] || as_user python3 -m venv "$VENVS/fortress"
as_user "$VENVS/fortress/bin/pip" install -q --upgrade pip
as_user "$VENVS/fortress/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"

step "Done. Check everything with:  ./scripts/check_env.sh --smoke"
