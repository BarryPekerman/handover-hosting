#!/bin/bash
# The eight end-to-end scenarios; run through run.sh (which starts the spawner).
# Each line prints what happened next to what should happen.
set -uo pipefail
HERE=$(dirname "$(readlink -f "$0")")
cd "$HANDOVER_VM_STATE" || exit 1
U=http://127.0.0.1:8099
PY=${PYTHON:-python3}

hid()   { awk '/\thid\t/{print $7}' "$1"; }
ready() { until curl -s "$U/ready?sid=$1" | grep -q ready; do sleep 1; done; }
field() { "$PY" -c "import json, sys; print(json.load(sys.stdin)$1)"; }
GH=$(cd "$HERE/../../spawner" && SESSION_SECRET=testsecret "$PY" -c \
     "import spawner; print(spawner._sign({'uid': 42, 'login': 'alice'}))")

# 1. A fresh box saves the passwords of the levels reached (the VM's login
#    history has su logins at levels 2 and 7).
curl -s -c A.jar -d mode=fresh "$U/play" >/dev/null; A=$(hid A.jar); ready "$A"
until curl -s -b A.jar "$U/save/$A" | grep -q '"7"'; do sleep 3; done
P7=$(curl -s -b A.jar "$U/save/$A" | field '["levels"]["7"]')
echo "1 fresh + save:   level 7 saved, ${#P7} chars              (want: 32 chars)"

# 2. Resume with one real and one forged password: start at the real one.
curl -s -c B.jar -d mode=resume --data-urlencode "save={\"levels\":{\"7\":\"$P7\",\"20\":\"nope\"}}" \
     "$U/play" >/dev/null; B=$(hid B.jar); ready "$B"
got=$(grep "provisioned sid=${B:0:12}" spawner.log | grep -o 'start=level[0-9]* dropped=[0-9]*')
echo "2 resume:         $got                (want: start=level7 dropped=1)"

# 3. A second device signed in to the same account lands in the same box.
curl -s -c C.jar -b "ghid=$GH" -d mode=fresh "$U/play" >/dev/null; C=$(hid C.jar); ready "$C"
r=$(curl -s -b "ghid=$GH" -d mode=fresh -o /dev/null -w '%{redirect_url}' "$U/play")
echo "3 account box:    $([ "${r##*/}" = "$C" ] && echo same || echo DIFFERENT) box                     (want: same)"

# 4. Both devices see one tmux screen.
echo "4 shared screen:  $("$PY" "$HERE/twodev.py" "$GH" 2>/dev/null)   (want: True)"

# 5. An image deploy: a reload is sent to the landing page, Play upgrades the
#    box at the level reached.
echo 22222222 > fp; sleep 35
code=$(curl -s -o /dev/null -w '%{http_code}' -b B.jar "$U/app/$B")
echo "5 image update:   reload -> $code, notice $(curl -s -b B.jar "$U/" | grep -c 'got an update')     (want: 302, notice 1)"
curl -s -c B.jar -b B.jar -d mode=fresh "$U/play" >/dev/null; ready "$B"; sleep 1
got=$(grep -o "provisioned sid=${B:0:12}.*start=level[0-9]*" spawner.log | tail -1 | grep -o 'start=.*')
echo "                  $got on image $(cat "base-p-${B:0:12}")       (want: start=level7 on 22222222)"

# 6. Ranking: every run here is unranked, because the VM's history skips
#    levels (a real box always records level0 first) and B also resumed.
rows=$("$PY" -c "import sqlite3; print(sqlite3.connect('lb.db').execute('select max_level, unranked from players').fetchall())")
echo "6 leaderboard:    $rows   (want: every unranked = 1)"
# 7. Metrics: the funnel counts levels reached, on the localhost-only port.
m=$(curl -s http://127.0.0.1:9199/metrics)
echo "7 metrics:        $(grep '^handover_level_reached_total' <<<"$m" | grep -vc ' 0$') levels, players $(grep '^handover_players ' <<<"$m" | cut -d' ' -f2), on play port: $(curl -s -o /dev/null -w '%{http_code}' "$U/metrics")   (want: >0 levels, players 3, 404)"
# 8. Events and commands: the box's reaches and logged command names land in
#    the events table (the VM's journal has level0's test commands, if any).
ev=$("$PY" -c "import sqlite3; print(dict(sqlite3.connect('lb.db').execute('select kind, count(*) from events group by kind').fetchall()))")
echo "8 events:         $ev   (want: start, reach, resume; cmd if the VM has logged any)"
echo "  commands:       $(curl -s http://127.0.0.1:9199/metrics | grep -c '^handover_commands_total')   (want: >0 if cmd above)"
echo "tracebacks:       $(grep -c Traceback spawner.log)                                  (want: 0)"
