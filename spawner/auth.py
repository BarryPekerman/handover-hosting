"""Who a visitor is: the box cookie, signed GitHub sign-in, the player id,
the per-IP throttle and the Turnstile bot check.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from contextlib import closing
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout

from common import (
    COOKIE, MAX_LIFE, PID_RE, PUBLIC_URL, RATE_MAX, RATE_WINDOW, db, ip_hits, log, redirect,
    sessions, states, who,
)

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
GH_CLIENT_ID     = os.environ.get("GH_CLIENT_ID", "")
GH_CLIENT_SECRET = os.environ.get("GH_CLIENT_SECRET", "")
SESSION_SECRET   = os.environ.get("SESSION_SECRET", "")


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


def player_id(request, uid) -> str:
    """The player's random id: the account's (so devices agree), else this
    browser's cookie, else a new one. Never derived from anything about them."""
    if uid:
        with closing(db()) as c:
            row = c.execute("SELECT pid FROM player_ids WHERE uid=?", (str(uid),)).fetchone()
            if not row:
                pid = request.cookies.get("pid", "")
                pid = pid if PID_RE.fullmatch(pid) else secrets.token_hex(16)
                c.execute("INSERT INTO player_ids(uid, pid) VALUES(?,?)", (str(uid), pid))
                c.commit()
                row = (pid,)
        return row[0]
    pid = request.cookies.get("pid", "")
    return pid if PID_RE.fullmatch(pid) else secrets.token_hex(16)


# Over https (the live site) the id cookies never travel in clear text; a
# local http:// run keeps them usable.
SECURE_COOKIES = PUBLIC_URL.startswith("https://")


def with_pid(resp, pid: str):
    resp.set_cookie("pid", pid, httponly=True, samesite="Lax", secure=SECURE_COOKIES,
                    path="/", max_age=365 * 86400)
    return resp


def with_cookie(resp, sid: str):
    resp.set_cookie(COOKIE, sid, httponly=True, samesite="Lax", secure=SECURE_COOKIES,
                    path="/", max_age=MAX_LIFE)
    return resp


def same_origin(request) -> bool:
    """True unless a browser opened this from another site's page. The cookies
    are SameSite=Lax and every *.barrypekerman.com is the same site, so without
    this a page on any subdomain could open a player's terminal WebSocket with
    their cookie. No Origin header means no browser (curl, the tests): those
    still need the cookie. The public URL is accepted as well as the request's
    own host, so a proxy that rewrites Host can't lock players out."""
    origin = request.headers.get("Origin")
    return origin is None or urlsplit(origin).netloc in (request.host, urlsplit(PUBLIC_URL).netloc)


def rate_key(ip: str) -> str:
    """What the throttle counts: an IPv4 address, or an IPv6 address's /64
    (one subscriber gets a whole /64, so per-address would be no limit)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if a.version == 6 and a.ipv4_mapped is None:
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return str(getattr(a, "ipv4_mapped", None) or a)


def throttled(ip: str) -> bool:
    """True if this IP (IPv6: its /64) has started too many containers lately
    (records a hit if not). Sources with no start in the window are forgotten."""
    now = time.time()
    for k in [k for k, q in ip_hits.items() if not q or now - q[-1] >= RATE_WINDOW]:
        del ip_hits[k]
    q = ip_hits.setdefault(rate_key(ip), [])
    q[:] = [t for t in q if now - t < RATE_WINDOW]
    if len(q) >= RATE_MAX:
        return True
    q.append(now)
    return False


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


def oauth_ready() -> bool:
    return bool(GH_CLIENT_ID and GH_CLIENT_SECRET and SESSION_SECRET)


async def h_login(request):
    if not oauth_ready():
        return redirect("/")
    state = secrets.token_urlsafe(16)
    url = ("https://github.com/login/oauth/authorize"
           f"?client_id={GH_CLIENT_ID}&redirect_uri={PUBLIC_URL}/auth/callback"
           f"&scope=read:user&state={state}")
    resp = redirect(url)
    resp.set_cookie("oauth_state", state, httponly=True, samesite="Lax", max_age=600, path="/")
    return resp


async def h_callback(request):
    code = request.query.get("code", "")
    state = request.query.get("state", "")
    if not oauth_ready() or not code or not state or state != request.cookies.get("oauth_state"):
        return redirect("/")
    try:
        async with ClientSession() as cs:
            async with cs.post("https://github.com/login/oauth/access_token",
                               headers={"Accept": "application/json"},
                               data={"client_id": GH_CLIENT_ID, "client_secret": GH_CLIENT_SECRET,
                                     "code": code, "redirect_uri": f"{PUBLIC_URL}/auth/callback"},
                               timeout=ClientTimeout(total=10)) as r:
                tok = (await r.json()).get("access_token")
            if not tok:
                return redirect("/")
            async with cs.get("https://api.github.com/user",
                              headers={"Authorization": f"Bearer {tok}", "User-Agent": "handover",
                                       "Accept": "application/vnd.github+json"},
                              timeout=ClientTimeout(total=10)) as r:
                u = await r.json()
    except Exception:
        log.exception("github oauth error")
        return redirect("/")
    if not u.get("id") or not u.get("login"):
        return redirect("/")
    resp = redirect("/")
    resp.set_cookie("ghid", _sign({"uid": u["id"], "login": u["login"]}),
                    httponly=True, samesite="Lax", secure=True, max_age=MAX_LIFE, path="/")
    resp.del_cookie("oauth_state", path="/")
    return resp


async def h_logout(request):
    resp = redirect("/")
    resp.del_cookie("ghid", path="/")
    resp.del_cookie(COOKIE, path="/")   # the box stays with the account, not this device
    return resp
