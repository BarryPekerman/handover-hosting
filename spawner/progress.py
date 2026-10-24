"""Pure parsing and scoring.

The spawner reads a container's own journal and login records (that I/O lives in
spawner.py, since it needs the incus-exec helper); this module turns the raw
output into command events, reaches and solve times, and writes leaderboard
rows. Everything here is deterministic given its arguments, which is why these
are the functions the unit tests exercise directly.
"""
import json
import re
import time
from contextlib import closing

from common import MAX_RESUME, PASS_RE, WIN_INDEX, db, log

# Runs INSIDE a player container via `incus exec ... bash`. The box is
# network-less, so it can't report progress itself — we read its own login
# records. Emits "<idx> <epoch>" for the EARLIEST login of each account:
# level0..level20 -> 0..20, dave (the win) -> 21. The spawner turns consecutive
# reaches into per-level durations.
PROGRESS_SH = r'''
set -uo pipefail
collect() {
  for f in /var/log/wtmp.1 /var/log/wtmp; do
    [ -f "$f" ] && last -f "$f" --time-format iso -w 2>/dev/null | awk '$2 ~ /^pts/ {print $1, $4}'
  done
  # _EXE is set by journald, not the sender: without it any player could
  # `logger -t su "session opened for user level20"` and fake a reach.
  journalctl -q -o short-iso SYSLOG_IDENTIFIER=su _EXE=/usr/bin/su --grep 'session opened for user' 2>/dev/null \
    | sed -nE 's/^([^ ]+) .*session opened for user ([A-Za-z0-9_]+)\(.*/\2 \1/p'
}
declare -A first
while read -r u when; do
  case "$u" in level[0-9]*) idx=${u#level};; dave) idx=21;; *) continue;; esac
  t=$(date -d "$when" +%s 2>/dev/null) || continue
  if [ -z "${first[$idx]:-}" ] || [ "$t" -lt "${first[$idx]}" ]; then first[$idx]=$t; fi
done < <(collect)
for idx in "${!first[@]}"; do echo "$idx ${first[$idx]}"; done
'''


# Runs INSIDE a player container. The image's bash logs each command line (up
# to 200 characters, with its exit status; a lone unknown word or any run of 32+
# letters and digits as "?", so no password) to the journal as handover-cmd;
# journald stamps the sender's uid, which maps to the level.
CMDS_SH = r'''
getent passwd | awk -F: '$1 ~ /^(level[0-9]+|dave)$/ {print "U", $3, $1}'
journalctl -q -o json SYSLOG_IDENTIFIER=handover-cmd SYSLOG_IDENTIFIER=su SYSLOG_IDENTIFIER=sshd \
  ${CURSOR:+"--after-cursor=$CURSOR"} -n 2000
'''
CMD_NAME = re.compile(r"^[A-Za-z0-9_.+-]{1,32}$")      # old images logged the name only
CMD_LINE = re.compile(r"^(\d{1,3}) (.{1,200})$", re.S)  # "<exit status> <command line>"
BAD_PASS = re.compile(r"authentication failure;.* user=(level\d+|dave)\b")


def parse_commands(out: str) -> tuple[list[tuple], str]:
    """(epoch, level, kind, detail, status) per command line or wrong password,
    and the journal cursor to resume from."""
    uids, found, cursor = {}, [], ""
    for ln in out.splitlines():
        if ln.startswith("U "):
            p = ln.split()
            if len(p) == 3:
                uids[p[1]] = WIN_INDEX if p[2] == "dave" else int(p[2][5:])
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        cursor = e.get("__CURSOR", cursor)
        msg, uid, ident = e.get("MESSAGE"), e.get("_UID"), e.get("SYSLOG_IDENTIFIER")
        if not isinstance(msg, str):
            continue
        ts = int(e.get("__REALTIME_TIMESTAMP", "0")) // 1_000_000
        if ident == "handover-cmd" and uid in uids:
            m = CMD_LINE.match(msg)
            if m:
                found.append((ts, uids[uid], "cmd", m.group(2), int(m.group(1))))
            elif CMD_NAME.match(msg):
                found.append((ts, uids[uid], "cmd", msg, None))
        elif ident in ("su", "sshd"):
            m = BAD_PASS.search(msg)
            if m:   # a wrong password for account N = stuck on level N-1
                target = WIN_INDEX if m.group(1) == "dave" else int(m.group(1)[5:])
                found.append((ts, max(target - 1, 0), "badpass", m.group(1), None))
    return found, cursor


def parse_save(raw: str) -> dict[int, str]:
    """The browser's save ({"levels": {"7": "<pass>", ...}}) as {level: pass}.
    Anything malformed is ignored, not an error: the save lives in the
    player's browser and may be stale, edited or corrupted."""
    try:
        d = json.loads(raw) if raw and len(raw) <= 8192 else {}
    except ValueError:
        return {}
    lv = d.get("levels") if isinstance(d, dict) else None
    out = {}
    if isinstance(lv, dict):
        for k, v in lv.items():
            if (str(k).isdigit() and 1 <= int(k) <= MAX_RESUME
                    and isinstance(v, str) and PASS_RE.fullmatch(v)):
                out[int(k)] = v
    return out


def solve_time(reaches: dict[int, int], prev_max: int, new_max: int) -> int:
    """Seconds spent on the levels completed since prev_max, each counted once."""
    added = 0
    for idx in range(prev_max, new_max):
        if idx in reaches and idx + 1 in reaches:
            added += max(0, reaches[idx + 1] - reaches[idx])
    return added


def skipped_levels(reaches: dict[int, int], prev_max: int, new_max: int) -> bool:
    """True if a level between prev_max and new_max has no recorded login: the
    account jumped ahead (passwords are shared, so knowing Dave's is enough to
    "finish" in 0 s), and its time is missing those levels."""
    return any(i not in reaches for i in range(prev_max, new_max + 1))


def save_progress(key: str, display: str, max_level: int, accum: int,
                  finished: int, unranked: int):
    try:
        with closing(db()) as c:
            c.execute("INSERT INTO players(key,display,max_level,accumulated,finished,updated,unranked) "
                      "VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                      "display=excluded.display, max_level=excluded.max_level, "
                      "accumulated=excluded.accumulated, finished=excluded.finished, "
                      "updated=excluded.updated, "
                      "unranked=MAX(COALESCE(players.unranked,0), excluded.unranked)",
                      (key, display, max_level, accum, finished, time.time(), unranked))
            c.commit()
    except Exception:
        log.exception("leaderboard write failed")
