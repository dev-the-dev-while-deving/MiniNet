#!/usr/bin/env bash
# vm.sh: run any command inside the project folder in the "fortress" VM.
# Example:  ./scripts/vm/vm.sh sudo python3 topology/fortress_topo.py
# With no arguments it opens an interactive shell in the project folder.
set -euo pipefail

if (( $# == 0 )); then
  exec multipass exec fortress -- bash -lc "cd ~/fortress-net && exec bash"
fi
# The arguments are joined and run by bash in the VM, so pipes work if quoted:
#   ./scripts/vm/vm.sh 'ls | wc -l'
exec multipass exec fortress -- bash -lc "cd ~/fortress-net && $*"
