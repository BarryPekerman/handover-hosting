#!/bin/bash
# End-to-end spawner check against bandit-wannabe's Vagrant VM (make local-up
# there first): real spawner + ttyd + tmux, with a fake `incus` that forwards
# into the VM. Runs the seven scenarios in scenarios.sh, then cleans up.
#   PYTHON=<python with aiohttp>  BANDIT=<path to bandit-wannabe>  tests/vm/run.sh
set -euo pipefail
HERE=$(dirname "$(readlink -f "$0")")
BANDIT=${BANDIT:-$HERE/../../../bandit-wannabe}
export PYTHON=${PYTHON:-python3}
HANDOVER_VM_STATE=$(mktemp -d)
export HANDOVER_VM_STATE
export HANDOVER_VM_SSH_CONFIG=$HANDOVER_VM_STATE/ssh.cfg
(cd "$BANDIT" && vagrant ssh-config 2>/dev/null) > "$HANDOVER_VM_SSH_CONFIG"
grep -q "^Host handover" "$HANDOVER_VM_SSH_CONFIG" || { echo "no VM: run 'make local-up' in $BANDIT"; exit 1; }
echo 11111111 > "$HANDOVER_VM_STATE/fp"
cleanup() { kill "$SPAWNER" 2>/dev/null; pkill -f "ttyd -p 819[0-9] .*$HANDOVER_VM_STATE" 2>/dev/null; pkill -f "[t]tyd -p 819" 2>/dev/null; rm -rf "$HANDOVER_VM_STATE"; }
trap cleanup EXIT
PATH=$HERE/bin:$PATH SESSION_SECRET=testsecret LISTEN_PORT=8099 TTYD_PORT_BASE=8190 METRICS_PORT=9199 \
  STATE_FILE=$HANDOVER_VM_STATE/state.json LEADERBOARD_DB=$HANDOVER_VM_STATE/lb.db \
  "$PYTHON" "$HERE/../../spawner/spawner.py" > "$HANDOVER_VM_STATE/spawner.log" 2>&1 &
SPAWNER=$!
for _ in $(seq 30); do curl -s http://127.0.0.1:8099/healthz >/dev/null && break; sleep 1; done
"$HERE/scenarios.sh"
