#!/usr/bin/env python3
"""The Handover spawner: one isolated Incus container per browser visitor,
fronted by a single localhost port (Cloudflare tunnel terminates upstream).

Flow:
  GET  /            landing page with a Play button
  GET  /play        find-or-create this visitor's container + ttyd, 302 to /t/<sid>/
  ANY  /t/<sid>/*   reverse-proxy (HTTP + WebSocket) to that session's ttyd

Each session gets a NIC-less container (profile `isolated`, no network device)
and its own ttyd on a loopback port; the terminal rides `incus exec` over the
control socket, so containers reach neither each other, the host, nor the net.

Config via env (all optional):
  LISTEN_HOST=127.0.0.1  LISTEN_PORT=8080
  BASE_IMAGE=handover    PROFILE=isolated
  MAX_PLAYERS=20         IDLE_MIN=60   MAX_LIFETIME_MIN=1440
  TTYD_PORT_BASE=8100    LOGIN_USER=level0
"""
import asyncio, os, secrets, time, logging, json, sqlite3, html, hmac, hashlib, base64
from aiohttp import web, ClientSession, WSMsgType, ClientTimeout

LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
BASE_IMAGE  = os.environ.get("BASE_IMAGE", "handover")
PROFILE     = os.environ.get("PROFILE", "isolated")
MAX_PLAYERS = int(os.environ.get("MAX_PLAYERS", "20"))
IDLE_SEC    = int(os.environ.get("IDLE_MIN", "60")) * 60
MAX_LIFE    = int(os.environ.get("MAX_LIFETIME_MIN", "1440")) * 60
PORT_BASE   = int(os.environ.get("TTYD_PORT_BASE", "8100"))
LOGIN_USER  = os.environ.get("LOGIN_USER", "level0")
COOKIE      = "hid"
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


def db():
    c = sqlite3.connect(DB_PATH)
    # key = GitHub uid for logged-in players (persists across sessions), or
    # "anon:<sid>" for anonymous players (single-session). accumulated = sum of
    # each level's solve time, counted once.
    c.execute("CREATE TABLE IF NOT EXISTS players "
              "(key TEXT PRIMARY KEY, display TEXT, max_level INTEGER, "
              " accumulated INTEGER, finished INTEGER, updated REAL)")
    return c


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
  journalctl -q -o short-iso SYSLOG_IDENTIFIER=su --grep 'session opened for user' 2>/dev/null \
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


async def update_progress(sid: str, ct: str):
    """Fold this container's progress into the player's running tally. Keyed by
    GitHub uid (cross-session) or anon:<sid> (single session). Adds only the
    time for levels newly completed since the stored max, so fast re-climbs with
    saved passwords don't double-count and between-session breaks never count."""
    info = who.get(sid) or {"uid": None, "display": "anonymous"}
    key = info["uid"] or ("anon:" + sid)
    display = info["display"]
    rc, out = await run("incus", "exec", ct, "--", "bash", "-c", PROGRESS_SH)
    reaches = {}
    for ln in out.splitlines():
        p = ln.split()
        if len(p) == 2 and p[0].isdigit() and p[1].isdigit():
            reaches[int(p[0])] = int(p[1])
    if not reaches:
        return
    new_max = max(reaches)
    row = player_row(key)
    prev_max, accum, finished = row if row else (0, 0, 0)
    if new_max <= prev_max:
        return                                   # no new levels completed
    added = 0
    for idx in range(prev_max, new_max):         # count each newly-completed level once
        if idx in reaches and (idx + 1) in reaches:
            d = reaches[idx + 1] - reaches[idx]
            if d >= 0:
                added += d
    accum += added
    finished = 1 if new_max >= 21 else finished
    try:
        c = db()
        c.execute("INSERT INTO players(key,display,max_level,accumulated,finished,updated) "
                  "VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                  "display=excluded.display, max_level=excluded.max_level, "
                  "accumulated=excluded.accumulated, finished=excluded.finished, "
                  "updated=excluded.updated",
                  (key, display, new_max, accum, finished, time.time()))
        c.commit(); c.close()
    except Exception:
        log.exception("leaderboard write failed")
    log.info("progress key=%s max=%d +%ds total=%ds fin=%d", key[:16], new_max, added, accum, finished)


async def record_finishers():
    for sid, s in list(sessions.items()):
        await update_progress(sid, s["ct"])


async def sweep_orphans():
    """Delete any p-* container the spawner isn't tracking. Every legitimate
    container is either a live session or mid-provision (its sid is in `states`
    'pending'), so anything else is abandoned — a crash mid-provision, a stale
    launch, a desync — and would otherwise leak RAM + inotify forever."""
    live = {"p-" + s[:12] for s in sessions} | {"p-" + s[:12] for s in states}
    rc, out = await run("incus", "list", "-c", "n", "-f", "csv")
    for name in out.splitlines():
        name = name.strip()
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


def identity_display(request) -> str:
    return identity(request)[1]


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
        data = {sid: {k: s[k] for k in ("ct", "port", "created", "last")}
                for sid, s in sessions.items()}
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        log.exception("save_state failed")


async def start_ttyd(sid: str, ct: str, port: int):
    """Launch a ttyd bound to loopback that attaches into the container."""
    return await asyncio.create_subprocess_exec(
        "ttyd", "-p", str(port), "-i", "127.0.0.1", "-b", f"/t/{sid}",
        "-W", "incus", "exec", ct, "--", "su", "-", LOGIN_USER,
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
            rc, out = await run("incus", "exec", ct, "--", "systemctl", "is-system-running")
            if out.strip() in ("running", "degraded"):
                ok = True
                break
            await asyncio.sleep(1)
        if not ok:
            log.error("container %s never became ready; tearing down", ct)
            await run("incus", "delete", "-f", ct)
            return None
        port = alloc_port()
        proc = await start_ttyd(sid, ct, port)
        now = time.time()
        sessions[sid] = {"ct": ct, "port": port, "proc": proc, "created": now, "last": now}
        save_state()
        log.info("provisioned sid=%s ct=%s port=%d (%d/%d)", sid[:12], ct, port, len(sessions), MAX_PLAYERS)
        # give ttyd a moment to bind
        await asyncio.sleep(0.5)
        return sessions[sid]


async def provision_bg(sid: str):
    """Background provisioning so /play can answer instantly with a spinner."""
    try:
        s = await provision(sid)
        states[sid] = "ready" if s else "full"
    except Exception:
        log.exception("provision failed sid=%s", sid)
        states[sid] = "error"


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
    try:
        s["proc"].terminate()
    except (ProcessLookupError, AttributeError):
        pass
    await run("incus", "delete", "-f", s["ct"])
    save_state()


async def reaper(app):
    try:
        while True:
            await asyncio.sleep(30)
            await record_finishers()      # capture victories before reaping
            await sweep_orphans()         # GC containers nobody is tracking
            now = time.time()
            for sid, s in list(sessions.items()):
                if now - s["last"] > IDLE_SEC:
                    await teardown(sid, "idle")
                elif now - s["created"] > MAX_LIFE:
                    await teardown(sid, "max-lifetime")
    except asyncio.CancelledError:
        pass


# ---------- HTTP handlers ----------

LANDING = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>The Handover</title><style>
:root{color-scheme:dark}
body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;
justify-content:center;background:#000;color:#33ff66;font-family:monospace;text-align:center}
.play{margin-top:2rem;padding:.8rem 2.2rem;border:1px solid #33ff66;color:#33ff66;
background:none;font-family:monospace;font-size:1.2rem;cursor:pointer}
.play:hover{background:#33ff66;color:#000}
p{max-width:34rem;line-height:1.5;padding:0 1rem}
.cf-turnstile{margin:1.4rem auto 0}
</style>__TS_SCRIPT__</head><body>
<h1>THE HANDOVER</h1>
<p>Dave quit &mdash; won the lottery &mdash; and left you his server. He left mail, too.
A browser terminal is all you need. Type <code>mail</code> to start.</p>
<p style="opacity:.75;font-size:.95rem">Tip: <b>save every password you find</b> &mdash; there's a notes panel beside the
terminal. Your session can reset; the passwords are how you pick back up.</p>
<form method="post" action="/play">__TS_WIDGET__<br><button class=play type=submit>&gt; play</button></form>
<p style="margin-top:1.5rem"><a href="/leaderboard" style="color:#33ff66;opacity:.8">leaderboard &rarr;</a></p>
<p style="opacity:.8;font-size:.9rem">__SIGNIN__</p>
</body></html>"""

FULL = """<!doctype html><html><head><meta charset=utf-8><title>Server full</title>
<style>body{background:#000;color:#ff5555;font-family:monospace;display:flex;height:100vh;
align-items:center;justify-content:center;text-align:center}</style></head><body>
<div><h1>server full</h1><p>All seats are taken right now. Try again in a few minutes.</p></div>
</body></html>"""

SLOWDOWN = """<!doctype html><html><head><meta charset=utf-8><title>Slow down</title>
<style>body{background:#000;color:#ffcc33;font-family:monospace;display:flex;height:100vh;
align-items:center;justify-content:center;text-align:center}</style></head><body>
<div><h1>easy there</h1><p>You've started a few servers in a short time. Give it a
couple of minutes, then <a style=color:#ffcc33 href="/">head back</a> and press Play.</p></div>
</body></html>"""

TS_FAIL = """<!doctype html><html><head><meta charset=utf-8><title>Are you human?</title>
<style>body{background:#000;color:#ff5555;font-family:monospace;display:flex;height:100vh;
align-items:center;justify-content:center;text-align:center}</style></head><body>
<div><h1>couldn't verify you</h1><p>The human check didn't pass. <a style=color:#ff5555
href="/">go back</a> and press Play again.</p></div>
</body></html>"""

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
@media(max-width:700px){
  #notes{position:fixed;top:0;right:0;height:100vh;width:82vw;max-width:none;z-index:9;
    box-shadow:-8px 0 24px #000}
  #toggle{display:block}
}
</style></head><body>
<iframe id=term src="/t/__SID__/" title="terminal"></iframe>
<aside id=notes>
  <h3><b>your notes</b> <span>saved in this browser</span></h3>
  <textarea id=pad spellcheck=false placeholder="Save each password here as you find it.

Stays in your browser &mdash; survives a session reset, never sent to the server."></textarea>
</aside>
<button id=toggle>notes</button>
<script>
var pad=document.getElementById('pad'),notes=document.getElementById('notes'),
    tg=document.getElementById('toggle'),KEY='handover-notes';
try{pad.value=localStorage.getItem(KEY)||'';}catch(e){}
pad.addEventListener('input',function(){try{localStorage.setItem(KEY,pad.value);}catch(e){}});
tg.addEventListener('click',function(){notes.classList.toggle('hidden');});
if(matchMedia('(max-width:700px)').matches)notes.classList.add('hidden');
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
            lvl = min(row[0], 20)
            resume = f' &middot; you reached level {lvl} &mdash; <code>go {lvl}</code> to resume'
        signin = (f'signed in as {html.escape(disp)}{resume} &middot; '
                  '<a href="/logout" style="color:#33ff66">log out</a>')
    elif oauth_ready():
        signin = ('<a href="/login" style="color:#33ff66">sign in with GitHub</a>'
                  ' &mdash; save progress &amp; join the leaderboard across sessions')
    else:
        signin = ""
    page = (LANDING.replace("__TS_SCRIPT__", script)
                   .replace("__TS_WIDGET__", widget)
                   .replace("__SIGNIN__", signin))
    return web.Response(text=page, content_type="text/html")


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
    sid = request.cookies.get(COOKIE)
    # returning visitor whose container is still alive → straight in, no new spawn
    if sid and sid in sessions:
        uid, disp = identity(request)
        who[sid] = {"uid": uid, "display": disp}
        resp = web.HTTPFound(f"/app/{sid}")
        resp.set_cookie(COOKIE, sid, httponly=True, samesite="Lax", path="/", max_age=MAX_LIFE)
        return resp
    ip = client_ip(request)
    # bot check before we spend a container (no-op until the secret is configured)
    if TS_SECRET:
        form = await request.post()
        if not await verify_turnstile(form.get("cf-turnstile-response", ""), ip):
            return web.Response(text=TS_FAIL, content_type="text/html", status=403)
    # new container: throttle per real client IP so one source can't take all seats
    if throttled(ip):
        return web.Response(text=SLOWDOWN, content_type="text/html", status=429)
    sid = sid or secrets.token_hex(16)
    uid, disp = identity(request)
    who[sid] = {"uid": uid, "display": disp}
    if sid not in sessions and states.get(sid) != "pending":
        states[sid] = "pending"
        asyncio.create_task(provision_bg(sid))
    resp = web.Response(text=SPAWNING.replace("__SID__", sid), content_type="text/html")
    resp.set_cookie(COOKIE, sid, httponly=True, samesite="Lax", path="/", max_age=MAX_LIFE)
    return resp


async def h_play_get(request):
    """A GET must never create a container. Live sessions go to their terminal;
    everyone else goes back to the landing page to press Play (which POSTs)."""
    sid = request.cookies.get(COOKIE)
    if sid and sid in sessions:
        return web.HTTPFound(f"/app/{sid}")
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
    return web.Response(text=APP.replace("__SID__", sid), content_type="text/html")


HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "content-length"}


async def h_proxy(request):
    sid = request.match_info["sid"]
    s = sessions.get(sid)
    if not s:
        return web.HTTPFound("/")
    s["last"] = time.time()
    upstream = f"http://127.0.0.1:{s['port']}{request.rel_url}"

    if request.headers.get("Upgrade", "").lower() == "websocket":
        ws_server = web.WebSocketResponse(protocols=("tty",))
        await ws_server.prepare(request)
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
                await asyncio.gather(c2s(), s2c(), return_exceptions=True)
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


async def h_leaderboard(request):
    rows = []
    try:
        c = db()
        rows = c.execute("SELECT display, accumulated FROM players "
                         "WHERE finished=1 ORDER BY accumulated ASC LIMIT 50").fetchall()
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
    return resp


async def h_health(request):
    return web.json_response({"players": len(sessions), "max": MAX_PLAYERS})


async def on_start(app):
    db().close()   # ensure the leaderboard table exists
    # Re-adopt containers that outlived a previous spawner (deploy/crash/reboot).
    # Incus keeps the containers running; only our in-memory map + ttyd procs are
    # lost, so clear any stale ttyd and respawn one per still-live container.
    await run("pkill", "-x", "ttyd")
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    for sid, s in data.items():
        rc, _ = await run("incus", "exec", s["ct"], "--", "true")
        if rc != 0:
            continue  # container is gone; drop it
        proc = await start_ttyd(sid, s["ct"], s["port"])
        sessions[sid] = {"ct": s["ct"], "port": s["port"], "proc": proc,
                         "created": s.get("created", time.time()), "last": time.time()}
    if sessions:
        log.info("re-adopted %d live session(s) after restart", len(sessions))
    save_state()
    app["reaper"] = asyncio.create_task(reaper(app))


async def on_cleanup(app):
    # Non-destructive: cancel the reaper and persist state, but DO NOT delete
    # containers — on_start re-adopts them, so a restart no longer resets players.
    app["reaper"].cancel()
    save_state()


def make_app():
    app = web.Application()
    app.add_routes([
        web.get("/", h_landing),
        web.post("/play", h_play),      # spawning happens only on POST
        web.get("/play", h_play_get),   # a GET never spawns
        web.get("/ready", h_ready),
        web.get("/app/{sid}", h_app),
        web.get("/leaderboard", h_leaderboard),
        web.get("/login", h_login),
        web.get("/auth/callback", h_callback),
        web.get("/logout", h_logout),
        web.get("/healthz", h_health),
        web.route("*", "/t/{sid}/{tail:.*}", h_proxy),
    ])
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=LISTEN_HOST, port=LISTEN_PORT)
