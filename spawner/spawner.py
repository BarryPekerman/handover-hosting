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

Split across modules next to this file: common.py (settings, shared state,
database), auth.py (who a visitor is), metrics.py (Prometheus and Grafana's
player data), gamedata.py (gameplay numbers), pages/ (the HTML).

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
import contextlib
import hmac
import html
import json
import os
import re
import secrets
import shlex
import time
from aiohttp import web, ClientSession, WSMsgType, WSCloseCode

import common
from common import (
    BASE_IMAGE, IDLE_SEC, LISTEN_HOST, LISTEN_PORT, LOGIN_USER, MAX_LIFE, MAX_PLAYERS,
    MAX_RESUME, OUTDATED_IDLE, PASS_RE, PORT_BASE, PROFILE, PUBLIC_URL, STATE_FILE,
    WIN_INDEX, background, box_pid, db, events, live_ws, locks, log, pids, player_row,
    resume_req, sessions, states, who,
)
from metrics import (
    METRICS_PORT, count_commands, count_progress, inc, init_counters, make_metrics_app,
)
from auth import (
    TS_ACTION, TS_SECRET, TS_SITEKEY, client_ip, h_callback, h_login, h_logout, identity,
    oauth_ready, owns, player_id, remember, resolve_sid, throttled, verify_turnstile,
    with_cookie, with_pid,
)


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



def spinner(sid: str):
    """The "spinning up" page, which polls /ready and then opens the terminal."""
    return with_cookie(web.Response(text=SPAWNING.replace("__SID__", sid),
                                    content_type="text/html"), sid)



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
                      "who": who.get(sid, {"uid": None, "display": "anonymous"}),
                      "pid": pids.get(sid, "")}
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
    return bool(common.live_image and s.get("image") and s["image"] != common.live_image)


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
        box_pid[ct] = pids.get(sid, "")
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
    try:
        while True:
            await asyncio.sleep(30)
            common.live_image = await image_fingerprint(BASE_IMAGE) or common.live_image
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
# The pages are plain files in spawner/pages/ (copied next to spawner.py by
# deploy.yml), read once at startup; edit the text there. Placeholders are
# filled in here: __ICON__ (favicon, every page) and __SHARE__ (the link-preview
# og:/twitter: tags LinkedIn, Slack and chat apps build their card from, on the
# pages people share), then per request __SID__, __ROWS__, __SAVE_JS__ and the
# landing page's own. The story page holds the premise rather than the front
# page; Dave's own voice is kept for his first mail in the game (bandit-wannabe
# VOICE.md). save.js is the browser's save, {"v":1,"levels":{"<n>":"<password>"}}
# in localStorage, read defensively: a corrupted save just loses the bad entries.
# The images are spawner/static/.
HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(HERE, "static"))
PAGES_DIR = os.environ.get("PAGES_DIR", os.path.join(HERE, "pages"))


def read_page(name: str) -> str:
    with open(os.path.join(PAGES_DIR, name), encoding="utf-8") as f:
        return f.read().removesuffix("\n")


ICON = '<link rel=icon type=image/png href=/favicon.ico>'
SHARE = ICON + "\n" + read_page("share.html").replace("__PUBLIC_URL__", PUBLIC_URL)


def page(name: str) -> str:
    return read_page(name).replace("__SHARE__", SHARE).replace("__ICON__", ICON)


LANDING = page("landing.html")
STORY = page("story.html")
LEADERBOARD = page("leaderboard.html")
SPAWNING = page("spawning.html")
SAVE_JS = page("save.js")
APP = page("app.html")


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



async def h_play(request):
    """POST only: find-or-create this visitor's container. A GET can never spawn
    (so crawlers / prefetch / a one-line loop can't fill every seat)."""
    uid, disp = identity(request)
    sid = resolve_sid(request)
    pid = pids.get(sid) if sid else None
    pid = pid or player_id(request, uid)
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
        pids[sid] = pid
        start_provision(sid)
        return with_pid(spinner(sid), pid)
    # returning visitor (this device or another one signed in to the same
    # account) whose box is alive or starting → straight in, no new spawn
    if sid and (sid in sessions or states.get(sid) == "pending"):
        remember(sid, uid, disp)
        if sid in sessions:
            return with_pid(with_cookie(web.HTTPFound(f"/app/{sid}"), sid), pid)
        return with_pid(spinner(sid), pid)
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
    pids[sid] = pid
    if form.get("mode") == "resume":
        resume_req[sid] = (parse_save(form.get("save", "")), server_level(uid), "")
    start_provision(sid)
    return with_pid(spinner(sid), pid)


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
        # heartbeat: ping the browser every 30 s. Cloudflare closes a WebSocket
        # with no traffic for 100 s, so a player who stopped typing to read
        # lost the terminal every ~2 minutes.
        ws_server = web.WebSocketResponse(protocols=("tty",), heartbeat=30)
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
    common.live_image = await image_fingerprint(BASE_IMAGE)
    for sid, s in data.items():
        rc, _ = await run("incus", "exec", s["ct"], "--", "true")
        if rc != 0:
            continue  # container is gone; drop it
        start = s.get("start", 0)
        if s.get("who"):
            who[sid] = s["who"]
        if s.get("pid"):
            pids[sid] = box_pid[s["ct"]] = s["pid"]
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
    app["metrics_runner"] = web.AppRunner(make_metrics_app())
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


async def h_static(request):
    """og.png (link preview) and the favicon; cached for a day."""
    name = {"/og.png": "og.png", "/favicon.ico": "favicon.png"}[request.path]
    path = os.path.join(STATIC_DIR, name)
    if not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Content-Type": "image/png",
                                           "Cache-Control": "public, max-age=86400"})


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
        web.get("/og.png", h_static),
        web.get("/favicon.ico", h_static),
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
