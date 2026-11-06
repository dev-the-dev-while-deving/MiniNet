#!/usr/bin/env bash
# create_vm.sh: creates the Fortress-Net lab VM with Multipass (run on the Mac).
#
# What it does:
#   1. Installs Multipass with Homebrew if it is missing (asks first).
#   2. Sizes the VM from the Mac's RAM and CPU count.
#   3. Launches an Ubuntu 22.04 VM called "fortress".
#   4. Shares this project folder into the VM at /home/ubuntu/fortress-net.
#   5. Makes sure the VM kernel has the Open vSwitch module.
#   6. Prints the VM's IP (Grafana will be at http://<IP>:3000 later).
#
# Safe to run again: steps that are already done are skipped.
set -euo pipefail

VM=fortress
MOUNT_POINT=/home/ubuntu/fortress-net
# Project folder = two levels up from this script (scripts/vm/ -> project root).
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

# --- 1. Multipass -----------------------------------------------------------
if ! command -v multipass >/dev/null; then
  if ! command -v brew >/dev/null; then
    echo "Homebrew is needed to install Multipass. Install it from https://brew.sh, then re-run." >&2
    exit 1
  fi
  read -r -p "Multipass is not installed. Install it with 'brew install --cask multipass'? [y/N] " ok
  [[ "$ok" == [yY]* ]] || { echo "Aborted."; exit 1; }
  brew install --cask multipass
fi

# --- 2. Pick VM size from the Mac's hardware --------------------------------
ram_gb=$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))
if (( ram_gb >= 16 )); then
  MEM=8G; CPUS=4
else
  MEM=4G; CPUS=2
  echo "NOTE: Mac has ${ram_gb} GB RAM, so the VM gets 4 GB. Run the lab with --small."
fi
echo "Mac: ${ram_gb} GB RAM, $(sysctl -n hw.ncpu) cores -> VM: ${MEM} RAM, ${CPUS} CPUs, 40G disk"

# --- 3. Create the VM (skip if it already exists) ---------------------------
if multipass info "$VM" >/dev/null 2>&1; then
  echo "VM '$VM' already exists, skipping launch."
  multipass start "$VM" 2>/dev/null || true
else
  multipass launch 22.04 --name "$VM" --memory "$MEM" --cpus "$CPUS" --disk 40G
fi

# --- 4. Share the project folder --------------------------------------------
if multipass info "$VM" | grep -q "$MOUNT_POINT"; then
  echo "Project folder already mounted."
else
  # Native mounts are faster but can only be added while the VM is stopped.
  # If native isn't supported, fall back to the default (sshfs) type on a running VM.
  multipass stop "$VM"
  if multipass mount --type native "$PROJECT_DIR" "$VM:$MOUNT_POINT"; then
    multipass start "$VM"
  else
    multipass start "$VM"
    multipass mount "$PROJECT_DIR" "$VM:$MOUNT_POINT"
  fi
fi

# --- 5. Open vSwitch kernel module ------------------------------------------
if ! multipass exec "$VM" -- sudo modprobe openvswitch; then
  echo "openvswitch module missing; installing extra kernel modules and rebooting the VM..."
  multipass exec "$VM" -- bash -c 'sudo apt-get update -q && (sudo apt-get install -y "linux-modules-extra-$(uname -r)" || sudo apt-get install -y linux-generic)'
  multipass restart "$VM"
  multipass exec "$VM" -- sudo modprobe openvswitch
fi
echo "Open vSwitch kernel module: OK"

# --- 6. Show the VM's IP ----------------------------------------------------
ip=$(multipass info "$VM" | awk '/IPv4/ {print $2; exit}')
echo
echo "VM '$VM' is ready at $ip"
echo "  Run commands in it:  ./scripts/vm/vm.sh <command>"
echo "  Grafana (later):     http://$ip:3000"
