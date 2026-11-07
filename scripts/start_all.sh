#!/usr/bin/env bash
# start_all.sh: one command brings up the whole Fortress-Net lab, checks it, and opens the CLI.
# Run from the Mac:  ./scripts/vm/vm.sh sudo ./scripts/start_all.sh [--small] [--check]
#   --small  3 zones and fewer hosts, for 4 GB laptops
#   --check  run the health checks plus the disruptive failover test, then stop (no CLI)
#
# Order (each step needs the ones before it):
#   1. clean slate (stop_all.sh), VM clock, fortress.yaml check
#   2. topology: switches, VLANs, RSTP, routers, hosts           (topology/fortress_topo.py)
#   3. services: FRR (OSPF + VRRP) and NAT, then DHCP/DNS        (topology/services.py)
#   4. health check, then the Mininet CLI                         (topology/checks.py)
# Leave the CLI with "exit" to stop everything; stop_all.sh cleans up after a crash.
# Until the controller exists (T08) the switches run standalone with RSTP.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

echo "==> Clean slate"
./scripts/stop_all.sh --quiet || { echo "stop_all.sh could not clean up; see above" >&2; exit 1; }

echo "==> VM clock"
# The VM clock drifts after a stop/start or Mac sleep (decisions D9); logs need it right.
if command -v chronyc >/dev/null; then chronyc -a makestep >/dev/null 2>&1 || true; fi
date -u

echo "==> Checking config/fortress.yaml"
python3 scripts/check_config.py

echo "==> Starting the lab"
exec python3 topology/fortress_topo.py --standalone "$@"
