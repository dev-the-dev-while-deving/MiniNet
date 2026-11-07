#!/usr/bin/env bash
# snapshot.sh <name>: save a restorable snapshot of the "fortress" VM.
# The VM must be stopped to snapshot, so this stops it, snapshots, and starts it again.
# Restore later with:  multipass stop fortress && multipass restore fortress.<name>
set -euo pipefail

name="${1:?usage: $0 <snapshot-name>   e.g. clean-setup, demo-ready}"
multipass stop fortress
multipass snapshot fortress --name "$name"
multipass start fortress
# A stopped VM's clock can come back hours behind; force a fresh NTP sync.
# chrony keeps the clock once setup_vm.sh has run; before that it's systemd-timesyncd.
# The script goes in on stdin because multipass exec flattens its arguments and breaks quoting.
multipass exec fortress -- sudo bash -s <<'EOF'
if command -v chronyc >/dev/null; then
  systemctl start chrony; sleep 2
  chronyc -a 'burst 4/4' >/dev/null; sleep 5; chronyc -a makestep >/dev/null
else
  systemctl restart systemd-timesyncd
fi
echo "VM clock: $(date -u)"
EOF
echo "Snapshot 'fortress.$name' created."
multipass list --snapshots
