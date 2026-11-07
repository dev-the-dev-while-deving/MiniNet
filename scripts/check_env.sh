#!/usr/bin/env bash
# check_env.sh: prints ✅ / ❌ for every tool the lab needs, with its version.
# Run from the Mac:  ./scripts/vm/vm.sh ./scripts/check_env.sh [--smoke]
#   --smoke  also starts the Ryu controller (simple_switch_13) and runs Mininet's sample
#            topology with pingall against it. Needs sudo; takes about 30 s.
# Exit code = number of failed checks (0 = all green).
set -uo pipefail

RYU="$HOME/venvs/ryu"
FORT="$HOME/venvs/fortress"
SYSPY=/usr/bin/python3      # the system Python, which Mininet uses
fails=0

ok()  { printf '✅ %-22s %s\n' "$1" "$2"; }
bad() { printf '❌ %-22s %s\n' "$1" "$2"; fails=$((fails + 1)); }

# check <label> <command...>: passes if the command succeeds; shows the first line it prints.
# Every check gets a 30 s time limit so one stuck tool can't hang the whole script.
check() {
  local label=$1; shift
  local out
  if out=$(timeout 30 "$@" 2>&1); then ok "$label" "$(head -n1 <<<"$out")"; else bad "$label" "$(head -n1 <<<"$out")"; fi
}
# pkg <label> <debian-package>: passes if the package is installed; shows its version.
pkg() { check "$1" dpkg-query -W -f='${Package} ${Version}\n' "$2"; }

echo "== Network emulation"
check "Mininet"            mn --version
check "Open vSwitch"       ovs-vsctl --version
check "OVS service"        sudo ovs-vsctl show
check "OVS kernel module"  bash -c 'lsmod | grep -q "^openvswitch" && echo loaded'

echo "== Routing (FRR)"
pkg   "FRR package"        frr
for d in zebra ospfd vrrpd staticd; do
  check "FRR $d"           bash -c "/usr/lib/frr/$d --version | head -1"
done
check "vtysh"              bash -c 'command -v vtysh'

echo "== Services"
check "dnsmasq"            dnsmasq --version
check "rsyslog"            rsyslogd -v
pkg   "strongSwan swanctl" strongswan-swanctl   # swanctl --version needs the daemon running
pkg   "OpenSSH server"     openssh-server
check "chrony (optional)"  chronyd --version

echo "== Detection and tools"
check "Suricata"           suricata -V
check "nmap"               bash -c 'nmap --version | head -1'
check "iperf3"             bash -c 'iperf3 --version | head -1'
check "tcpdump"            bash -c 'tcpdump --version 2>&1 | head -1'
check "tshark"             bash -c 'tshark --version | head -1'
check "tree"               tree --version
check "arping"             bash -c "arping -V 2>&1 | head -1"
pkg   "DHCP relay"         isc-dhcp-relay
check "dhclient"           bash -c "command -v dhclient"
check "dig"                bash -c "dig -v 2>&1 | head -1"

echo "== Monitoring"
check "Prometheus"         bash -c 'prometheus --version 2>&1 | head -1'
check "Grafana"            grafana server -v

echo "== Python: system (Mininet topology)"
check "python3"            "$SYSPY" --version
check "PyYAML"             "$SYSPY" -c 'import yaml; print("yaml", yaml.__version__)'
check "Mininet module"     "$SYSPY" -c 'import mininet.net; print("import mininet OK")'

echo "== Python: ~/venvs/ryu (controller)"
check "python"             "$RYU/bin/python" --version
check "ryu-manager"        bash -c "test -x '$RYU/bin/ryu-manager' && echo '$RYU/bin/ryu-manager'"
for mod in ryu eventlet networkx yaml requests; do
  check "py: $mod"         "$RYU/bin/python" -W ignore -c "import $mod; print(getattr($mod, '__version__', 'ok'))"
done

echo "== Python: ~/venvs/fortress (detection, monitoring)"
check "python"             "$FORT/bin/python" --version
# Import names differ from pip names for some packages (scikit-learn -> sklearn).
for mod in yaml jinja2 networkx sklearn pandas joblib scapy flask requests prometheus_client matplotlib; do
  check "py: $mod"         "$FORT/bin/python" -W ignore -c "import $mod; print(getattr($mod, '__version__', 'ok'))"
done

echo "== VM clock"
check "chrony running"     bash -c 'systemctl is-active chrony'
check "clock synced"       bash -c 'chronyc tracking | grep -E "^Leap status +: Normal" && chronyc tracking | grep "System time"'

echo "== Apple Silicon workaround"
if lscpu | grep -q 'Vendor ID:.*Apple'; then
  check "OPENSSL_armcap=0"  bash -c 'grep -q "^OPENSSL_armcap=0" /etc/environment && echo "set in /etc/environment"'
else
  ok "OPENSSL_armcap"      "not an Apple CPU, not needed"
fi

# --- Optional smoke test: controller + Mininet sample topology ---------------
if [[ "${1:-}" == "--smoke" ]]; then
  echo "== Smoke test: Ryu simple_switch_13 + 'mn --topo tree,depth=2' pingall"
  timeout 60 sudo mn -c >/dev/null 2>&1   # clear anything a previous run left behind
  log=$(mktemp)
  "$RYU/bin/ryu-manager" --ofp-tcp-listen-port 6653 ryu.app.simple_switch_13 >"$log" 2>&1 &
  ctrl=$!          # kill exactly this PID afterwards (pkill -f could match this script too)
  sleep 5
  if ! kill -0 "$ctrl" 2>/dev/null; then
    bad "controller start" "$(tail -n1 "$log")"
  else
    ok "controller start" "ryu-manager pid $ctrl listening on port 6653"
    out=$(timeout 120 sudo mn --controller=remote,ip=127.0.0.1,port=6653 \
            --switch ovs,protocols=OpenFlow13 --topo tree,depth=2 --test pingall 2>&1)
    result=$(grep -m1 'Results:' <<<"$out")
    if grep -q 'Results: 0% dropped' <<<"$out"; then ok "pingall" "$result"; else bad "pingall" "${result:-no result (timed out?)}"; fi
  fi
  kill "$ctrl" 2>/dev/null; wait "$ctrl" 2>/dev/null
  timeout 60 sudo mn -c >/dev/null 2>&1   # always clean up leftover switches and links
  rm -f "$log"
fi

echo
if (( fails == 0 )); then echo "All checks passed."; else echo "$fails check(s) failed."; fi
exit "$fails"
