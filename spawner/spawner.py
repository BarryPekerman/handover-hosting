#!/usr/bin/env python3
"""The Handover spawner: one isolated Incus container per browser visitor,
fronted by a single localhost port (Cloudflare tunnel terminates upstream).

Routes:
  GET  /                 landing page (Play / resume, sign-in, update notice)
  POST /play             find-or-create this visitor's box: by GitHub account or
                         the `hid` cookie; mode=resume starts it at the highest
                         level the browser's save proves; replaces an outdated box
  GET  /ready            spinner polling: pending | ready | full | error
  GET  /app/<sid>        terminal page (iframe + notes panel + save sync)
  ANY  /t/<sid>/*        reverse-proxy (HTTP + WebSocket) to that box's ttyd
  GET  /save/<sid>       passwords of the levels reached, for the browser's save
  GET  /leaderboard      finishers by summed solve time
  GET  /story            the premise and how to play (kept off the front page)
  GET  /login /auth/callback /logout   optional GitHub sign-in
  GET  /healthz          {"players": N, "max": M}

Each box is a NIC-less container (profile `isolated`) plus its own ttyd on a
loopback port; the terminal rides `incus exec` over the control socket, so
containers reach neither each other, the host, nor the net. Every connection
joins the box's one tmux session, so devices share a screen.

Config via env (all optional; secrets come from the EnvironmentFile):
  LISTEN_HOST=127.0.0.1  LISTEN_PORT=8080   PUBLIC_URL=https://play.barrypekerman.com
  BASE_IMAGE=handover    PROFILE=isolated   LOGIN_USER=level0
  MAX_PLAYERS=20         IDLE_MIN=60        MAX_LIFETIME_MIN=1440
  OUTDATED_IDLE_MIN=5    (idle boxes on a replaced image are recycled)
  RATE_MAX=5 RATE_WINDOW=600   (new boxes per client IP per window, seconds)
  TTYD_PORT_BASE=8100   METRICS_PORT=9101 (localhost-only /metrics)
  STATE_FILE=~/.handover-sessions.json   LEADERBOARD_DB=~/handover-leaderboard.db
  TURNSTILE_SITEKEY / TURNSTILE_ACTION=play / TURNSTILE_HOSTNAMES   bot check
  GH_CLIENT_ID                           GitHub OAuth app (public)
  secrets: TURNSTILE_SECRET, GH_CLIENT_SECRET, SESSION_SECRET (cookie signing)
"""
import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import shlex
import sqlite3
import time
from aiohttp import web, ClientSession, WSMsgType, ClientTimeout, WSCloseCode

LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
BASE_IMAGE  = os.environ.get("BASE_IMAGE", "handover")
PROFILE     = os.environ.get("PROFILE", "isolated")
MAX_PLAYERS = int(os.environ.get("MAX_PLAYERS", "20"))
IDLE_SEC    = int(os.environ.get("IDLE_MIN", "60")) * 60
OUTDATED_IDLE = int(os.environ.get("OUTDATED_IDLE_MIN", "5")) * 60
MAX_LIFE    = int(os.environ.get("MAX_LIFETIME_MIN", "1440")) * 60
PORT_BASE   = int(os.environ.get("TTYD_PORT_BASE", "8100"))
LOGIN_USER  = os.environ.get("LOGIN_USER", "level0")
COOKIE      = "hid"
MAX_RESUME  = 20          # highest level a save can resume at
WIN_INDEX   = 21          # dave's index in the login records: reaching it is the win
PASS_RE     = re.compile(r"[A-Za-z0-9]{1,64}")
STATE_FILE  = os.environ.get("STATE_FILE", os.path.expanduser("~/.handover-sessions.json"))
DB_PATH     = os.environ.get("LEADERBOARD_DB", os.path.expanduser("~/handover-leaderboard.db"))
RATE_MAX    = int(os.environ.get("RATE_MAX", "5"))       # new containers per IP ...
RATE_WINDOW = int(os.environ.get("RATE_WINDOW", "600"))  # ... per this many seconds
# Turnstile (Cloudflare bot check). Sitekey is public; secret lives only on the
# host (EnvironmentFile), never in git. Verification is enforced only when the
# secret is present, so the site keeps working until it's placed.
TS_SITEKEY  = os.environ.get("TURNSTILE_SITEKEY", "")
TS_SECRET   = os.environ.get("TURNSTILE_SECRET", "")
TS_ACTION   = os.environ.get("TURNSTILE_ACTION", "play")
TS_HOSTS    = {h.strip() for h in os.environ.get(
    "TURNSTILE_HOSTNAMES", "play.barrypekerman.com,barrypekerman.com").split(",") if h.strip()}
TS_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
# GitHub OAuth (optional login, for the cross-session leaderboard). Client id is
# public (unit); secret + session-signing key are host-only (EnvironmentFile).
PUBLIC_URL       = os.environ.get("PUBLIC_URL", "https://play.barrypekerman.com")
GH_CLIENT_ID     = os.environ.get("GH_CLIENT_ID", "")
GH_CLIENT_SECRET = os.environ.get("GH_CLIENT_SECRET", "")
SESSION_SECRET   = os.environ.get("SESSION_SECRET", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spawner")

# sid -> {ct, port, proc, created, last}
sessions: dict[str, dict] = {}
locks: dict[str, asyncio.Lock] = {}      # per-sid provisioning lock
states: dict[str, str] = {}              # sid -> "pending"|"full"|"error" (ready == in sessions)
ip_hits: dict[str, list] = {}            # client IP -> recent provision timestamps (throttle)
who: dict[str, dict] = {}                # sid -> {"uid": <github id>|None, "display": str}
                                         # (a signed-in player's box follows the account)
live_ws: set = set()                     # open terminal WebSockets, closed on shutdown
resume_req: dict[str, tuple] = {}        # sid -> (saved {level: pass}, server-known level, note)
live_image = ""                          # fingerprint BASE_IMAGE points at now
background: set = set()                  # running provision tasks (asyncio keeps only weak refs)

# ---------- metrics: Prometheus text format on a localhost-only port ----------
# Scraped by the OKE Prometheus through the tunnel route handover-metrics (Access
# service token). Counters reset when the spawner restarts; Prometheus handles that.
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9101"))
SOLVE_BUCKETS = (60, 180, 300, 600, 1200, 1800, 3600, 7200, 14400)  # seconds
SOLVE_CAP = 14400        # longer is a player who walked away: kept in events, not in the histogram
# Commands counted by name in the metrics; anything else is "other" (the
# events table keeps every name). Bounded so a player can't explode the series.
TRACKED_CMDS = frozenset("""
    ls cd cat less more head tail grep find file strings xxd od hexdump base64
    openssl gpg tar gzip zcat unzip bzip2 xz diff sort uniq wc cut tr awk sed
    vim vi nano man mail su ssh go sudo id whoami groups getfacl setfacl chmod
    stat ps top kill pkill pgrep systemctl journalctl crontab at env export
    echo printf history clear exit git python3 curl wget nc ss lsof sqlite3 jq
    tmux tree pwd cp mv rm mkdir touch ln readlink which type alias help
""".split())
counters: dict[tuple, float] = {}        # (name, sorted label items) -> value
solve_hist: dict[int, list] = {}         # level -> per-bucket counts + [+Inf count, sum]


def inc(name: str, n: float = 1, **labels):
    key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
    counters[key] = counters.get(key, 0) + n


def init_counters():
    """Every series with a known label set starts at 0, so a quiet game reads 0
    on the dashboard instead of "No data" (and increase() sees the first event)."""
    zero = [("handover_level_reached_total", {"level": i}) for i in range(WIN_INDEX + 1)]
    zero += [("handover_level_solve_capped_total", {"level": i}) for i in range(WIN_INDEX)]
    zero += [("handover_finishes_total", {"ranked": r}) for r in ("true", "false")]
    zero += [("handover_resumes_total", {"outcome": o}) for o in ("resumed", "no_match", "nothing_saved")]
    zero += [("handover_box_starts_total", {"result": r}) for r in ("ready", "full", "error")]
    zero += [("handover_teardowns_total", {"reason": r})
             for r in ("idle", "max-lifetime", "outdated-idle", "upgrade")]
    zero += [("handover_errors_total", {})]
    for name, labels in zero:
        inc(name, 0, **labels)


class CountErrors(logging.Handler):
    """Every ERROR the spawner logs (incl. log.exception) counts in
    handover_errors_total; the journal has the detail."""

    def __init__(self):
        super().__init__(logging.ERROR)

    def emit(self, record):
        inc("handover_errors_total")


log.addHandler(CountErrors())


def observe_solve(level: int, secs: float):
    h = solve_hist.setdefault(level, [0] * len(SOLVE_BUCKETS) + [0, 0.0])
    for i, b in enumerate(SOLVE_BUCKETS):
        if secs <= b:
            h[i] += 1
    h[-2] += 1
    h[-1] += secs


def count_progress(s: dict, reaches: dict[int, int]):
    """Count each level a box reaches once (the funnel), and the time spent on
    the level before it, when that level was played in this box. Every reach
    is also an event row with its real time."""
    counted = s.setdefault("counted", [])
    rows = []
    for idx in sorted(reaches):
        if idx in counted:
            continue
        counted.append(idx)
        rows.append((reaches[idx], s.get("ct", ""), "reach", idx, ""))
        inc("handover_level_reached_total", level=idx)
        if idx - 1 in reaches and idx - 1 >= s.get("start", 0):
            secs = max(0, reaches[idx] - reaches[idx - 1])
            if secs <= SOLVE_CAP:
                observe_solve(idx - 1, secs)
            else:
                inc("handover_level_solve_capped_total", level=idx - 1)
        if idx == WIN_INDEX:
            ranked = s.get("start", 0) == 0 and all(i in reaches for i in range(WIN_INDEX + 1))
            inc("handover_finishes_total", ranked=str(ranked).lower())
            rows.append((reaches[idx], s.get("ct", ""), "finish", idx, "ranked" if ranked else "unranked"))
    events(rows)


def count_commands(s: dict, found: list[tuple[int, int, str]]):
    """found: (epoch, level, command name) read from the box's journal."""
    for _, lvl, cmd in found:
        inc("handover_commands_total", level=lvl, cmd=cmd if cmd in TRACKED_CMDS else "other")
    events([(t, s.get("ct", ""), "cmd", lvl, cmd) for t, lvl, cmd in found])


def _labels(items) -> str:
    return "{" + ",".join(f'{k}="{v}"' for k, v in items) + "}" if items else ""


def render_metrics() -> str:
    out = [
        "# TYPE handover_players gauge", f"handover_players {len(sessions)}",
        "# TYPE handover_seats gauge", f"handover_seats {MAX_PLAYERS}",
        "# TYPE handover_starting gauge",
        f"handover_starting {sum(1 for v in states.values() if v == 'pending')}",
        "# TYPE handover_info gauge", f'handover_info{{image="{live_image[:12]}"}} 1',
        "# TYPE handover_player_level gauge",
    ]
    at = {}
    for x in sessions.values():
        lvl = max(x.get("counted") or [x.get("start", 0)])
        at[lvl] = at.get(lvl, 0) + 1
    out += [f'handover_player_level{{level="{lvl}"}} {n}' for lvl, n in sorted(at.items())]
    for name in sorted({k[0] for k in counters}):
        out.append(f"# TYPE {name} counter")
        out += [f"{name}{_labels(items)} {v:g}" for (n, items), v in sorted(counters.items()) if n == name]
    out.append("# TYPE handover_level_solve_seconds histogram")
    for lvl, h in sorted(solve_hist.items()):
        for b, c in zip(SOLVE_BUCKETS, h, strict=False):
            out.append(f'handover_level_solve_seconds_bucket{{level="{lvl}",le="{b}"}} {c}')
        out.append(f'handover_level_solve_seconds_bucket{{level="{lvl}",le="+Inf"}} {h[-2]}')
        out.append(f'handover_level_solve_seconds_count{{level="{lvl}"}} {h[-2]}')
        out.append(f'handover_level_solve_seconds_sum{{level="{lvl}"}} {h[-1]:g}')
    return "\n".join(out) + "\n"


async def h_metrics(request):
    return web.Response(text=render_metrics(), content_type="text/plain")


def db():
    c = sqlite3.connect(DB_PATH)
    # key = GitHub uid for logged-in players (persists across sessions), or
    # "anon:<sid>" for anonymous players (single-session). accumulated = sum of
    # each level's solve time, counted once.
    c.execute("CREATE TABLE IF NOT EXISTS players "
              "(key TEXT PRIMARY KEY, display TEXT, max_level INTEGER, "
              " accumulated INTEGER, finished INTEGER, updated REAL)")
    # unranked = resumed past the levels this key actually played, so its total
    # is missing time and it stays off the leaderboard.
    if "unranked" not in {r[1] for r in c.execute("PRAGMA table_info(players)")}:
        c.execute("ALTER TABLE players ADD COLUMN unranked INTEGER DEFAULT 0")
    # Raw game events for analytics (later: Postgres). box = container name;
    # no player identity. kind: start, resume, reach, finish, end, cmd.
    c.execute("CREATE TABLE IF NOT EXISTS events "
              "(ts INTEGER, box TEXT, kind TEXT, level INTEGER, detail TEXT)")
    return c


def events(rows: list[tuple]):
    """Append (ts, box, kind, level, detail) rows; analytics never break play."""
    if not rows:
        return
    try:
        c = db()
        c.executemany("INSERT INTO events(ts,box,kind,level,detail) VALUES(?,?,?,?,?)", rows)
        c.commit()
        c.close()
    except Exception:
        log.exception("events write failed")


def player_row(key):
    c = db()
    row = c.execute("SELECT max_level, accumulated, finished FROM players WHERE key=?",
                    (key,)).fetchone()
    c.close()
    return row  # (max_level, accumulated, finished) or None


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


# Runs INSIDE a player container. The image's bash logs each command's name
# (never its arguments, only names bash knows as a command) to the journal as
# handover-cmd; journald stamps the sender's uid, which maps to the level.
CMDS_SH = r'''
getent passwd | awk -F: '$1 ~ /^(level[0-9]+|dave)$/ {print "U", $3, $1}'
journalctl -q -o json SYSLOG_IDENTIFIER=handover-cmd ${CURSOR:+"--after-cursor=$CURSOR"} -n 2000
'''
CMD_NAME = re.compile(r"^[A-Za-z0-9_.+-]{1,32}$")


def parse_commands(out: str) -> tuple[list[tuple[int, int, str]], str]:
    """(epoch, level, name) per logged command, and the journal cursor to resume from."""
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
        name, uid = e.get("MESSAGE"), e.get("_UID")
        if isinstance(name, str) and CMD_NAME.match(name) and uid in uids:
            found.append((int(e.get("__REALTIME_TIMESTAMP", "0")) // 1_000_000, uids[uid], name))
    return found, cursor


async def read_commands(s: dict):
    script = f"CURSOR={shlex.quote(s.get('cmd_cursor', ''))}\n" + CMDS_SH
    found, cursor = parse_commands(await ct_bash(s["ct"], script))
    if cursor:
        s["cmd_cursor"] = cursor
    count_commands(s, found)


async def ct_bash(ct: str, script: str) -> str:
    """Run a bash script inside a container as root; returns its output."""
    _, out = await run("incus", "exec", ct, "--", "bash", "-c", script)
    return out


async def read_passes(ct: str, levels) -> dict[int, str]:
    """{level: password} read from the container's /etc/handover_pass, in one
    exec. Never log the result."""
    levels = [int(n) for n in levels if 1 <= int(n) <= MAX_RESUME]
    if not levels:
        return {}
    script = ("for n in %s; do printf '%%s ' $n; head -c 64 /etc/handover_pass/level$n "
              "2>/dev/null | tr -d '\\n'; echo; done" % " ".join(map(str, levels)))
    out = await ct_bash(ct, script)
    got = {}
    for ln in out.splitlines():
        p = ln.split(" ", 1)
        if len(p) == 2 and p[0].isdigit() and PASS_RE.fullmatch(p[1]):
            got[int(p[0])] = p[1]
    return got


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


async def push_playtime(ct: str, secs: int):
    """Write the player's running total (seconds) into the network-less
    container, so the victory screen can show the real cross-session time
    instead of just this box's own clock. Best-effort; tmpfs, world-readable."""
    cmd = ("mkdir -p /run/handover && echo %d > /run/handover/playtime "
           "&& chmod 644 /run/handover/playtime" % int(secs))
    await ct_bash(ct, cmd)


async def read_reaches(ct: str) -> dict[int, int]:
    """{level index: epoch of its first login} from the container's own records."""
    reaches = {}
    for ln in (await ct_bash(ct, PROGRESS_SH)).splitlines():
        p = ln.split()
        if len(p) == 2 and p[0].isdigit() and p[1].isdigit():
            reaches[int(p[0])] = int(p[1])
    return reaches


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
        c = db()
        c.execute("INSERT INTO players(key,display,max_level,accumulated,finished,updated,unranked) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                  "display=excluded.display, max_level=excluded.max_level, "
                  "accumulated=excluded.accumulated, finished=excluded.finished, "
                  "updated=excluded.updated, "
                  "unranked=MAX(COALESCE(players.unranked,0), excluded.unranked)",
                  (key, display, max_level, accum, finished, time.time(), unranked))
        c.commit()
        c.close()
    except Exception:
        log.exception("leaderboard write failed")


async def update_progress(sid: str, ct: str):
    """Fold this container's progress into the player's running tally. Keyed by
    GitHub uid (cross-session) or anon:<sid> (single session). Adds only the
    time for levels newly completed since the stored max, so fast re-climbs with
    saved passwords don't double-count and between-session breaks never count.
    Always pushes the player's current total back into the container for the
    victory screen."""
    info = who.get(sid) or {"uid": None, "display": "anonymous"}
    key = info["uid"] or ("anon:" + sid)
    reaches = await read_reaches(ct)
    s = sessions.get(sid)
    if s is not None:
        count_progress(s, reaches)
        await read_commands(s)
    if s is not None:   # remember reached levels' passwords for the browser's save
        new = [n for n in reaches if n not in s["passes"]]
        s["passes"].update(await read_passes(ct, new))
    row = player_row(key)
    prev_max, accum, finished = row if row else (0, 0, 0)
    new_max = max(reaches, default=prev_max)
    if new_max > prev_max:                       # new levels completed this poll
        added = solve_time(reaches, prev_max, new_max)
        accum += added
        finished = 1 if new_max >= WIN_INDEX else finished
        # Resuming above what this key has played, or jumping past levels,
        # leaves those levels' time out, so the run can't be ranked.
        resumed_past = s is not None and s.get("start", 0) > prev_max
        unranked = 1 if resumed_past or skipped_levels(reaches, prev_max, new_max) else 0
        save_progress(key, info["display"], new_max, accum, finished, unranked)
        log.info("progress key=%s max=%d +%ds total=%ds fin=%d", key[:16], new_max, added, accum, finished)
    # The container is network-less and can't read the DB, so hand it the total.
    await push_playtime(ct, accum)


async def record_finishers():
    for sid, s in list(sessions.items()):
        await update_progress(sid, s["ct"])


async def sweep_orphans():
    """Delete any p-* container the spawner isn't tracking. Every legitimate
    container is either a live session or mid-provision (its sid is in `states`
    'pending'), so anything else is abandoned — a crash mid-provision, a stale
    launch, a desync — and would otherwise leak RAM + inotify forever."""
    live = {"p-" + s[:12] for s in sessions} | {"p-" + s[:12] for s in states}
    _, out = await run("incus", "list", "-c", "n", "-f", "csv")
    for line in out.splitlines():
        name = line.strip()
        if name.startswith("p-") and name not in live:
            log.warning("sweeping orphan container %s", name)
            await run("incus", "delete", "-f", name)


def client_ip(request) -> str:
    # behind Cloudflare the real client is in CF-Connecting-IP; fall back sanely
    return (request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote or "?")


def _sign(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    sig = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + sig


def _unsign(token: str):
    try:
        raw, sig = token.rsplit(".", 1)
        good = hmac.new(SESSION_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        return json.loads(base64.urlsafe_b64decode(raw.encode()))
    except Exception:
        return None


def identity(request):
    """(uid, display) from the signed ghid cookie, or (None, 'anonymous')."""
    if not SESSION_SECRET:
        return (None, "anonymous")
    tok = request.cookies.get("ghid")
    p = _unsign(tok) if tok else None
    if not p:
        return (None, "anonymous")
    return (str(p.get("uid")), "@" + str(p.get("login", "?")))


def account_sid(uid) -> str | None:
    """The live (or still provisioning) box owned by GitHub account `uid`."""
    for sid, w in who.items():
        if w.get("uid") == uid and (sid in sessions or states.get(sid) == "pending"):
            return sid
    return None


def resolve_sid(request) -> str | None:
    """This visitor's box. Signed in: the account's box, from any device; else
    the cookie's box, which a signed-in player claims if it's anonymous (never
    another account's). Anonymous: the cookie's box."""
    uid, _ = identity(request)
    cookie = request.cookies.get(COOKIE)
    if uid:
        mine = account_sid(uid)
        if mine:
            return mine
        if cookie and who.get(cookie, {}).get("uid") not in (None, uid):
            return None
    return cookie


def remember(sid: str, uid, display: str):
    """Record who a box belongs to. Signing out never downgrades an account's box."""
    if uid or who.get(sid, {}).get("uid") is None:
        who[sid] = {"uid": uid, "display": display}


def with_cookie(resp, sid: str):
    resp.set_cookie(COOKIE, sid, httponly=True, samesite="Lax", path="/", max_age=MAX_LIFE)
    return resp


def spinner(sid: str):
    """The "spinning up" page, which polls /ready and then opens the terminal."""
    return with_cookie(web.Response(text=SPAWNING.replace("__SID__", sid),
                                    content_type="text/html"), sid)


def throttled(ip: str) -> bool:
    """True if this IP has started too many containers lately (records a hit if not)."""
    now = time.time()
    q = ip_hits.setdefault(ip, [])
    q[:] = [t for t in q if now - t < RATE_WINDOW]
    if len(q) >= RATE_MAX:
        return True
    q.append(now)
    return False


async def run(*args) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return p.returncode, out.decode(errors="replace")


def alloc_port() -> int:
    used = {s["port"] for s in sessions.values()}
    p = PORT_BASE
    while p in used:
        p += 1
    return p


def save_state():
    """Persist sessions (minus the live proc handle) so a restart can re-adopt them."""
    try:
        data = {sid: {**{k: s[k] for k in ("ct", "port", "created", "last", "start", "image")},
                      "counted": s.get("counted", []), "cmd_cursor": s.get("cmd_cursor", ""),
                      "who": who.get(sid, {"uid": None, "display": "anonymous"})}
                for sid, s in sessions.items()}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        log.exception("save_state failed")


async def image_fingerprint(ref: str) -> str:
    _, out = await run("incus", "image", "info", ref)
    m = re.search(r"^Fingerprint: *([0-9a-f]+)", out, re.M)
    return m.group(1) if m else ""


async def container_image(ct: str) -> str:
    rc, out = await run("incus", "config", "get", ct, "volatile.base_image")
    return out.strip() if rc == 0 else ""


def outdated(s: dict) -> bool:
    """True once the image this session's container came from is no longer the
    live one (an image deploy moved the alias)."""
    return bool(live_image and s.get("image") and s["image"] != live_image)


def reached_level(s: dict) -> int:
    return max([s.get("start", 0)] + [n for n in s["passes"] if n <= MAX_RESUME])


# Each connection joins one tmux session per box, so every device signed in to
# the same account sees (and types into) the same screen. The outer login shell
# skips the banner (MOTD_SHOWN); the shell inside tmux prints it, once. Images
# without tmux get a plain login shell.
SHELL_CMD = ("if command -v tmux >/dev/null; then exec env -u MOTD_SHOWN tmux new-session -A -s play; "
             "else exec env -u MOTD_SHOWN bash -l; fi")


def login_user(start: int) -> str:
    return f"level{start}" if start else LOGIN_USER


async def start_ttyd(sid: str, ct: str, port: int, user: str):
    """Launch a ttyd bound to loopback that attaches into the container as `user`."""
    return await asyncio.create_subprocess_exec(
        "ttyd", "-p", str(port), "-i", "127.0.0.1", "-b", f"/t/{sid}",
        # A slightly smaller font buys a few more columns on narrow phone
        # screens (xterm's fit addon derives columns from pixel width / char
        # width) while staying readable on a desktop.
        "-t", "fontSize=13",
        "-W", "incus", "exec", ct, "--env", "MOTD_SHOWN=yes", "--",
        "su", "-", user, "-w", "MOTD_SHOWN", "-c", SHELL_CMD,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)


async def provision(sid: str) -> dict | None:
    """Create the container + ttyd for sid. Returns the session or None on cap/failure."""
    lock = locks.setdefault(sid, asyncio.Lock())
    async with lock:
        if sid in sessions:
            return sessions[sid]
        if len(sessions) >= MAX_PLAYERS:
            return None
        ct = "p-" + sid[:12]
        # NIC-less launch prints a warning and returns non-zero; that's expected.
        await run("incus", "launch", BASE_IMAGE, ct, "--profile", PROFILE)
        ok = False
        for _ in range(40):
            _, out = await run("incus", "exec", ct, "--", "systemctl", "is-system-running")
            if out.strip() in ("running", "degraded"):
                ok = True
                break
            await asyncio.sleep(1)
        if not ok:
            log.error("container %s never became ready; tearing down", ct)
            await run("incus", "delete", "-f", ct)
            return None
        # Clean prompt: give every container the same friendly hostname so the
        # shell shows "levelN@handover" instead of "levelN@p-9f3c…". The random
        # instance-name hostname reads as noise on a cramped phone screen
        # (e.g. "level0@p-…" gets misread as "level10").
        await run("incus", "exec", ct, "--", "hostnamectl", "set-hostname", "handover")
        start, drop, note = await choose_start(ct, resume_req.pop(sid, None))
        port = alloc_port()
        proc = await start_ttyd(sid, ct, port, login_user(start))
        now = time.time()
        sessions[sid] = {"ct": ct, "port": port, "proc": proc, "created": now, "last": now,
                         "start": start, "passes": {}, "drop": drop, "note": note,
                         "image": await container_image(ct)}
        save_state()
        log.info("provisioned sid=%s ct=%s port=%d start=level%d dropped=%d (%d/%d)",
                 sid[:12], ct, port, start, len(drop), len(sessions), MAX_PLAYERS)
        # give ttyd a moment to bind
        await asyncio.sleep(0.5)
        return sessions[sid]


async def choose_start(ct: str, req) -> tuple[int, list, str]:
    """Pick the level to resume at: the highest saved level whose password
    matches this container's (so a save can't be forged without the password),
    or the signed-in player's recorded level, whichever is higher. Returns
    (start, saved levels that didn't match, message for the player)."""
    if not req:
        return 0, [], ""
    saved, srv, override = req
    real = await read_passes(ct, saved) if saved else {}
    good = [n for n, p in saved.items() if n in real and hmac.compare_digest(p, real[n])]
    drop = sorted(n for n in saved if n not in good)
    start = max([*good, srv])
    outcome = "resumed" if start else ("no_match" if drop else "nothing_saved")
    inc("handover_resumes_total", outcome=outcome)
    events([(int(time.time()), ct, "resume", start, outcome)])
    if start:
        note = f"resumed at level {start}"
    elif drop:
        note = ("your saved progress doesn't match this server (it may have been "
                "rebuilt) - starting from level 0")
    else:
        note = ""
    return start, drop, override or note


async def provision_bg(sid: str):
    """Background provisioning so /play can answer instantly with a spinner."""
    try:
        s = await provision(sid)
        states[sid] = "ready" if s else "full"
        if s:
            events([(int(time.time()), s["ct"], "start", s.get("start", 0), s.get("image", "")[:12])])
    except Exception:
        log.exception("provision failed sid=%s", sid)
        states[sid] = "error"
    inc("handover_box_starts_total", result=states[sid])


def start_provision(sid: str):
    """Provision in the background, keeping a reference until it finishes."""
    states[sid] = "pending"
    task = asyncio.create_task(provision_bg(sid))
    background.add(task)
    task.add_done_callback(background.discard)


async def teardown(sid: str, reason: str = ""):
    s = sessions.get(sid)
    if s:
        await update_progress(sid, s["ct"])   # last chance to fold in progress
    s = sessions.pop(sid, None)
    locks.pop(sid, None)
    states.pop(sid, None)
    who.pop(sid, None)
    if not s:
        return
    log.info("teardown sid=%s ct=%s (%s)", sid[:12], s["ct"], reason)
    inc("handover_teardowns_total", reason=reason or "unknown")
    events([(int(time.time()), s["ct"], "end", max(s.get("counted") or [s.get("start", 0)]), reason)])
    with contextlib.suppress(ProcessLookupError, AttributeError):
        s["proc"].terminate()
    await run("incus", "delete", "-f", s["ct"])
    save_state()


async def reaper(app):
    global live_image
    try:
        while True:
            await asyncio.sleep(30)
            live_image = await image_fingerprint(BASE_IMAGE) or live_image
            await record_finishers()      # capture victories before reaping
            await sweep_orphans()         # GC containers nobody is tracking
            now = time.time()
            for sid, s in list(sessions.items()):
                if now - s["last"] > IDLE_SEC:
                    await teardown(sid, "idle")
                elif now - s["created"] > MAX_LIFE:
                    await teardown(sid, "max-lifetime")
                # After an image deploy, idle players move over now; their open
                # tab sends them to the landing page, where they can resume.
                elif outdated(s) and now - s["last"] > OUTDATED_IDLE:
                    await teardown(sid, "outdated-idle")
    except asyncio.CancelledError:
        pass


# ---------- HTTP handlers ----------

LANDING = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>The Handover</title><style>
:root{color-scheme:dark}
body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;
justify-content:center;background:#000;color:#33ff66;font-family:monospace;text-align:center}
/* one vertical rhythm: title -> hook small, hook -> buttons -> links equal */
h1{margin:0 0 1rem}p{margin:0}a{color:#33ff66}
.hook{font-size:1.05rem;line-height:1.65;text-wrap:balance}
form{display:flex;flex-direction:column;align-items:center;gap:.7rem;margin-top:2rem}
.links{margin-top:2rem;opacity:.8}.signin{margin-top:.9rem;opacity:.8;font-size:.9rem}
.play{margin:0;padding:.8rem 2.2rem;border:1px solid #33ff66;color:#33ff66;
background:none;font-family:monospace;font-size:1.2rem;cursor:pointer}
.play:hover{background:#33ff66;color:#000}
p{max-width:34rem;line-height:1.5;padding:0 1rem}
.cf-turnstile{margin-top:.3rem}
</style>__TS_SCRIPT__</head><body>
<h1>THE HANDOVER</h1>
<p class=hook>Dave won the lottery and left you his server.<br>Nothing is documented.</p>
<form method="post" action="/play"><input type=hidden name=save id=save>
<button class=play type=submit name=mode value=resume id=resume hidden></button>
<button class=play type=submit name=mode value=fresh id=fresh>&gt; play</button>
__TS_WIDGET__</form>
<p class=links><a href="/story">story &amp; how to play</a>
&nbsp;&middot;&nbsp; <a href="/leaderboard">leaderboard</a></p>
<p class=signin>__SIGNIN__</p>
<script>__SAVE_JS__
(function(){
  var lv=loadSave(),n=Math.max(topLevel(lv),__SRV__);
  if(!n)return;
  var r=document.getElementById('resume');
  r.hidden=false;r.textContent='> resume at level '+n;
  document.getElementById('fresh').textContent='> start from level 0';
  document.getElementById('save').value=JSON.stringify({levels:lv});
})();
</script>
</body></html>"""

# The premise lives here rather than on the front page; Dave's own voice is
# kept for his first mail in the game (bandit-wannabe VOICE.md).
STORY = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>The Handover - story</title><style>
:root{color-scheme:dark}
body{margin:0;min-height:100vh;background:#000;color:#33ff66;font-family:monospace;
display:flex;flex-direction:column;align-items:center;padding:2rem 1rem}
main{max-width:36rem;line-height:1.55}h1{margin:.2rem 0;text-align:center}
h2{font-size:1rem;margin:2rem 0 .4rem;border-bottom:1px solid #0a3;padding-bottom:.2rem}
a{color:#33ff66}code{background:#021;padding:0 .25rem}ul{padding-left:1.2rem}li{margin:.3rem 0}
.dim{opacity:.75}em{font-style:italic}
</style></head><body><main>
<h1>THE HANDOVER</h1>

<h2>The story</h2>
<p>Congratulations &mdash; you're the DevOps team.</p>
<p>Nobody quite put it that way when they hired you. They talked about the product, the
roadmap, the funding round. Your laptop arrived before your contract did. The servers came
up once, in passing: <em>"Dave handles that."</em></p>
<p>Dave won the lottery last week. The actual lottery. You've got his server, his logins,
and a pile of notes he wrote on the way to the airport.</p>
<p>Somewhere on that box is his admin password. Find it, and everything he built is yours.</p>

<h2>How it works</h2>
<ul>
<li>Press play and you get your own Linux box in the browser. Nothing to install, nobody
else on it.</li>
<li><code>mail</code> reads Dave's note. Each level's puzzle hides the next account's
password; <code>go 3</code> logs you into level 3.</li>
<li>Your progress saves in this browser &mdash; if your session resets, come back and resume.
Sign in with GitHub to keep the same box on every device and to appear on the
leaderboard.</li>
<li>Stuck? Re-read the note; <code>man</code> is there. The box has no internet &mdash;
everything you need is already on it.</li>
</ul>

<h2>What you'll touch</h2>
<p class=dim>No spoilers: files and filesystems &middot; permissions and sudo &middot; git's
insides &middot; processes, signals and cron. 21 levels, four acts.</p>

<h2>Fair play</h2>
<p class=dim>Everyone gets the same passwords, so yes, you could look them up. It's your game
to spoil. The leaderboard only counts runs that climb every level.</p>

<h2>What we record</h2>
<p class=dim>To see where people get stuck: when each level is reached, and the names of the
commands used on it (<code>find</code>, <code>grep</code>&hellip; never what you type after
them). Not tied to your GitHub account.</p>

<p style="margin-top:2rem;text-align:center"><a href="/">&larr; back to play</a></p>
</main></body></html>"""


def message_page(title: str, heading: str, body: str, color: str) -> str:
    """A one-message page (slow down, failed bot check)."""
    return (f"""<!doctype html><html><head><meta charset=utf-8><title>{title}</title>
<style>body{{background:#000;color:{color};font-family:monospace;display:flex;height:100vh;
align-items:center;justify-content:center;text-align:center}}a{{color:{color}}}</style></head>
<body><div><h1>{heading}</h1><p>{body}</p></div></body></html>""")


SLOWDOWN = message_page("Slow down", "easy there",
                        "You've started a few servers in a short time. Give it a couple of "
                        'minutes, then <a href="/">head back</a> and press Play.', "#ffcc33")
TS_FAIL = message_page("Are you human?", "couldn't verify you",
                       'The human check didn\'t pass. <a href="/">go back</a> and press Play again.',
                       "#ff5555")


LEADERBOARD = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>The Handover - leaderboard</title><style>
:root{color-scheme:dark}
body{margin:0;min-height:100vh;background:#000;color:#33ff66;font-family:monospace;
display:flex;flex-direction:column;align-items:center;padding:2rem 1rem}
h1{margin:.2rem 0}table{border-collapse:collapse;margin-top:1.2rem;width:100%;max-width:34rem}
td,th{padding:.4rem .8rem;border-bottom:1px solid #0a3;text-align:left}
th{opacity:.7;font-weight:normal}td:first-child{opacity:.6}td:last-child{text-align:right}
a{color:#33ff66}p{opacity:.7}
</style></head><body>
<h1>THE HANDOVER</h1><p>fastest to recover Dave's admin password</p>
<table><tr><th>#</th><th>who</th><th>time</th></tr>__ROWS__</table>
<p style="margin-top:1.5rem"><a href="/">&larr; back</a></p>
</body></html>"""

SPAWNING = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Spinning up…</title><style>
:root{color-scheme:dark}
body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;
justify-content:center;background:#000;color:#33ff66;font-family:monospace;text-align:center}
.dots::after{content:'';animation:d 1.2s steps(4,end) infinite}
@keyframes d{0%{content:''}25%{content:'.'}50%{content:'..'}75%{content:'...'}}
.spin{width:2.2rem;height:2.2rem;margin:1.4rem;border:3px solid #0a3;border-top-color:#33ff66;
border-radius:50%;animation:s .8s linear infinite}@keyframes s{to{transform:rotate(360deg)}}
.err{color:#ff5555}
</style></head><body>
<h2>spinning up your server<span class=dots></span></h2>
<div class=spin></div>
<p id=msg>handing you Dave's box — a few seconds.</p>
<script>
const sid="__SID__";
async function poll(){
  try{
    const r=await fetch("/ready?sid="+sid,{cache:"no-store"});
    const j=await r.json();
    if(j.state==="ready"){location.replace("/app/"+sid);return;}
    if(j.state==="full"){document.getElementById("msg").outerHTML=
      "<p class=err>server full — all seats taken. <a style=color:#33ff66 href=/play>retry</a></p>";return;}
    if(j.state==="error"){document.getElementById("msg").outerHTML=
      "<p class=err>something broke spinning up. <a style=color:#33ff66 href=/play>retry</a></p>";return;}
  }catch(e){}
  setTimeout(poll,1200);
}
poll();
</script>
</body></html>"""

# The browser's save: {"v":1,"levels":{"<n>":"<password>"}} in localStorage.
# Read defensively - a hand-edited or corrupted save just loses the bad entries.
SAVE_JS = r"""var SAVE_KEY='handover-save';
function loadSave(){var lv={};
  try{var d=JSON.parse(localStorage.getItem(SAVE_KEY));
    if(d&&d.levels&&typeof d.levels==='object')for(var k in d.levels){var v=d.levels[k];
      if(/^([1-9]|1[0-9]|20)$/.test(k)&&typeof v==='string'&&/^[A-Za-z0-9]{1,64}$/.test(v))lv[k]=v;}
  }catch(e){}
  return lv;}
function storeSave(lv){try{localStorage.setItem(SAVE_KEY,JSON.stringify({v:1,levels:lv}));}catch(e){}}
function topLevel(lv){var m=0;for(var k in lv)m=Math.max(m,+k);return m;}"""

APP = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>The Handover</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;background:#000;color:#33ff66;
font-family:monospace;overflow:hidden}
#term{flex:1;border:0;height:100vh;width:100%}
#notes{width:300px;max-width:42vw;display:flex;flex-direction:column;
border-left:1px solid #0a3;background:#021}
#notes h3{margin:0;padding:.5rem .7rem;font-size:.8rem;border-bottom:1px solid #0a3;
display:flex;justify-content:space-between;align-items:center;font-weight:normal}
#notes h3 b{color:#33ff66}#notes h3 span{opacity:.55}
#pad{flex:1;background:#000;color:#33ff66;border:0;padding:.6rem;
font-family:monospace;font-size:.85rem;line-height:1.5;resize:none;outline:none}
#toggle{position:fixed;top:.4rem;right:.4rem;z-index:10;background:#021;color:#33ff66;
border:1px solid #0a3;padding:.35rem .7rem;cursor:pointer;font-family:monospace;display:none}
.hidden{display:none!important}
#toast{position:fixed;top:.6rem;left:50%;transform:translateX(-50%);z-index:20;
background:#021;border:1px solid #33ff66;padding:.5rem .9rem;max-width:90vw;transition:opacity .6s}
@media(max-width:700px){
  #notes{position:fixed;top:0;right:0;height:100vh;width:82vw;max-width:none;z-index:9;
    box-shadow:-8px 0 24px #000}
  #toggle{display:block}
}
</style></head><body>
<iframe id=term src="/t/__SID__/" title="terminal"></iframe>
<aside id=notes>
  <h3><b>your notes</b> <span id=st>saved in this browser</span></h3>
  <textarea id=pad spellcheck=false placeholder="Your progress saves automatically. Jot down anything else worth remembering.

Stays in your browser &mdash; never sent to the server."></textarea>
</aside>
<button id=toggle>notes</button>
<script>
var pad=document.getElementById('pad'),notes=document.getElementById('notes'),
    tg=document.getElementById('toggle'),KEY='handover-notes';
try{pad.value=localStorage.getItem(KEY)||'';}catch(e){}
pad.addEventListener('input',function(){try{localStorage.setItem(KEY,pad.value);}catch(e){}});
tg.addEventListener('click',function(){notes.classList.toggle('hidden');});
if(matchMedia('(max-width:700px)').matches)notes.classList.add('hidden');
__SAVE_JS__
var st=document.getElementById('st'),noted=false;
function toast(t){var d=document.createElement('div');d.id='toast';d.textContent=t;
  document.body.appendChild(d);setTimeout(function(){d.style.opacity=0;},5000);
  setTimeout(function(){d.remove();},6000);}
function sync(){
  fetch('/save/__SID__',{cache:'no-store'}).then(function(r){
    if(r.status===404){location.replace('/');return null;}   // session recycled
    return r.ok?r.json():null;})
  .then(function(j){
    if(!j)return;
    var lv=loadSave();
    (j.drop||[]).forEach(function(n){delete lv[n];});
    for(var k in j.levels)lv[k]=j.levels[k];
    storeSave(lv);
    var n=topLevel(lv);
    st.textContent=n?'progress saved: level '+n:'saved in this browser';
    if(j.note&&!noted){noted=true;toast(j.note);}
  }).catch(function(){});
}
sync();setInterval(sync,20000);
</script>
</body></html>"""


async def h_landing(request):
    if TS_SITEKEY:
        script = ('<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"'
                  ' async defer></script>')
        widget = (f'<div class="cf-turnstile" data-sitekey="{TS_SITEKEY}"'
                  f' data-action="{TS_ACTION}" data-theme="dark"></div>')
    else:
        script = widget = ""
    uid, disp = identity(request)
    if uid:
        row = player_row(uid)
        resume = ""
        if row and row[2]:
            resume = " &middot; you finished &#127881;"
        elif row and row[0] and row[0] > 0:
            resume = f" &middot; you reached level {min(row[0], MAX_RESUME)}"
        signin = (f'signed in as {html.escape(disp)}{resume} &middot; '
                  '<a href="/logout" style="color:#33ff66">log out</a>')
    elif oauth_ready():
        signin = ('<a href="/login" style="color:#33ff66">sign in with GitHub</a>'
                  ' &mdash; same box on every device, and the leaderboard')
    else:
        signin = ""
    sid = resolve_sid(request)
    if sid in sessions and outdated(sessions[sid]):
        lvl = reached_level(sessions[sid])
        signin = ('<b>Dave\'s box got an update.</b> Press play to continue on the new version'
                  + (f" at level {lvl}" if lvl else "") + ".<br>" + signin)
    page = (LANDING.replace("__TS_SCRIPT__", script)
                   .replace("__TS_WIDGET__", widget)
                   .replace("__SIGNIN__", signin)
                   .replace("__SAVE_JS__", SAVE_JS)
                   .replace("__SRV__", str(server_level(uid))))
    return web.Response(text=page, content_type="text/html")


def server_level(uid) -> int:
    """The level a signed-in player has reached, from the leaderboard DB."""
    row = player_row(uid) if uid else None
    return min(row[0], MAX_RESUME) if row and row[0] else 0


async def verify_turnstile(token: str, ip: str) -> bool:
    """Server-side Turnstile check. No secret configured -> don't block (lets the
    site run before the secret is placed). Token is single-use."""
    if not TS_SECRET:
        return True
    if not token:
        return False
    body = {"secret": TS_SECRET, "response": token, "remoteip": ip}
    try:
        async with ClientSession() as cs:
            async with cs.post(TS_VERIFY_URL, data=body, timeout=ClientTimeout(total=10)) as r:
                j = await r.json()
    except Exception:
        log.exception("turnstile siteverify error")
        return False
    ok = (bool(j.get("success"))
          and j.get("action") == TS_ACTION
          and (not TS_HOSTS or j.get("hostname") in TS_HOSTS))
    if not ok:
        log.warning("turnstile reject: %s",
                    {k: j.get(k) for k in ("success", "action", "hostname", "error-codes")})
    return ok


async def h_play(request):
    """POST only: find-or-create this visitor's container. A GET can never spawn
    (so crawlers / prefetch / a one-line loop can't fill every seat)."""
    uid, disp = identity(request)
    sid = resolve_sid(request)
    # returning visitor on a replaced image → new container at the level they
    # reached (known server-side). It replaces a seat, so no bot check/throttle.
    if sid and sid in sessions and outdated(sessions[sid]):
        lvl = reached_level(sessions[sid])
        owner = who.get(sid)
        await teardown(sid, "upgrade")
        who[sid] = owner or {"uid": uid, "display": disp}
        remember(sid, uid, disp)
        resume_req[sid] = ({}, lvl, "the server was updated - " +
                           (f"resumed you at level {lvl}" if lvl else "you're on the new version"))
        start_provision(sid)
        return spinner(sid)
    # returning visitor (this device or another one signed in to the same
    # account) whose box is alive or starting → straight in, no new spawn
    if sid and (sid in sessions or states.get(sid) == "pending"):
        remember(sid, uid, disp)
        if sid in sessions:
            return with_cookie(web.HTTPFound(f"/app/{sid}"), sid)
        return spinner(sid)
    ip = client_ip(request)
    form = await request.post()
    # bot check before we spend a container (no-op until the secret is configured)
    if TS_SECRET:
        if not await verify_turnstile(form.get("cf-turnstile-response", ""), ip):
            return web.Response(text=TS_FAIL, content_type="text/html", status=403)
    # new container: throttle per real client IP so one source can't take all seats
    if throttled(ip):
        return web.Response(text=SLOWDOWN, content_type="text/html", status=429)
    sid = sid or secrets.token_hex(16)
    who[sid] = {"uid": uid, "display": disp}
    if form.get("mode") == "resume":
        resume_req[sid] = (parse_save(form.get("save", "")), server_level(uid), "")
    start_provision(sid)
    return spinner(sid)


async def h_play_get(request):
    """A GET must never create a container. Live sessions go to their terminal;
    everyone else goes back to the landing page to press Play (which POSTs)."""
    sid = resolve_sid(request)
    if sid and sid in sessions and not outdated(sessions[sid]):
        return with_cookie(web.HTTPFound(f"/app/{sid}"), sid)
    return web.HTTPFound("/")


async def h_ready(request):
    sid = request.query.get("sid", "")
    if sid in sessions:
        return web.json_response({"state": "ready"})
    return web.json_response({"state": states.get(sid, "pending")})


def owns(request, sid: str) -> bool:
    """True if this request carries the session cookie for `sid`, or is signed
    in to the GitHub account that owns it. The sid is also in the URL, so this
    binds a terminal to its owner: a leaked /app/<sid> or /t/<sid>/ URL is
    useless without the matching httponly `hid` cookie or account."""
    uid = identity(request)[0]
    owner = who.get(sid, {}).get("uid")
    if uid and owner and uid != owner:   # signed in as someone else: never theirs
        return False
    return request.cookies.get(COOKIE) == sid or (bool(uid) and uid == owner)


async def h_app(request):
    sid = request.match_info["sid"]
    if sid not in sessions:          # expired/reset → landing page (Play re-spawns via POST)
        return web.HTTPFound("/")
    if not owns(request, sid):       # not your session → back to the start
        return web.HTTPFound("/")
    if outdated(sessions[sid]):      # reload after an image deploy → offer the upgrade
        return web.HTTPFound("/")
    return web.Response(text=APP.replace("__SAVE_JS__", SAVE_JS).replace("__SID__", sid),
                        content_type="text/html")


async def h_save(request):
    """Passwords of the levels reached in this session, for the browser's save,
    plus saved levels that failed to verify at resume (the browser drops them)."""
    sid = request.match_info["sid"]
    s = sessions.get(sid)
    if not s or not owns(request, sid):
        return web.json_response({}, status=404)
    return web.json_response(
        {"levels": {str(k): v for k, v in s["passes"].items()},
         "drop": s["drop"], "note": s["note"]},
        headers={"Cache-Control": "no-store"})


HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "content-length"}


async def h_proxy(request):
    sid = request.match_info["sid"]
    s = sessions.get(sid)
    if not s:
        return web.HTTPFound("/")
    if not owns(request, sid):          # bind the terminal/ws to its owner's cookie
        return web.HTTPForbidden(text="not your session")
    s["last"] = time.time()
    upstream = f"http://127.0.0.1:{s['port']}{request.rel_url}"

    if request.headers.get("Upgrade", "").lower() == "websocket":
        ws_server = web.WebSocketResponse(protocols=("tty",))
        await ws_server.prepare(request)
        live_ws.add(ws_server)                    # so on_shutdown can close it
        try:
            async with ClientSession() as cs:
                async with cs.ws_connect(upstream, protocols=("tty",)) as ws_up:
                    async def c2s():
                        async for m in ws_server:
                            s["last"] = time.time()
                            if m.type == WSMsgType.TEXT:
                                await ws_up.send_str(m.data)
                            elif m.type == WSMsgType.BINARY:
                                await ws_up.send_bytes(m.data)
                            else:
                                break
                    async def s2c():
                        async for m in ws_up:
                            if m.type == WSMsgType.TEXT:
                                await ws_server.send_str(m.data)
                            elif m.type == WSMsgType.BINARY:
                                await ws_server.send_bytes(m.data)
                            else:
                                break
                    # Whichever direction ends first (a side closed, or the
                    # server closed ws_server on shutdown), cancel the other so
                    # the handler returns instead of hanging on a half-open pipe.
                    t1 = asyncio.ensure_future(c2s())
                    t2 = asyncio.ensure_future(s2c())
                    _, pending = await asyncio.wait(
                        {t1, t2}, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
        finally:
            live_ws.discard(ws_server)
        return ws_server

    # plain HTTP
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    async with ClientSession(auto_decompress=False) as cs:
        async with cs.request(request.method, upstream, headers=headers,
                              data=await request.read(), allow_redirects=False) as r:
            body = await r.read()
            out = {k: v for k, v in r.headers.items() if k.lower() not in HOP}
            return web.Response(status=r.status, headers=out, body=body)


def fmt_dur(sec: int) -> str:
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


async def h_story(request):
    return web.Response(text=STORY, content_type="text/html")


async def h_leaderboard(request):
    rows = []
    try:
        c = db()
        rows = c.execute("SELECT display, accumulated FROM players "
                         "WHERE finished=1 AND COALESCE(unranked,0)=0 "
                         "ORDER BY accumulated ASC LIMIT 50").fetchall()
        c.close()
    except Exception:
        log.exception("leaderboard read failed")
    body = "".join(
        f"<tr><td>{i + 1}</td><td>{html.escape(d)}</td><td>{fmt_dur(t)}</td></tr>"
        for i, (d, t) in enumerate(rows)
    ) or '<tr><td colspan="3">nobody\'s finished yet — be the first.</td></tr>'
    return web.Response(text=LEADERBOARD.replace("__ROWS__", body), content_type="text/html")


def oauth_ready() -> bool:
    return bool(GH_CLIENT_ID and GH_CLIENT_SECRET and SESSION_SECRET)


async def h_login(request):
    if not oauth_ready():
        return web.HTTPFound("/")
    state = secrets.token_urlsafe(16)
    url = ("https://github.com/login/oauth/authorize"
           f"?client_id={GH_CLIENT_ID}&redirect_uri={PUBLIC_URL}/auth/callback"
           f"&scope=read:user&state={state}")
    resp = web.HTTPFound(url)
    resp.set_cookie("oauth_state", state, httponly=True, samesite="Lax", max_age=600, path="/")
    return resp


async def h_callback(request):
    code = request.query.get("code", "")
    state = request.query.get("state", "")
    if not oauth_ready() or not code or not state or state != request.cookies.get("oauth_state"):
        return web.HTTPFound("/")
    try:
        async with ClientSession() as cs:
            async with cs.post("https://github.com/login/oauth/access_token",
                               headers={"Accept": "application/json"},
                               data={"client_id": GH_CLIENT_ID, "client_secret": GH_CLIENT_SECRET,
                                     "code": code, "redirect_uri": f"{PUBLIC_URL}/auth/callback"},
                               timeout=ClientTimeout(total=10)) as r:
                tok = (await r.json()).get("access_token")
            if not tok:
                return web.HTTPFound("/")
            async with cs.get("https://api.github.com/user",
                              headers={"Authorization": f"Bearer {tok}", "User-Agent": "handover",
                                       "Accept": "application/vnd.github+json"},
                              timeout=ClientTimeout(total=10)) as r:
                u = await r.json()
    except Exception:
        log.exception("github oauth error")
        return web.HTTPFound("/")
    if not u.get("id") or not u.get("login"):
        return web.HTTPFound("/")
    resp = web.HTTPFound("/")
    resp.set_cookie("ghid", _sign({"uid": u["id"], "login": u["login"]}),
                    httponly=True, samesite="Lax", secure=True, max_age=MAX_LIFE, path="/")
    resp.del_cookie("oauth_state", path="/")
    return resp


async def h_logout(request):
    resp = web.HTTPFound("/")
    resp.del_cookie("ghid", path="/")
    resp.del_cookie(COOKIE, path="/")   # the box stays with the account, not this device
    return resp


async def h_health(request):
    return web.json_response({"players": len(sessions), "max": MAX_PLAYERS})


async def on_start(app):
    db().close()   # ensure the leaderboard table exists
    init_counters()
    # Re-adopt containers that outlived a previous spawner (deploy/crash/reboot).
    # Incus keeps the containers running; only our in-memory map + ttyd procs are
    # lost, so clear any stale ttyd and respawn one per still-live container.
    await run("pkill", "-x", "ttyd")
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    global live_image
    live_image = await image_fingerprint(BASE_IMAGE)
    for sid, s in data.items():
        rc, _ = await run("incus", "exec", s["ct"], "--", "true")
        if rc != 0:
            continue  # container is gone; drop it
        start = s.get("start", 0)
        if s.get("who"):
            who[sid] = s["who"]
        proc = await start_ttyd(sid, s["ct"], s["port"], login_user(start))
        sessions[sid] = {"ct": s["ct"], "port": s["port"], "proc": proc,
                         "created": s.get("created", time.time()), "last": time.time(),
                         "start": start, "passes": {}, "drop": [], "note": "",
                         "image": s.get("image") or await container_image(s["ct"]),
                         "counted": s.get("counted", []),
                         "cmd_cursor": s.get("cmd_cursor", "")}
    if sessions:
        log.info("re-adopted %d live session(s) after restart", len(sessions))
    save_state()
    app["reaper"] = asyncio.create_task(reaper(app))
    # Metrics on their own localhost-only port: play.barrypekerman.com can't reach them.
    mapp = web.Application()
    mapp.router.add_get("/metrics", h_metrics)
    app["metrics_runner"] = web.AppRunner(mapp)
    await app["metrics_runner"].setup()
    await web.TCPSite(app["metrics_runner"], "127.0.0.1", METRICS_PORT).start()


async def on_shutdown(app):
    # Terminal WebSockets are long-lived; aiohttp's graceful shutdown would
    # otherwise block on them until the systemd stop-timeout SIGKILLs us. Close
    # them so the proxy handlers return at once. Containers keep running and the
    # browser's ttyd client auto-reconnects once we're back.
    for ws in list(live_ws):
        with contextlib.suppress(Exception):
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server restarting")


async def on_cleanup(app):
    # Non-destructive: cancel the reaper and persist state, but DO NOT delete
    # containers — on_start re-adopts them, so a restart no longer resets players.
    app["reaper"].cancel()
    save_state()
    await app["metrics_runner"].cleanup()


def make_app():
    app = web.Application()
    app.add_routes([
        web.get("/", h_landing),
        web.post("/play", h_play),      # spawning happens only on POST
        web.get("/play", h_play_get),   # a GET never spawns
        web.get("/ready", h_ready),
        web.get("/app/{sid}", h_app),
        web.get("/save/{sid}", h_save),
        web.get("/leaderboard", h_leaderboard),
        web.get("/story", h_story),
        web.get("/login", h_login),
        web.get("/auth/callback", h_callback),
        web.get("/logout", h_logout),
        web.get("/healthz", h_health),
        web.route("*", "/t/{sid}/{tail:.*}", h_proxy),
    ])
    app.on_startup.append(on_start)
    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    # shutdown_timeout is a backstop: even if a socket won't close, give up
    # fast instead of hanging out the full systemd stop-timeout.
    web.run_app(make_app(), host=LISTEN_HOST, port=LISTEN_PORT, shutdown_timeout=5.0)
