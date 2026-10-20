#!/bin/bash
# Capacity test: how many players this host really holds. Runs ON the host
# (make capacity copies it there and runs it).
#
# Starts load-N boxes from the live image on the players' profile, in steps,
# each with a player logged in the way the spawner does it (incus exec -> su ->
# tmux, held open by a host-side client like ttyd's). After each step it
# records how long boxes took to come up, free memory, the boxes' memory, pool use, load and how fast the
# spawner still answers; at the end every box runs a filesystem search at
# once, the worst moment a busy evening could bring. Then it suggests a
# MAX_PLAYERS.
#
# The load boxes never go through the spawner, so nothing reaches the
# leaderboard, the events table or the player metrics (Grafana's host panels,
# if any, will show the memory spike). The spawner's orphan sweep only deletes
# p-* boxes, so it leaves these alone.
#
# Safety: refuses to start if anyone is playing, stops early if a player
# arrives or free memory drops under MIN_FREE_MB, and deletes every load box
# on any exit.
#
#   bash capacity.sh [TARGET=60]      STEP=10 MIN_FREE_MB=3072
set -uo pipefail
TARGET=${1:-60}
STEP=${STEP:-10}
MIN_FREE_MB=${MIN_FREE_MB:-3072}
IMAGE=${IMAGE:-handover}
PROFILE=${PROFILE:-isolated}
LAUNCH_LOCK=${LAUNCH_LOCK:-/run/lock/handover-launch}
# the profile's limits.memory, in MiB (e.g. 320MiB, 1GiB)
BOX_LIMIT_MB=$(incus profile get "$PROFILE" limits.memory | awk '/GiB$/{print int($1) * 1024; next} {print int($1)}')
OUT=~/capacity-$(date -u +%Y%m%dT%H%MZ).txt
WORK=$(mktemp -d)
HOLDERS=()
starting=()

say() { echo "$*" | tee -a "$OUT"; }
avail_mb() { awk '/^MemAvailable/{print int($2/1024)}' /proc/meminfo; }
total_mb() { awk '/^MemTotal/{print int($2/1024)}' /proc/meminfo; }
health() { curl -s -m 5 localhost:8080/healthz; }
players() { health | python3 -c 'import json, sys; print(json.load(sys.stdin)["players"])' 2>/dev/null || echo "?"; }
health_ms() { curl -s -m 10 -o /dev/null -w '%{time_total}' localhost:8080/healthz | awk '{printf "%.1f", $1 * 1000}'; }
pool_used() { incus storage info games | awk '/space used/{print $3}'; }
load_boxes() { incus list -c n -f csv | grep '^load-' || true; }
boxes_mb() {
  incus list -f json | python3 -c '
import json, sys
print(int(sum((i.get("state") or {}).get("memory", {}).get("usage", 0)
              for i in json.load(sys.stdin) if i["name"].startswith("load-")) / 2**20))'
}

cleanup() {
  local n
  # Boxes still being created when the run is stopped appear only after the
  # first pass, and a box mid-start can't be deleted yet: let the creates
  # finish, then delete until none are left (at most ~2 min).
  wait "${starting[@]}" 2>/dev/null
  n=$(load_boxes | wc -l)
  [ "$n" -gt 0 ] && echo "cleaning up $n load boxes..."
  for _ in $(seq 24); do
    load_boxes | xargs -r -n1 -P8 incus delete -f 2>/dev/null
    [ -z "$(load_boxes)" ] && break
    sleep 5
  done
  for p in "${HOLDERS[@]}"; do pkill -P "$p" 2>/dev/null; kill "$p" 2>/dev/null; done
  pkill -f '[i]ncus exec load-' 2>/dev/null
  rm -rf "$WORK"
  [ -n "$(load_boxes)" ] && echo "WARNING: load boxes left behind: $(load_boxes | tr '\n' ' ')"
  return 0
}
trap cleanup EXIT
trap 'say "interrupted"; exit 130' INT TERM

# One box: start it, wait for systemd, log a player in and keep the session
# open from the host. Writes its seconds to ready from the click and from the end
# of its launch (or FAIL) to $WORK/<name>.
start_box() {
  local c=$1 t0=$SECONDS t1 ok=0 st=""
  # The spawner's launch lock: Incus 6.0 races on simultaneous creates (see
  # LAUNCH_LOCK in spawner/common.py). NIC-less launch: non-zero is expected.
  flock "$LAUNCH_LOCK" incus launch "$IMAGE" "$c" --profile "$PROFILE" >/dev/null 2>&1
  t1=$SECONDS
  for _ in $(seq 60); do
    st=$(incus exec "$c" -- systemctl is-system-running 2>&1 </dev/null)
    case $st in running|degraded) ok=1; break ;; esac
    sleep 1
  done
  [ "$ok" = 1 ] || { echo "FAIL ${st:0:80}" > "$WORK/$c"; return; }
  incus exec "$c" -- hostnamectl set-hostname handover </dev/null
  echo "$((SECONDS - t0)) $((SECONDS - t1))" > "$WORK/$c"     # click to ready, launched to ready
}

hold_session() {
  local c=$1
  # tmux needs a real terminal type; ttyd gives xterm-256color.
  ( sleep infinity | TERM=xterm-256color script -qfc "incus exec $c -- su - level0 -c 'tmux new-session -A -s play'" /dev/null \
      >/dev/null 2>&1 ) &
  HOLDERS+=($!)
}

sessions_up() {
  local n=0 c
  for c in $(load_boxes); do
    incus exec "$c" -- su - level0 -c 'tmux has-session -t play' >/dev/null 2>&1 </dev/null && n=$((n + 1))
  done
  echo "$n"
}

# ---- preflight ---------------------------------------------------------------
p=$(players)
if [ "$p" != 0 ]; then
  echo "refusing: the spawner reports players=$p (or isn't answering). Run this while nobody plays."
  exit 1
fi
[ -n "$(load_boxes)" ] && { echo "leftover load boxes from an earlier run; removing them"; cleanup; WORK=$(mktemp -d); }

BASE_AVAIL=$(avail_mb)
TOTAL=$(total_mb)
say "capacity test $(date -u '+%F %T UTC'), image $(incus image info "$IMAGE" | awk '/^Fingerprint/{print substr($2, 1, 12)}')"
say "host: $(nproc) cores, ${TOTAL} MiB RAM, ${BASE_AVAIL} MiB available before; target $TARGET in steps of $STEP; stop under ${MIN_FREE_MB} MiB free"
say ""
say "$(printf '%-6s %-9s %-10s %-7s %-11s %-10s %-9s %-6s %-10s %s' boxes sessions 'ready s' 'boot s' 'avail MiB' 'boxes MiB' 'pool' load 'spawner ms' note)"

n=0
stopped=""
while [ "$n" -lt "$TARGET" ]; do
  want=$(( n + STEP > TARGET ? TARGET : n + STEP ))
  starting=()
  for i in $(seq $((n + 1)) "$want"); do start_box "load-$i" & starting+=($!); done
  wait "${starting[@]}"                              # not the session holders: they never end
  for i in $(seq $((n + 1)) "$want"); do hold_session "load-$i"; done
  this=$(seq -f "$WORK/load-%g" $((n + 1)) "$want")
  n=$want
  sleep 20                                           # let memory settle after the burst
  # shellcheck disable=SC2086
  ready=$(cat $this | grep -v FAIL | sort -n | awk '{a[NR]=$1} END{printf "%s-%s", a[1], a[NR]}')
  # shellcheck disable=SC2086
  boot=$(cat $this | grep -v FAIL | awk '$2 > m{m=$2} END{print m+0}')
  # shellcheck disable=SC2086
  fails=$(cat $this | grep -c FAIL)
  av=$(avail_mb)
  note=""
  # shellcheck disable=SC2086
  [ "$fails" -gt 0 ] && note="$fails never ready: $(cat $this | grep FAIL | sort | uniq -c | head -2 | tr -s ' \n' ' ')"
  say "$(printf '%-6s %-9s %-10s %-7s %-11s %-10s %-9s %-6s %-10s %s' "$n" "$(sessions_up)" "$ready" "$boot" "$av" "$(boxes_mb)" \
        "$(pool_used)" "$(cut -d' ' -f1 /proc/loadavg)" "$(health_ms)" "$note")"
  p=$(players)
  if [ "$p" != 0 ]; then stopped="a real player arrived (players=$p)"; break; fi
  if [ "$av" -lt "$MIN_FREE_MB" ]; then stopped="free memory under ${MIN_FREE_MB} MiB"; break; fi
done
[ -n "$stopped" ] && say "stopped early at $n boxes: $stopped"

# ---- everyone busy at once ----------------------------------------------------
if [ -z "$stopped" ]; then
  say ""
  say "all $n boxes searching the filesystem at once:"
  t0=$(date +%s.%N)
  busy=()
  for c in $(load_boxes); do
    incus exec "$c" -- su - level0 -c 'find / -xdev -name "*.conf" 2>/dev/null | wc -l' >/dev/null 2>&1 </dev/null &
    busy+=($!)
  done
  worst_ms=0 min_av=$(avail_mb)
  running() { for b in "${busy[@]}"; do kill -0 "$b" 2>/dev/null && return 0; done; return 1; }
  while running; do
    ms=$(health_ms); awk -v a="$ms" -v b="$worst_ms" 'BEGIN{exit !(a > b)}' && worst_ms=$ms
    av=$(avail_mb); [ "$av" -lt "$min_av" ] && min_av=$av
    sleep 1
  done
  say "  took $(awk -v a="$t0" -v b="$(date +%s.%N)" 'BEGIN{printf "%.1f", b - a}') s; spawner answered within ${worst_ms} ms; lowest free memory ${min_av} MiB"
fi

# ---- suggestion ------------------------------------------------------------------
av=$(avail_mb)
per_box=$(( (BASE_AVAIL - av) / (n > 0 ? n : 1) ))
fit_measured=$(( n + (av - MIN_FREE_MB) / (per_box > 0 ? per_box : 1) ))
fit_limit=$(( (BASE_AVAIL - MIN_FREE_MB) / BOX_LIMIT_MB ))
say ""
say "measured: ${per_box} MiB per box with a player logged in -> about $fit_measured boxes before free memory reaches ${MIN_FREE_MB} MiB"
say "worst case: every box at its ${BOX_LIMIT_MB} MiB limit -> $fit_limit boxes"
say "suggested MAX_PLAYERS: $(( fit_limit < fit_measured ? fit_limit : fit_measured )) (the lower of the two; nothing swaps, so running out of memory kills processes)"
say "results saved to $OUT"
