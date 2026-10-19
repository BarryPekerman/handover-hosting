#!/usr/bin/env bash
# Runs ON THE HOST: start a throwaway player box from the live image with the
# real profile and check it's a working game box. Used by rebuild.yml.
set -euo pipefail
c=smoke-$(date +%s)
trap 'incus delete -f "$c" >/dev/null 2>&1 </dev/null || true' EXIT
incus launch handover "$c" --profile isolated >/dev/null 2>&1 </dev/null || true
s=""
for _ in $(seq 60); do
  s=$(incus exec "$c" -- systemctl is-system-running 2>/dev/null </dev/null || true)
  case $s in running|degraded) break ;; esac
  sleep 2
done
nics=$(incus exec "$c" -- ls /sys/class/net </dev/null | tr '\n' ' ')
svcs=$(incus exec "$c" -- systemctl is-active dave-cache dave-daemon dave-worker dave-race level3-backup </dev/null | grep -c '^active' || true)
echo "player box: $s | interfaces: $nics| game services: $svcs/5"
[[ $s == running || $s == degraded ]] && [[ $nics == "lo " ]] && [[ $svcs == 5 ]]
