#!/bin/bash
# The nine end-to-end scenarios; run through run.sh (which starts the spawner).
# Each check prints ok or FAIL with what happened and what should; the script
# exits non-zero if any check failed.
# shellcheck disable=SC2329  # the is_*/saved7/... checks are called through wait_for
set -uo pipefail
HERE=$(dirname "$(readlink -f "$0")")
cd "$HANDOVER_VM_STATE" || exit 1
U=http://127.0.0.1:8099
PY=${PYTHON:-python3}
FAILS=0

hid()   { awk '/\thid\t/{print $7}' "$1"; }
# wait_for <seconds> <command...>: retry once a second; false if it never succeeds.
# Every wait has a limit, so a stuck spawner fails a check instead of hanging the run.
wait_for() { local t=$1; shift; for _ in $(seq "$t"); do "$@" && return 0; sleep 1; done; return 1; }
is_ready() { curl -s "$U/ready?sid=$1" | grep -q ready; }
ready() { wait_for 90 is_ready "$1" || report FAIL "box ${1:0:12}" "not ready after 90 s"; }
field() { "$PY" -c "import json, sys; print(json.load(sys.stdin)$1)"; }
# check <label> <got> <want>: exact match.  check_min <label> <got> <min>: number >= min.
report() { printf '%-4s %-18s %s\n' "$1" "$2" "$3"; [ "$1" = ok ] || FAILS=$((FAILS + 1)); }
check() { if [ "$2" = "$3" ]; then report ok "$1" "$2"; else report FAIL "$1" "got: $2   want: $3"; fi; }
check_min() { if [ "${2:-0}" -ge "$3" ] 2>/dev/null; then report ok "$1" "$2"; else report FAIL "$1" "got: $2   want: >= $3"; fi; }
GH=$(cd "$HERE/../../spawner" && SESSION_SECRET=testsecret "$PY" -c \
     "import auth; print(auth._sign({'uid': 42, 'login': 'alice'}))")

# 1. A fresh box saves the passwords of the levels reached (the VM's login
#    history has su logins at levels 2 and 7).
curl -s -c A.jar -d mode=fresh "$U/play" >/dev/null; A=$(hid A.jar); ready "$A"
saved7() { curl -s -b A.jar "$U/save/$A" | grep -q '"7"'; }
wait_for 60 saved7
P7=$(curl -s -b A.jar "$U/save/$A" | field '["levels"]["7"]')
check "1 fresh + save" "level 7 saved, ${#P7} chars" "level 7 saved, 32 chars"

# 2. Resume with one real and one forged password: start at the real one.
curl -s -c B.jar -d mode=resume --data-urlencode "save={\"levels\":{\"7\":\"$P7\",\"20\":\"nope\"}}" \
     "$U/play" >/dev/null; B=$(hid B.jar); ready "$B"
got=$(grep "provisioned sid=${B:0:12}" spawner.log | grep -o 'start=level[0-9]* dropped=[0-9]*')
check "2 resume" "$got" "start=level7 dropped=1"

# 3. A second device signed in to the same account lands in the same box.
curl -s -c C.jar -b "ghid=$GH" -d mode=fresh "$U/play" >/dev/null; C=$(hid C.jar); ready "$C"
r=$(curl -s -b "ghid=$GH" -d mode=fresh -o /dev/null -w '%{redirect_url}' "$U/play")
check "3 account box" "$([ "${r##*/}" = "$C" ] && echo same || echo different) box" "same box"

# 4. Both devices see one tmux screen.
check "4 shared screen" "$("$PY" "$HERE/twodev.py" "$GH" 2>&1 | tail -1 | grep -o "B saw A's command output: .*")" \
      "B saw A's command output: True"

# 5. An image deploy: a reload is sent to the landing page, Play upgrades the
#    box at the level reached.
echo 22222222 > fp
outdated() { [ "$(curl -s -o /dev/null -w '%{http_code}' -b B.jar "$U/app/$B")" = 302 ]; }
wait_for 60 outdated                                 # the spawner looks for a new image every 30 s
check "5 image update" "reload -> $(curl -s -o /dev/null -w '%{http_code}' -b B.jar "$U/app/$B"), notice $(curl -s -b B.jar "$U/" | grep -c 'got an update')" \
      "reload -> 302, notice 1"
provisions() { grep -c "provisioned sid=${B:0:12}" spawner.log; }
before=$(provisions)
reprovisioned() { [ "$(provisions)" -gt "$before" ]; }
curl -s -c B.jar -b B.jar -d mode=fresh "$U/play" >/dev/null; ready "$B"; wait_for 15 reprovisioned
# The new box may be a standby one (w-*), so take its name from the log.
line=$(grep "provisioned sid=${B:0:12}" spawner.log | tail -1)
ct=$(grep -o 'ct=[^ ]*' <<<"$line" | cut -d= -f2)
check "  upgraded" "$(grep -o 'start=level[0-9]*' <<<"$line") on image $(cat "base-$ct")" "start=level7 on image 22222222"

# 6. Ranking: every run here is unranked, because the VM's history skips
#    levels (a real box always records level0 first) and B also resumed.
ranked=$("$PY" -c "import sqlite3; print(sqlite3.connect('lb.db').execute('select count(*) from players where unranked=0').fetchone()[0])")
check "6 leaderboard" "ranked runs: $ranked" "ranked runs: 0"

# 7. Metrics: the funnel counts levels reached, on the localhost-only port.
#    Three boxes are live: A, B (upgraded in place) and the account's C.
m=$(curl -s http://127.0.0.1:9199/metrics)
check_min "7 levels counted" "$(grep '^handover_level_reached_total' <<<"$m" | grep -vc ' 0$')" 1
check "  players" "$(grep '^handover_players ' <<<"$m" | cut -d' ' -f2)" 3
check "  play port" "metrics -> $(curl -s -o /dev/null -w '%{http_code}' "$U/metrics")" "metrics -> 404"

# 8. Events: the boxes' starts, reaches and resumes land in the events table,
#    and logged commands (the VM's journal has level0's test commands, if any)
#    are counted in the metrics too.
kinds=$("$PY" -c "import sqlite3; print(' '.join(sorted(k for k, in sqlite3.connect('lb.db').execute('select distinct kind from events') if k in ('start', 'reach', 'resume'))))")
check "8 events" "$kinds" "reach resume start"
ncmd=$("$PY" -c "import sqlite3; print(sqlite3.connect('lb.db').execute(\"select count(*) from events where kind='cmd'\").fetchone()[0])")
if [ "$ncmd" -gt 0 ]; then
  check_min "  commands" "$(grep -c '^handover_commands_total' <<<"$m")" 1
fi
# 9. Standby boxes (WARM_SPARES=1): one is started at once, players get it
#    without a create, each handout starts a replacement, and the image
#    update above replaced the one on the old image.
check_min "9 standby handed out" "$(grep -c 'gets standby box' spawner.log)" 1
check_min "  standby started" "$(grep -c 'standby box w-[0-9a-f]* ready' spawner.log)" 2
check "  old image dropped" "$(grep -c 'is on the old image' spawner.log)" 1
has_spare() { [ "$(curl -s http://127.0.0.1:9199/metrics | grep '^handover_spares ' | cut -d' ' -f2)" = 1 ]; }
check "  one waiting" "$(wait_for 30 has_spare && echo 1 || echo 0) standby box" "1 standby box"
check "tracebacks" "$(grep -c Traceback spawner.log)" 0

echo; [ "$FAILS" -eq 0 ] && echo "all checks passed" || echo "$FAILS check(s) failed"
exit "$FAILS"
