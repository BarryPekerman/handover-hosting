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
import asyncio, os, secrets, time, logging, json
from aiohttp import web, ClientSession, WSMsgType

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spawner")

# sid -> {ct, port, proc, created, last}
sessions: dict[str, dict] = {}
locks: dict[str, asyncio.Lock] = {}      # per-sid provisioning lock
states: dict[str, str] = {}              # sid -> "pending"|"full"|"error" (ready == in sessions)


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
    s = sessions.pop(sid, None)
    locks.pop(sid, None)
    states.pop(sid, None)
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
a.play{margin-top:2rem;padding:.8rem 2.2rem;border:1px solid #33ff66;color:#33ff66;
text-decoration:none;font-size:1.2rem}a.play:hover{background:#33ff66;color:#000}
p{max-width:34rem;line-height:1.5;padding:0 1rem}
</style></head><body>
<h1>THE HANDOVER</h1>
<p>Dave quit &mdash; won the lottery &mdash; and left you his server. He left mail, too.
A browser terminal is all you need. Type <code>mail</code> to start.</p>
<p style="opacity:.75;font-size:.95rem">Tip: <b>save every password you find</b> &mdash; there's a notes panel beside the
terminal. Your session can reset; the passwords are how you pick back up.</p>
<a class=play href="/play">&gt; play</a>
</body></html>"""

FULL = """<!doctype html><html><head><meta charset=utf-8><title>Server full</title>
<style>body{background:#000;color:#ff5555;font-family:monospace;display:flex;height:100vh;
align-items:center;justify-content:center;text-align:center}</style></head><body>
<div><h1>server full</h1><p>All seats are taken right now. Try again in a few minutes.</p></div>
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
    return web.Response(text=LANDING, content_type="text/html")


async def h_play(request):
    sid = request.cookies.get(COOKIE)
    # returning visitor whose container is still alive → straight into the terminal
    if sid and sid in sessions:
        resp = web.HTTPFound(f"/app/{sid}")
        resp.set_cookie(COOKIE, sid, httponly=True, samesite="Lax", path="/", max_age=MAX_LIFE)
        return resp
    sid = sid or secrets.token_hex(16)
    # kick off provisioning in the background (unless already running) and answer instantly
    if sid not in sessions and states.get(sid) != "pending":
        states[sid] = "pending"
        asyncio.create_task(provision_bg(sid))
    resp = web.Response(text=SPAWNING.replace("__SID__", sid), content_type="text/html")
    resp.set_cookie(COOKIE, sid, httponly=True, samesite="Lax", path="/", max_age=MAX_LIFE)
    return resp


async def h_ready(request):
    sid = request.query.get("sid", "")
    if sid in sessions:
        return web.json_response({"state": "ready"})
    return web.json_response({"state": states.get(sid, "pending")})


async def h_app(request):
    sid = request.match_info["sid"]
    if sid not in sessions:          # expired/reset → back to the spinner, which re-provisions
        return web.HTTPFound("/play")
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


async def h_health(request):
    return web.json_response({"players": len(sessions), "max": MAX_PLAYERS})


async def on_start(app):
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
        web.get("/play", h_play),
        web.get("/ready", h_ready),
        web.get("/app/{sid}", h_app),
        web.get("/healthz", h_health),
        web.route("*", "/t/{sid}/{tail:.*}", h_proxy),
    ])
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=LISTEN_HOST, port=LISTEN_PORT)
