#!/bin/bash
# One box's footprint while the whole game is played in it: memory (cgroup
# current, peak, what it is, whether anything was killed) and disk (btrfs
# exclusive). Runs ON the host, next to solutions.sh: the solution() function
# from bandit-wannabe's tests/solve.sh (spoilers, so it isn't copied here):
#
#   sed -n '/^solution() {/,/^}/p' ../bandit-wannabe/tests/solve.sh > /tmp/solutions.sh
#   scp scripts/footprint.sh /tmp/solutions.sh handover:
#   ssh handover 'MEM=256MiB bash footprint.sh </dev/null; rm -f footprint.sh solutions.sh'
#
# MEM (optional) overrides the profile's memory limit for this box only. Each
# level's intended solution runs as that level's user via incus exec, as root,
# so no passwords are needed. The box never goes through the spawner, so
# nothing reaches the leaderboard or the analytics; it refuses to run while
# anyone plays and deletes its box on exit.
set -uo pipefail
cd "$(dirname "$0")" || exit 1
# shellcheck source=/dev/null
source ./solutions.sh
c=load-footprint
LAUNCH_LOCK=/run/lock/handover-launch
mib() { awk '{printf "%d", $1 / 1048576}'; }
cg() { incus exec "$c" -- cat "/sys/fs/cgroup/$1" </dev/null; }
disk() { sudo btrfs qgroup show -reF --raw "/var/lib/incus/storage-pools/games/containers/$c" 2>/dev/null \
           | awk 'NR > 2 && $1 ~ /^0\// {printf "%d", $3 / 1048576; exit}'; }
holder=""
cleanup() { [ -n "$holder" ] && { pkill -P "$holder"; kill "$holder"; } 2>/dev/null; incus delete -f "$c" >/dev/null 2>&1; true; }
trap cleanup EXIT

p=$(curl -s -m 5 localhost:8080/healthz | python3 -c 'import json,sys; print(json.load(sys.stdin)["players"])' 2>/dev/null)
[ "$p" = 0 ] || { echo "refusing: players=$p"; exit 1; }

flock "$LAUNCH_LOCK" incus launch handover "$c" --profile isolated ${MEM:+-c limits.memory=$MEM} >/dev/null 2>&1
until [ "$(incus exec "$c" -- systemctl is-system-running 2>/dev/null </dev/null)" = running ]; do sleep 1; done
incus exec "$c" -- hostnamectl set-hostname handover </dev/null
sleep 10
echo "booted:            current $(cg memory.current | mib) MiB, peak $(cg memory.peak | mib) MiB, disk $(disk) MiB"
( sleep infinity | TERM=xterm-256color script -qfc "incus exec $c -- su - level0 -c 'tmux new-session -A -s play'" /dev/null >/dev/null 2>&1 ) &
holder=$!
sleep 5
echo "player logged in:  current $(cg memory.current | mib) MiB, peak $(cg memory.peak | mib) MiB"

solved=0 worst=0 worst_level=""
for n in $(seq 0 19); do
  # the login banner comes first; the answer is the last 32-character word
  out=$(incus exec "$c" -- su - "level$n" -c "$(solution "$n")" 2>/dev/null </dev/null | grep -oE '\b[A-Za-z0-9]{32}\b' | tail -1)
  [ -n "$out" ] && solved=$((solved + 1))
  pk=$(cg memory.peak | mib)
  [ "$pk" -gt "$worst" ] && { worst=$pk; worst_level=$n; }
  printf 'level %-2s  current %4s MiB  peak so far %4s MiB\n' "$n" "$(cg memory.current | mib)" "$pk"
done
echo "solved $solved/20 (a solution prints a 32-character password)"
echo "whole game played: peak $(cg memory.peak | mib) MiB (reached by level $worst_level), now $(cg memory.current | mib) MiB, disk $(disk) MiB"
echo "limits: memory $(cg memory.max | mib) MiB; swap $(cg memory.swap.max 2>/dev/null)"
echo
echo "what the memory is (memory.stat, MiB):"
cg memory.stat | awk '$1 ~ /^(anon|file|kernel|shmem|sock)$/ {printf "  %-7s %d\n", $1, $2 / 1048576}'
echo "processes by size (RSS MiB, count):"
incus exec "$c" -- ps -eo rss=,args= </dev/null | awk '{rss=$1; $1=""; sub(/^ /, ""); k=$0; sub(/ .*/, "", k); if ($0 ~ /systemd --user/) k="systemd --user"; if ($0 ~ /sd-pam/) k="(sd-pam)"; s[k]+=rss; n[k]++} END {for (k in s) printf "  %5d MiB  %3d x %s\n", s[k]/1024, n[k], k}' | sort -rn | head -8
echo "memory.events: $(cg memory.events | tr '\n' ' ')"
