#!/usr/bin/env bash
# stop_all.sh: stops the Fortress-Net lab and removes everything it may have left behind.
# Run from the Mac:  ./scripts/vm/vm.sh sudo ./scripts/stop_all.sh
#
# Normally you leave the lab by typing "exit" in the Mininet CLI, which stops it cleanly.
# This script is for everything else: a crash, a killed terminal, or a clean slate before
# start_all.sh. It is safe to run when nothing is running. It ends by checking that nothing
# is left: no lab daemons, no OVS bridges, no lab interfaces. Exit code 0 = clean.
set -uo pipefail
cd "$(dirname "$0")/.."

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi
quiet=false; [[ "${1:-}" == "--quiet" ]] && quiet=true
say() { $quiet || echo "$@"; }

# 1. A lab still running in another terminal: ask it to stop. (The [t] keeps pkill from
#    matching this script's own command line.)
if pkill -f "[t]opology/fortress_topo.py"; then
  say "Stopping a running lab..."
  sleep 3
  pkill -9 -f "[t]opology/fortress_topo.py" 2>/dev/null
fi

# 2. Our daemons (FRR, dnsmasq, relays, DHCP clients) and the leftover interfaces.
say "$(python3 topology/fortress_topo.py --cleanup 2>&1 | tail -1)"

# 3. Mininet's own clean-up: OVS bridges, its interfaces, its helper processes.
mn -c >/dev/null 2>&1

# 4. Prove it's clean.
left=0
procs=$(pgrep -a -f "[r]syslogd -f /etc/fortress|[s]shd -f /etc/fortress|[c]haron-systemd|[r]yu-manager|[w]atchfrr|/usr/lib/frr/|[d]nsmasq --conf-file=/etc/fortress|[d]hcrelay|[b]usybox udhcpc|[m]nexec" || true)
bridges=$(ovs-vsctl list-br)
intfs=$(ip -o link show | grep -oE ': (s[0-9]+|c[0-9]+|mon0)-[^:@]+' || true)
if [[ -n "$procs" ]];   then echo "❌ lab processes still running:"; echo "$procs"; left=1; else say "✅ no lab processes running"; fi
if [[ -n "$bridges" ]]; then echo "❌ OVS bridges left: $bridges"; left=1; else say "✅ no OVS bridges"; fi
if [[ -n "$intfs" ]];   then echo "❌ lab interfaces left:$intfs"; left=1; else say "✅ no lab interfaces left"; fi
exit $left
