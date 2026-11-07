#!/usr/bin/env bash
# demo.sh: run the 10-minute viva demo (T26). Run inside the VM as root, from the Mac:
#     ./scripts/vm/vm.sh sudo ./scripts/demo.sh            # live, pauses for Enter between steps
#     ./scripts/vm/vm.sh sudo ./scripts/demo.sh --auto     # timed rehearsal, no pauses
# It clears any leftovers first (like start_all.sh), then hands over to scripts/demo.py.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $EUID -ne 0 ]]; then
    echo "demo.sh must run as root: ./scripts/vm/vm.sh sudo ./scripts/demo.sh" >&2
    exit 1
fi
./scripts/stop_all.sh --quiet >/dev/null 2>&1 || true     # clean slate
chronyc -a makestep >/dev/null 2>&1 || true               # clock right for log timestamps (D9)
exec python3 scripts/demo.py "$@"
