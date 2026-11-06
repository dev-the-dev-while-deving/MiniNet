#!/usr/bin/env bash
# snapshot.sh <name>: save a restorable snapshot of the "fortress" VM.
# The VM must be stopped to snapshot, so this stops it, snapshots, and starts it again.
# Restore later with:  multipass stop fortress && multipass restore fortress.<name>
set -euo pipefail

name="${1:?usage: $0 <snapshot-name>   e.g. clean-setup, demo-ready}"
multipass stop fortress
multipass snapshot fortress --name "$name"
multipass start fortress
echo "Snapshot 'fortress.$name' created."
multipass list --snapshots
